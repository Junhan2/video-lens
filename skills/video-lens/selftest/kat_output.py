"""Output-contract KATs (spec 10.4): image limits (O1), report budget and pointers (O2), cache reuse (O4) and
OCR resume after a kill (O5). O3 (stdout is the report) lives in kat_core.py.

O1 and O2 run their own analyses and then sweep every OUT folder the selftest run wrote, so with a full run they
cover every fixture any KAT analysed.
"""
import json
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

import fixtures_audio as fa
import fixtures_core as fx
import fixtures_motion as fm
from kat_content import synth_talk
from katlib import VL_PY, kat, verdict
from vl import views
from vl.cache import cache_root, file_key

VIEW_LINE = re.compile(r"^(?P<path>/.+) · (?P<w>\d+)x(?P<h>\d+) · (?P<tok>[\d,]+) tok( · (Read this|optional native crop))?$")
POINTER = re.compile(r"\+(\d+) rows: vl\.py rows OUT --kind (\w+) --from (\d+)")
SMALL_BUDGET = 700              # the UI fixture's report is about 1,400 chars, so this forces a pointer
CACHED_STAGE_MAX_S = 0.3        # a cached stage only loads its file
STAGE_TIMES = ("audio", "speech", "survey", "ocr", "motion_detect", "motion_fit")
OCR_SLIDES = 120                # O5: a new text every 0.5 s, so the OCR plan has about 120 samples
OCR_SLIDE_S = 0.5
OCR_FPS = 10
OCR_SIZE = (640, 360)
KILL_WAIT_S = 120
POLL_S = 0.02


def ui_video(ctx):
    return ctx.fixture("motion_ui_cfr.mp4", fm.build_ui)


def talk_with_audio(ctx):
    """The audio builder's synth_talk with speech (mode both); None without the Yuna voice."""
    return ctx.fixture("audio_talk.mp4", fa.build_talk) if fa.has_voice(fa.TALK_VOICE) else None


def analyze(ctx, video, name, *flags):
    """(OUT, CompletedProcess) of `vl.py analyze VIDEO --out OUT FLAGS`; raises on a non-zero exit."""
    out = ctx.out_dir(name)
    result = ctx.vl("analyze", video, "--out", out, *flags)
    if result.returncode != 0:
        raise AssertionError(f"{name}: analyze exit {result.returncode}: {result.stderr.strip()}")
    return out, result


def image_failures(path):
    """Limit breaches of one image file on disk (spec 5.4)."""
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None:
        return [f"{path}: unreadable"]
    height, width = image.shape[:2]
    tokens = views.visual_tokens(width, height)
    if max(width, height) > views.MAX_SIDE_PX or tokens > views.MAX_VISUAL_TOKENS:
        return [f"{path.name}: {width}x{height} = {tokens} tok breaks the limit"]
    return []


def record_failures(path, width, height, tokens):
    """A recorded view (analysis.json or a printed view line) must describe the written pixels."""
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None:
        return [f"{path}: recorded but unreadable"]
    actual = (image.shape[1], image.shape[0])
    if actual != (width, height) or tokens != views.visual_tokens(*actual):
        return [f"{path.name}: recorded {width}x{height} {tokens} tok, file {actual[0]}x{actual[1]}"]
    return []


def view_line_failures(result):
    failures = [] if result.returncode == 0 else [f"view command exit {result.returncode}: {result.stderr.strip()}"]
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    for line in lines:
        match = VIEW_LINE.match(line)
        if not match:
            failures.append(f"not a view line: {line!r}")
            continue
        failures += record_failures(Path(match["path"]), int(match["w"]), int(match["h"]), int(match["tok"].replace(",", "")))
    return failures, len(lines)


def sweep_outputs(ctx):
    """(images checked, OUT folders with analysis.json, failures) over everything this selftest run wrote."""
    root = ctx.work / "out"
    images = sorted(p for p in root.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    failures = [f for path in images for f in image_failures(path)]
    analyses = sorted(root.rglob("analysis.json"))
    for analysis_path in analyses:
        out = analysis_path.parent
        for view in json.loads(analysis_path.read_text())["views"]:
            failures += record_failures(out / view["file"], view["w"], view["h"], view["visual_tokens"])
    return len(images), len(analyses), failures


@kat("O1", "output", quick=True)
def o1_image_limits(ctx):
    """Every image <= 1932 px per side and <= 4,761 tokens; recorded w/h/tokens equal the file."""
    ui_out, _ = analyze(ctx, ui_video(ctx), "O1-ui")
    synth_out, _ = analyze(ctx, synth_talk(ctx), "O1-synth", "--mode", "content")
    talk = talk_with_audio(ctx)
    if talk:
        analyze(ctx, talk, "O1-talk")
    failures, printed = [], 0
    for argv in (("zoom", ui_out, "--event", "M02"), ("zoom", synth_out, "--segment", "S02"),
                 ("zoom", ui_out, "--range", "0.5:1.5", "--cells", "48"), ("frame", synth_out, "--t", "6.5,12.0")):
        found, count = view_line_failures(ctx.vl(*argv))
        failures += found
        printed += count
    images, folders, swept = sweep_outputs(ctx)
    failures += swept
    return verdict(failures, f"{images} images in {folders} analysed OUT folders plus {printed} zoom/frame views: all "
                             f"<= {views.MAX_SIDE_PX} px and <= {views.MAX_VISUAL_TOKENS:,} tok, records match the files")


def report_budget_failures(ctx):
    """Every report.md this run wrote is within its own --budget-chars."""
    failures, count = [], 0
    for report in sorted((ctx.work / "out").rglob("report.md")):
        budget = json.loads((report.parent / "analysis.json").read_text())["params"]["report"]["budget_chars"]
        length = len(report.read_text())
        count += 1
        if length > budget:
            failures.append(f"{report.parent.name}/report.md {length} chars > {budget}")
    return failures, count


@kat("O2", "output", quick=True)
def o2_report_budget(ctx):
    """report.md <= --budget-chars on every fixture; an overflow ends in a `rows` pointer that prints the rest."""
    out, result = analyze(ctx, ui_video(ctx), "O2-small", "--budget-chars", str(SMALL_BUDGET))
    failures = []
    if len(result.stdout) > SMALL_BUDGET:
        failures.append(f"report {len(result.stdout)} chars > {SMALL_BUDGET}")
    pointer = POINTER.search(result.stdout)
    if not pointer:
        failures.append("no rows pointer in the cut report")
    else:
        hidden, kind, first = int(pointer[1]), pointer[2], int(pointer[3])
        shown_ids = set(re.findall(r"^\| (M\d+\.\d+) \|", result.stdout, re.M))
        rest = ctx.vl("rows", out, "--kind", kind, "--from", first, "--count", hidden)
        rest_ids = re.findall(r"^\| (M\d+\.\d+) \|", rest.stdout, re.M)
        all_ids = {e["id"] for ev in json.loads((out / "analysis.json").read_text())["motion"]["events"] for e in ev["elements"]}
        if rest.returncode != 0 or len(rest_ids) != hidden or shown_ids | set(rest_ids) != all_ids:
            failures.append(f"pointer {pointer[0]!r} gave {rest_ids} (rc {rest.returncode}); shown {sorted(shown_ids)}, "
                            f"all {sorted(all_ids)}")
    budget_failures, reports = report_budget_failures(ctx)
    failures += budget_failures
    detail = pointer[0] if pointer else ""
    return verdict(failures, f"cut report {len(result.stdout)}/{SMALL_BUDGET} chars ends in '{detail}', which prints "
                             f"the hidden rows; {reports} report.md files within their budget")


@kat("O4", "output", quick=True)
def o4_second_run_cached(ctx):
    """The second analyze of a file decodes nothing: cache.misses empty and the stage timings about 0."""
    video = talk_with_audio(ctx) or synth_talk(ctx)
    first_out, _ = analyze(ctx, video, "O4-first", "--mode", "both")
    second_out, _ = analyze(ctx, video, "O4-second", "--mode", "both")
    first = json.loads((first_out / "analysis.json").read_text())
    second = json.loads((second_out / "analysis.json").read_text())
    failures = []
    if second["cache"]["misses"]:
        failures.append(f"second run misses {second['cache']['misses']}")
    decoding = {"survey", "motion"} | ({"audio"} if second["audio"] else set())
    if not decoding <= set(second["cache"]["hits"]):
        failures.append(f"second run reused {second['cache']['hits']}, expected at least {sorted(decoding)}")
    slow = {k: second["timing_s"][k] for k in STAGE_TIMES if second["timing_s"][k] > CACHED_STAGE_MAX_S}
    if slow:
        failures.append(f"cached stages took {slow}")
    if (first_out / "report.md").read_text().split("\n", 4)[4] != (second_out / "report.md").read_text().split("\n", 4)[4]:
        failures.append("the cached report differs from the first one below the header")
    stages = ", ".join(sorted(second["cache"]["hits"]))
    timings = ", ".join(f"{k} {second['timing_s'][k]:.2f}" for k in ("survey", "ocr", "motion_detect", "motion_fit"))
    return verdict(failures, f"{video.name}: second run 0 misses, reused {stages}; {timings} s, total "
                             f"{second['timing_s']['total']:.1f} s (first run {first['timing_s']['total']:.1f} s, "
                             f"misses {first['cache']['misses'] or 'none'})")


def build_ocr_slides(path):
    """O5 fixture: 60 s of 640x360 slides at 10 fps, a new 'SLIDE nnn' every 0.5 s."""
    width, height = OCR_SIZE
    frames = []
    for number in range(OCR_SLIDES):
        slide = np.full((height, width, 3), 245, np.uint8)
        cv2.putText(slide, f"SLIDE {number + 1:03d}", (60, 200), cv2.FONT_HERSHEY_DUPLEX, 2.4, (30, 30, 30), 4)
        frames += [slide] * int(OCR_SLIDE_S * OCR_FPS)
    fx.encode_bgr(frames, path, OCR_SIZE, OCR_FPS)


def ocr_part_rows(video):
    parts = list((cache_root() / file_key(video)).glob("ocr.*.part"))
    return sum(1 for line in parts[0].read_text().splitlines() if line.strip()) if parts else 0


def kill_during_ocr(video, out):
    """Starts analyze and SIGKILLs it once OCR has finished at least one batch but not all; returns the status."""
    process = subprocess.Popen([sys.executable, str(VL_PY), "analyze", str(video), "--out", str(out), "--mode", "content",
                                "--jobs", "1", "--refresh"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + KILL_WAIT_S
    status = None
    try:
        while process.poll() is None and time.monotonic() < deadline:
            try:
                status = json.loads((out / "status.json").read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                status = None
            if status and status["stage"] == "ocr" and 0 < status["done"] < status["total"]:
                process.send_signal(signal.SIGKILL)
                break
            time.sleep(POLL_S)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
    return process.returncode, status


@kat("O5", "output")
def o5_ocr_resume(ctx):
    """analyze killed mid-OCR, then rerun: it finishes and OCRs only the samples the killed run had not done."""
    video = ctx.fixture("output_ocr_slides.mp4", build_ocr_slides)
    out = ctx.out_dir("O5")
    code, status = kill_during_ocr(video, out)
    if code != -signal.SIGKILL:
        return verdict([f"analyze was not killed mid-OCR (exit {code}, last status {status})"], "")
    done_before = ocr_part_rows(video)
    rerun = ctx.vl("analyze", video, "--out", out, "--mode", "content", "--jobs", "1", "--progress")
    failures = [] if rerun.returncode == 0 else [f"rerun exit {rerun.returncode}: {rerun.stderr.strip()[-300:]}"]
    if failures:
        return verdict(failures, "")
    analysis = json.loads((out / "analysis.json").read_text())
    samples = analysis["content"]["ocr"]["samples"]
    totals = [int(t) for t in re.findall(r"^video-lens: ocr \d+/(\d+)", rerun.stderr, re.M)]
    texts = {e["text"] for e in analysis["content"]["text"]["events"]}
    found = sum(f"SLIDE {n + 1:03d}" in texts for n in range(OCR_SLIDES))
    if done_before == 0:
        failures.append("the killed run left no OCR rows in the cache")
    if not totals or totals[0] != samples - done_before:
        failures.append(f"rerun OCR'd {totals[:1]} of {samples} samples; {done_before} were done before the kill")
    if "ocr" not in analysis["cache"]["misses"]:
        failures.append("the resumed OCR stage was not recorded as computed")
    if found < OCR_SLIDES * 0.95:
        failures.append(f"only {found}/{OCR_SLIDES} slide labels read after the resume")
    return verdict(failures, f"killed at OCR {status['done']}/{status['total']} ({done_before} rows kept); rerun OCR'd the "
                             f"other {totals[0] if totals else '?'} of {samples} samples and read {found}/{OCR_SLIDES} labels")
