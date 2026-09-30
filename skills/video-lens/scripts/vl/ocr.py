"""OCR (spec 7.6): frames exported at min(width, 1920) (cropped to --roi), Apple Vision accurate in --jobs parallel
swift/ocr.swift processes, resumable rows in the cache, fuzzy text events, persistent text and legibility.
Ported from D2 ocr.swift and its text-event rule.
"""
import difflib
import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from . import decode, sheets_content
from .context import content_time
from .swiftbuild import helper_path

ENGINE = "apple-vision-accurate"
OCR_MAX_WIDTH = 1920            # Korean needs about 16 px glyph height; exports never upscale
BATCH_PER_JOB = 24              # images per OCR process per batch; the next batch exports while this one is read
HELPER_TIMEOUT_S = 60           # one image takes 0.05-0.5 s; a helper silent this long is stuck in Vision and killed
MIN_CONF = 0.3
MIN_CHARS = 2
MATCH_RATIO = 0.8
PERSISTENT_SHARE = 0.5
PERSISTENT_MIN_SAMPLES = 3
LEGIBLE_PX = 8                  # glyph height in a keyframe-sheet tile; 6 px read 40/40 on a clean synthetic
INK_CHANGE_LEVELS = 40          # grey change that counts as a changed glyph pixel (codec noise stays far below)
TEXT_REGION_CHANGED = 0.005     # one changed syllable moved 1.6 to 12 % of a line box's pixels; identical text 0 %
NON_WORD = re.compile(r"[\W_]+")
NON_DIGIT = re.compile(r"\D+")


def time_key(t):
    """Rows and keyframes meet on this key: frame times rounded to 0.1 ms."""
    return f"{float(t):.4f}"


def analyze_text(run, times, cached_times=()):
    """(text section, ocr info, {time_key: [text ids]}) for the OCR samples at `times` (sorted frame times).
    `cached_times` were already exported at the OCR width into the frame cache (the keyframes): no second decode."""
    langs = run.params["content"]["ocr_langs"]
    rows = ocr_rows(run, times, langs, {time_key(t) for t in cached_times})
    region = ocr_region(run)
    events, ids_per_row = text_events(rows, region, run.display_size, run.range.end_s)
    persistent, events, ids_per_row = split_persistent(events, ids_per_row, len(rows))
    for event in events:
        event["legible_in_sheet"] = event["px_h"] * sheets_content.KF_TILE_W / run.display_size[0] >= LEGIBLE_PX
    ids_by_time = {time_key(row["t"]): ids for row, ids in zip(rows, ids_per_row)}
    info = {"engine": ENGINE, "langs": langs, "samples": len(rows), "frame_width": min(region[2], OCR_MAX_WIDTH)}
    return {"persistent": persistent, "events": events}, info, ids_by_time


def ocr_region(run):
    """(x, y, w, h) in display px: --roi, else the whole frame."""
    width, height = run.display_size
    return tuple(run.params["roi"]) if run.params["roi"] else (0, 0, width, height)


# ---------------------------------------------------------------- rows (cached, resumable)

def ocr_rows(run, times, langs, cached_keys=frozenset()):
    """One row per sample time: {"t", "lines": [{"text", "conf", "box"}]} with boxes normalized to the OCR region.

    A killed run keeps the rows it finished (`.part`); the rerun OCRs only the remaining times. Frames whose OCR
    failed are left out of `.part` and the stage file is not finished, so the next run retries them.
    """
    exe = helper_path("ocr")
    region = ocr_region(run)
    plan_hash = hashlib.sha1(",".join(time_key(t) for t in times).encode()).hexdigest()[:12]
    entry = run.cache.entry("ocr", {"plan": plan_hash, "langs": langs, "region": region, "width": OCR_MAX_WIDTH,
                                    "helper": exe.name}, "jsonl")
    if entry.is_hit:
        return sorted(entry.read_jsonl(), key=lambda row: row["t"])
    done = {time_key(row["t"]) for row in entry.read_partial()}
    todo = [float(t) for t in times if time_key(t) not in done]
    if todo:
        usable = supported_langs(exe, langs, run)
        if not usable:
            return []
        previous_of = dict(zip(map(time_key, times[1:]), map(time_key, times[:-1])))
        failed = recognize_times(run, exe, todo, usable, entry, previous_of, cached_keys)
        if failed:
            run.warn(f"OCR could not read {failed} frame(s); rerun the same command to retry them")
            return sorted(entry.read_partial(), key=lambda row: row["t"])
    entry.finish_partial()
    return sorted(entry.read_jsonl(), key=lambda row: row["t"])


def supported_langs(exe, langs, run):
    """The requested languages Vision's accurate level supports; the rest are dropped with a warning."""
    listed = subprocess.run([str(exe), "--list"], capture_output=True, text=True)
    supported = set(json.loads(listed.stdout)) if listed.returncode == 0 and listed.stdout.strip() else set()
    usable = [lang for lang in langs if lang in supported]
    missing = [lang for lang in langs if lang not in supported]
    if missing:
        run.warn(f"OCR languages not supported by Vision and skipped: {', '.join(missing)}")
    if not usable:
        run.warn("OCR skipped: none of --ocr-langs is supported by Vision (for example ko-KR,en-US)")
    return usable


def recognize_times(run, exe, times, langs, entry, previous_of, cached_keys):
    """Exports and OCRs `times` in batches: batch k+1 is exported while the OCR processes read batch k. Returns the
    number of frames that failed.

    Each line also gets `changed`: the share of its box whose pixels changed since the previous sample of the plan
    (None when that sample is not in this run's rows). text_events uses it to refuse fuzzy matches.
    """
    jobs = run.params["content"]["jobs"]
    batch = jobs * BATCH_PER_JOB
    region = ocr_region(run)
    folder = Path(tempfile.mkdtemp(prefix="video-lens-ocr-"))
    pool = OcrPool(exe, langs, jobs)
    last_key, last_grey, failed = None, None, 0
    try:
        with ThreadPoolExecutor(1) as exporter:
            chunks = [times[k:k + batch] for k in range(0, len(times), batch)]
            future = exporter.submit(export_batch, run, chunks[0], region, folder, cached_keys)
            for number, chunk in enumerate(chunks):
                paths, greys = future.result()
                if number + 1 < len(chunks):
                    future = exporter.submit(export_batch, run, chunks[number + 1], region, folder, cached_keys)
                rows = []
                for t, result, grey in zip(chunk, pool.recognize(paths), greys):
                    if "error" in result:
                        run.warn(f"OCR failed on some frames: {result['error']}")
                        failed += 1
                        last_key = None
                        continue
                    before = last_grey if previous_of.get(time_key(t)) == last_key else None
                    rows.append(ocr_row(t, result, grey, before))
                    last_key, last_grey = time_key(t), grey
                entry.append_partial(rows)
                for path in paths:
                    if path.parent == folder:           # frame-cache files stay
                        path.unlink(missing_ok=True)
                run.progress("ocr", min(len(times), (number + 1) * batch), len(times))
    finally:
        pool.close()
        shutil.rmtree(folder, ignore_errors=True)
    return failed


def export_batch(run, times, region, folder, cached_keys):
    """OCR images of the region at min(width, 1920) (only whole-frame exports skip the crop), plus each image as
    half-size grey for the line-change check. Whole-frame keyframe times come from the frame cache, where the
    survey already exported them with the same ffmpeg job."""
    width, height = run.display_size
    roi = None if tuple(region) == (0, 0, width, height) else tuple(region)
    reused = [t for t in times if roi is None and time_key(t) in cached_keys]
    fresh = [t for t in times if t not in reused]
    jobs = [decode.ExportJob(t, folder / f"{time_key(t)}.jpg", width=OCR_MAX_WIDTH, roi=roi) for t in fresh]
    path_of = dict(zip(fresh, decode.export_frames(run.input_path, jobs, run.probe["video"]["index"])))
    path_of.update(zip(reused, decode.cached_frames(run, reused, width=OCR_MAX_WIDTH)))
    paths = [path_of[t] for t in times]
    return paths, [cv2.imread(str(path), cv2.IMREAD_REDUCED_GRAYSCALE_2) for path in paths]


def ocr_row(t, result, grey, previous_grey):
    for line in result["lines"]:
        line["changed"] = None if previous_grey is None else line_change(grey, previous_grey, line["box"])
    return {"t": t, "lines": result["lines"]}


def line_change(grey, previous_grey, box):
    """Share of the line box's pixels whose grey level moved by more than INK_CHANGE_LEVELS between two samples."""
    height, width = grey.shape
    x0, y0 = max(0, int(box[0] * width)), max(0, int(box[1] * height))
    x1, y1 = min(width, math.ceil((box[0] + box[2]) * width)), min(height, math.ceil((box[1] + box[3]) * height))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    moved = cv2.absdiff(grey[y0:y1, x0:x1], previous_grey[y0:y1, x0:x1]) > INK_CHANGE_LEVELS
    return round(float(np.count_nonzero(moved)) / moved.size, 4)


class OcrPool:
    """`jobs` long-lived swift/ocr.swift processes; images are dealt round-robin and each process answers in order.
    A helper that died (or was killed by the watchdog) is replaced at the start of the next batch."""

    def __init__(self, exe, langs, jobs):
        self.command = [str(exe), "--langs", ",".join(langs)]
        self.procs = [self.start() for _ in range(jobs)]

    def start(self):
        return subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def recognize(self, paths):
        """One result dict per path, in order; a helper that dies yields {"error"} for its remaining images."""
        self.procs = [proc if proc.poll() is None else self.restart(proc) for proc in self.procs]
        for number, path in enumerate(paths):
            self.send(self.procs[number % len(self.procs)], f"{path}\n")
        return [self.answer(self.procs[number % len(self.procs)]) for number in range(len(paths))]

    def restart(self, dead):
        close_pipes(dead)
        return self.start()

    @staticmethod
    def answer(proc):
        """The helper's next result line. Vision was seen to stall a helper forever under load (2 of 12 cold runs on
        the orbit sample, 2026-09-27), so a helper silent for HELPER_TIMEOUT_S is killed instead of waited on."""
        watchdog = threading.Timer(HELPER_TIMEOUT_S, proc.kill)
        watchdog.start()
        try:
            line = proc.stdout.readline()
        finally:
            watchdog.cancel()
        if line:
            return json.loads(line)
        return {"error": f"OCR helper stopped answering (exit code {proc.wait()})"}

    @staticmethod
    def send(proc, text):
        """A helper that died has a closed pipe; its answers then read as errors in recognize()."""
        try:
            proc.stdin.write(text)
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def close(self):
        for proc in self.procs:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        for proc in self.procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            close_pipes(proc)


def close_pipes(proc):
    """A finished helper's pipes; a pipe to a dead process may fail to flush on close."""
    for pipe in (proc.stdin, proc.stdout):
        try:
            pipe.close()
        except (BrokenPipeError, OSError):
            pass


def recognize_files(paths, langs, jobs=4):
    """OCR of existing image files (selftest C4); results in `paths` order."""
    pool = OcrPool(helper_path("ocr"), langs, jobs)
    try:
        return pool.recognize([Path(p) for p in paths])
    finally:
        pool.close()


# ---------------------------------------------------------------- text events

def normalize(text):
    """Lower case, punctuation and whitespace stripped (the matching key)."""
    return NON_WORD.sub("", text.casefold())


def match_score(line, event):
    """How well a line continues an open event: 1.0 for equal normalized text, the difflib ratio when it is >= 0.8,
    else 0. A fuzzy (unequal) match is refused when the digits differ ("SLIDE 01" vs "SLIDE 02" has ratio 0.86;
    a changed price is a change, not OCR noise) or when the line's pixels changed since the previous sample
    ("두 번째 장면" vs "세 번째 장면" has ratio 0.8)."""
    a, b = line["norm"], event["norm"]
    if a == b:
        return 1.0
    if NON_DIGIT.sub("", a) != NON_DIGIT.sub("", b):
        return 0.0
    if line["changed"] is not None and line["changed"] >= TEXT_REGION_CHANGED:
        return 0.0
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if matcher.real_quick_ratio() < MATCH_RATIO or matcher.quick_ratio() < MATCH_RATIO:
        return 0.0
    ratio = matcher.ratio()
    return ratio if ratio >= MATCH_RATIO else 0.0


def usable_lines(row, region, frame_size):
    """Lines with conf >= 0.3 and >= 2 normalized chars; boxes renormalized from the OCR region to the display frame,
    px_h in source px."""
    x, y, w, h = region
    width, height = frame_size
    lines = []
    for line in sorted(row["lines"], key=lambda item: (item["box"][1], item["box"][0])):
        norm = normalize(line["text"])
        if line["conf"] < MIN_CONF or len(norm) < MIN_CHARS:
            continue
        bx, by, bw, bh = line["box"]
        lines.append({"text": line["text"].strip(), "norm": norm, "conf": line["conf"], "px_h": round(bh * h),
                      "changed": line.get("changed"),
                      "box": [(bx * w + x) / width, (by * h + y) / height, bw * w / width, bh * h / height]})
    return lines


def text_events(rows, region, frame_size, range_end_s):
    """D2 rule: a line joins the open event it matches best (match_score); unseen open events close, and the sample
    that no longer sees them is their `end_s` (range end for events still open). Returns (events, per-row event
    indices). Each event keeps its highest-confidence variant as the display text."""
    events, open_events, per_row = [], [], []
    for row in rows:
        lines = usable_lines(row, region, frame_size)
        pairs = sorted(((match_score(line, events[e]), n, e)
                        for n, line in enumerate(lines) for e in open_events), reverse=True)
        taken_lines, matched = set(), {}
        for ratio, n, e in pairs:
            if ratio and n not in taken_lines and e not in matched:
                taken_lines.add(n)
                matched[e] = n
        for e, n in matched.items():
            extend_event(events[e], lines[n], row["t"])
        for e in set(open_events) - set(matched):
            events[e]["end_s"] = row["t"]
        new = [start_event(events, lines[n], row["t"], range_end_s) for n in range(len(lines)) if n not in taken_lines]
        open_events = sorted(matched) + new
        per_row.append(open_events)
    return events, per_row


def start_event(events, line, t, range_end_s):
    events.append({"text": line["text"], "norm": line["norm"], "conf": line["conf"], "first_s": t, "last_s": t,
                   "end_s": range_end_s, "samples": 1, "box": line["box"], "px_h": line["px_h"]})
    return len(events) - 1


def extend_event(event, line, t):
    event["last_s"] = t
    event["samples"] += 1
    if line["conf"] > event["conf"]:
        event.update(text=line["text"], norm=line["norm"], conf=line["conf"], box=line["box"], px_h=line["px_h"])


def split_persistent(events, per_row, sample_count):
    """Events present in >= 50 % of at least 3 OCR samples (logos, watermarks) go to the header as `persistent`; the
    rest get their final ids T001... in order. With 1 or 2 samples a slide seen once would count as 50 %."""
    persistent, kept, final_id = [], [], {}
    for index, event in enumerate(events):
        share = event["samples"] / sample_count if sample_count else 0.0
        if sample_count >= PERSISTENT_MIN_SAMPLES and share >= PERSISTENT_SHARE:
            persistent.append({"text": event["text"], "share": round(share, 2), "first_s": content_time(event["first_s"]),
                               "last_s": content_time(event["last_s"])})
            continue
        final_id[index] = f"T{len(kept) + 1:03d}"
        kept.append(event_record(final_id[index], event))
    ids = [[final_id[i] for i in row if i in final_id] for row in per_row]
    return persistent, kept, ids


def event_record(event_id, event):
    return {"id": event_id, "text": event["text"], "conf": event["conf"], "first_s": content_time(event["first_s"]),
            "last_s": content_time(event["last_s"]), "end_s": content_time(event["end_s"]), "samples": event["samples"],
            "box": [round(v, 4) for v in event["box"]], "px_h": event["px_h"]}
