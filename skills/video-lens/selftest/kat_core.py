"""Core KATs: timeline, decode, export, probe, cache, CLI contract and image limits (spec 10.2, 10.4 O3).

C5-C9 and O3 are the spec rows; X1-X6 cover core behaviour no spec row reaches (exit codes, cache, swift build,
view limits, one video per OUT, URL download retry).
"""
import importlib.util
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

import fixtures_core as fx
from katlib import SKIP, kat, verdict
from vl import decode, views
from vl.cache import Cache, download_url, enforce_lru, file_key, purge_older
from vl.cli import prepare_run
from vl.errors import EXIT_DEPENDENCY, EXIT_INPUT, VlError
from vl.probe import probe
from vl.swiftbuild import build_swift

ORBIT = Path(os.environ.get("VIDEO_LENS_REAL_SAMPLE", "/nonexistent"))   # a real 3D carousel recording; KATs skip without it
PTS_TOLERANCE_S = 0.0001
THUMB_VF = "scale=160:-2:flags=area"
DETECT_VF = "scale=w=640:h=640:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=area"
EXPORT_WIDTH = 320
EXPORT_SAMPLES = 8
SAME_FRAME_MAX_DIFF = 0.1       # mean |diff| (0-255); exports measured bit-exact (0.0), neighbours 0.5+
MOVING_MIN_DIFF = 0.1           # thumbnail mean |diff| to both neighbours for a frame to count as moving
O3_MODULES = ("detect", "sheets", "report")


def motion_cfr(ctx):
    return ctx.fixture("core_motion_cfr.mp4", fx.build_motion_cfr)


def motion_vfr(ctx):
    return ctx.fixture("core_motion_vfr.mp4", fx.build_motion_vfr(motion_cfr(ctx)))


def korean_named(ctx):
    return ctx.fixture(fx.KOREAN_NAME, fx.build_copy(motion_cfr(ctx)))


def thumbnails(path, **options):
    """(times, grey 160 px thumbnails) from one showinfo-timed decode."""
    frames = [(f.t, f.image.astype(np.int16)) for f in decode.FrameStream(path, THUMB_VF, pix_fmt="gray", **options)]
    return [t for t, _ in frames], [image for _, image in frames]


def cv2_times(path):
    capture = cv2.VideoCapture(str(path))
    times = []
    while capture.grab():
        times.append(capture.get(cv2.CAP_PROP_POS_MSEC) / 1000)
    capture.release()
    return times


def time_mismatch(label, actual, expected):
    """None when both lists have the same length and agree within 0.1 ms, else a failure message."""
    if len(actual) != len(expected):
        return f"{label}: {len(actual)} frames vs {len(expected)} expected"
    worst = max((abs(a - b) for a, b in zip(actual, expected)), default=0.0)
    return f"{label}: max |dt| {worst * 1000:.3f} ms > 0.1 ms" if worst > PTS_TOLERANCE_S else None


def mean_diff(a, b):
    return float(np.mean(cv2.absdiff(np.asarray(a), np.asarray(b))))


def moving_indices(thumbs, count):
    """Evenly spread frames that differ from both neighbours, so a wrong frame would be detectable."""
    candidates = [i for i in range(1, len(thumbs) - 1)
                  if min(mean_diff(thumbs[i], thumbs[i - 1]), mean_diff(thumbs[i], thumbs[i + 1])) > MOVING_MIN_DIFF]
    if len(candidates) <= count:
        return candidates
    return [candidates[round(k * (len(candidates) - 1) / (count - 1))] for k in range(count)]


def is_intended(image, frames, index):
    """The exported image equals frame `index` and is closer to it than to either neighbour."""
    diffs = {i: mean_diff(image, frames[i]) for i in (index - 1, index, index + 1) if i in frames}
    return diffs[index] <= SAME_FRAME_MAX_DIFF and min(diffs, key=diffs.get) == index


def export_hits(path, times, indices, width=None, stream_index=None):
    """How many `-ss` exports at times[i] return exactly the frame the stream decoded at i."""
    wanted = {j for i in indices for j in (i - 1, i, i + 1)}
    scale = f"scale='min({width},iw)':-2:flags=area" if width else ""
    frames = {f.index: f.image.copy() for f in decode.FrameStream(path, scale, stream_index=stream_index) if f.index in wanted}
    exported = decode.read_frames(path, [times[i] for i in indices], width=width, stream_index=stream_index)
    return sum(is_intended(image, frames, i) for image, i in zip(exported, indices))


def stream_images(path, vf=""):
    return [(f.t, f.image.copy()) for f in decode.FrameStream(path, vf)]


def written_run(ctx, video, name, *flags):
    """OUT folder holding the analysis.json that analyze writes before its stages (for view commands)."""
    out = ctx.out_dir(name)
    run = prepare_run(["analyze", str(video), "--out", str(out), *flags])
    run.write_analysis()
    return run


def one_error_line(result):
    lines = result.stderr.splitlines()
    return result.stdout == "" and len(lines) == 1 and lines[0].startswith("video-lens: ")


@kat("C5", "io", quick=True)
def c5_showinfo_matches_cv2(ctx):
    """showinfo pts = cv2 pts within 0.1 ms; `-ss (t - 0.0005)` export returns the intended frame 8/8."""
    failures, notes = [], []
    sources = [("vfr-fixture", motion_vfr(ctx))] + ([("orbit", ORBIT)] if ORBIT.is_file() else [])
    for label, path in sources:
        times, thumbs = thumbnails(path)
        seen = cv2_times(path)
        mismatch = time_mismatch(f"{label} showinfo vs cv2", times, seen)
        failures += [mismatch] if mismatch else []
        info, _ = probe(path)
        if not info["video"]["is_vfr"]:
            failures.append(f"{label}: expected a VFR file")
        indices = moving_indices(thumbs, EXPORT_SAMPLES)
        hits = export_hits(path, times, indices, width=EXPORT_WIDTH)
        if len(indices) < EXPORT_SAMPLES or hits < len(indices):
            failures.append(f"{label}: export {hits}/{len(indices)} (need {EXPORT_SAMPLES}/{EXPORT_SAMPLES})")
        worst = max(abs(a - b) for a, b in zip(times, seen)) * 1000 if len(times) == len(seen) else float("nan")
        notes.append(f"{label} {len(times)} fr VFR max|dt| {worst:.3f} ms, export {hits}/{len(indices)}")
    return verdict(failures, "; ".join(notes))


@kat("C6", "io")
def c6_offsets(ctx):
    """start_time 5 s and a 22 ms video offset: exports at reported t equal the unshifted frames."""
    base = motion_cfr(ctx)
    shifted = ctx.fixture("core_ts5.mp4", fx.build_ts_shifted(base))
    delayed = ctx.fixture("core_video22.mp4", fx.build_video_offset(base))
    failures = []
    base_frames = stream_images(base)
    base_times = [t for t, _ in base_frames]
    shifted_info, _ = probe(shifted)
    if abs(shifted_info["start_time_s"] - fx.TS_OFFSET_S) > 1e-6:
        failures.append(f"shifted start_time {shifted_info['start_time_s']} != 5")
    shifted_times, _ = thumbnails(shifted)
    mismatch = time_mismatch("ts+5 times vs base", shifted_times, base_times)
    failures += [mismatch] if mismatch else []
    base_thumbs = thumbnails(base)[1]
    indices = moving_indices(base_thumbs, EXPORT_SAMPLES)
    exported = decode.read_frames(shifted, [shifted_times[i] for i in indices])
    frames = dict(enumerate(image for _, image in base_frames))
    hits = sum(is_intended(image, frames, i) for image, i in zip(exported, indices))
    if hits < len(indices) or len(indices) < EXPORT_SAMPLES:
        failures.append(f"ts+5 export equals base frame {hits}/{len(indices)}")
    cli_hits = frame_command_hits(ctx, shifted, shifted_times, indices[:3], frames)
    if cli_hits < 3:
        failures.append(f"`vl.py frame` on ts+5 (exact and mid-frame t) {cli_hits}/3")
    offset = probe(delayed)[0]["video"]["video_start_offset_s"]
    if abs(offset - fx.VIDEO_OFFSET_S) > 0.001:
        failures.append(f"video_start_offset_s {offset} != 0.022 +- 0.001")
    delayed_times, _ = thumbnails(delayed)
    first = decode.read_frame(delayed, delayed_times[0])
    if abs(delayed_times[0] - fx.VIDEO_OFFSET_S) > 0.001 or mean_diff(first, frames[0]) > SAME_FRAME_MAX_DIFF:
        failures.append(f"22 ms clip: first frame t {delayed_times[0]:.4f} or its export differs from base frame 0")
    return verdict(failures, f"ts+5: {len(shifted_times)} times = base, export {hits}/{len(indices)}, "
                             f"frame cmd {cli_hits}/3; video offset {offset:.6f} s, first frame t {delayed_times[0]:.4f}")


def frame_command_hits(ctx, video, times, indices, base_frames):
    """`vl.py frame` at t and at t + 0.4 frame (still frame i on screen) both return base frame i."""
    run = written_run(ctx, video, "C6-frame")
    step = run.frame_interval_s
    hits = 0
    for i in indices:
        asked = times[i] + (0.4 * step if i % 2 else 0.0)
        result = ctx.vl("frame", run.out, "--t", f"{asked:.6f}")
        path = Path(result.stdout.split(" · ")[0]) if result.returncode == 0 else None
        image = cv2.imread(str(path)) if path and path.is_file() else None
        hits += bool(image is not None and f"f_{times[i]:.4f}_full" in path.name and is_intended(image, base_frames, i))
    return hits


@kat("C7", "timeline", quick=True)
def c7_range_and_window_times(ctx):
    """--start/--end decodes and select-window decodes land on the full decode's pts within 0.1 ms."""
    failures, notes = [], []
    for label, video in (("cfr", motion_cfr(ctx)), ("vfr", motion_vfr(ctx))):
        full = [f.t for f in decode.FrameStream(video, DETECT_VF)]
        run = prepare_run(["analyze", str(video), "--out", str(ctx.out_dir(f"C7-{label}")), "--start", "1.0", "--end", "2.5"])
        ranged = [f.t for f in decode.stream_run(run, DETECT_VF)]
        expected = [t for t in full if 1.0 <= t < 2.5]
        failures += [m for m in [time_mismatch(f"{label} range 1.0-2.5", ranged, expected)] if m]
        windows = [(full[k], full[k + 12]) for k in (len(full) // 8, len(full) // 2)]
        crop = "crop=240:136:100:60,scale=320:-2:flags=area"
        stream = decode.FrameStream(video, crop, windows=windows)
        picked = [(f.t, f.window) for f in stream]
        wanted = [(t, n) for t in full for n, (a, b) in enumerate(windows) if a <= t <= b]
        failures += [m for m in [time_mismatch(f"{label} windows", [t for t, _ in picked], [t for t, _ in wanted])] if m]
        if [n for _, n in picked] != [n for _, n in wanted]:
            failures.append(f"{label} window indices differ")
        inside = [(a, b) for a, b in windows if a >= 1.0 and b < 2.5]
        if not inside:
            failures.append(f"{label}: no window inside 1.0-2.5 to test -ss with")
        ranged_windows = [f.t for f in decode.stream_run(run, crop, windows=inside)]
        expected_inside = [t for t in full for a, b in inside if a <= t <= b]
        failures += [m for m in [time_mismatch(f"{label} windows after -ss 1.0", ranged_windows, expected_inside)] if m]
        whole = prepare_run(["analyze", str(video), "--out", str(ctx.out_dir(f"C7-{label}-full"))])
        whole_times = [f.t for f in decode.stream_run(whole, DETECT_VF)]
        if whole_times != full or whole.analysis["video"]["pts_count_check"] != "ok":
            failures.append(f"{label}: full-range stream_run count check {whole.analysis['video']['pts_count_check']}")
        notes.append(f"{label}: range {len(ranged)}/{len(expected)}, windows {len(picked)}/{len(wanted)}, "
                     f"windows+ss {len(ranged_windows)}/{len(expected_inside)}")
    return verdict(failures, "; ".join(notes))


@kat("C8", "io")
def c8_rotation(ctx):
    """90 degree display matrix: display size reported, decoded, exported and used for --roi."""
    video = ctx.fixture("core_rot90.mp4", fx.build_rotated(motion_cfr(ctx)))
    width, height = fx.MOTION_SIZE
    info, _ = probe(video)
    failures = []
    shown = (info["video"]["display_width"], info["video"]["display_height"])
    if shown != (height, width) or info["video"]["rotation"] % 180 != 90:
        failures.append(f"display {shown}, rotation {info['video']['rotation']}")
    first = next(iter(decode.FrameStream(video)))
    if first.image.shape[:2] != (width, height):
        failures.append(f"decoded shape {first.image.shape[:2]}")
    exported = decode.read_frame(video, first.t)
    if exported.shape[:2] != (width, height):
        failures.append(f"exported shape {exported.shape[:2]}")
    run = written_run(ctx, video, "C8")
    inside = ctx.vl("frame", run.out, "--t", "0.5", "--roi", f"0,{width - 100},{height},100")
    outside = ctx.vl("frame", run.out, "--t", "0.5", "--roi", f"0,0,{width},{height}")
    if inside.returncode != 0 or f"{height}x100" not in inside.stdout:
        failures.append(f"roi inside display frame: rc {inside.returncode} {inside.stdout.strip()} {inside.stderr.strip()}")
    if outside.returncode != 2:
        failures.append(f"landscape roi on portrait display: rc {outside.returncode}")
    return verdict(failures, f"display {shown[0]}x{shown[1]} (coded {width}x{height}), decode/export/roi use it")


@kat("C9", "io")
def c9_korean_file_name(ctx):
    """Korean file name with spaces: probe, cache and frame commands run and print the right paths.
    The report half of C9 runs inside O3, which analyses this same file."""
    video = korean_named(ctx)
    failures = []
    probed = ctx.vl("probe", video)
    if probed.returncode != 0 or probed.stderr or json.loads(probed.stdout)["input"]["path"] != str(video):
        failures.append(f"probe rc {probed.returncode} stderr {probed.stderr.strip()!r}")
    located = ctx.vl("cache", "--path", video)
    if located.returncode != 0 or not located.stdout.strip().endswith(file_key(video)):
        failures.append(f"cache --path rc {located.returncode}")
    frames = sum(1 for _ in decode.FrameStream(video, THUMB_VF, pix_fmt="gray"))
    if frames != probe(video)[0]["video"]["frames"]:
        failures.append(f"decoded {frames} frames")
    run = written_run(ctx, video, "C9 한글 출력")
    framed = ctx.vl("frame", run.out, "--t", "1.0")
    written = Path(framed.stdout.split(" · ")[0]) if framed.returncode == 0 else None
    if not written or not written.is_file() or run.out not in written.parents:
        failures.append(f"frame rc {framed.returncode} {framed.stdout.strip()} {framed.stderr.strip()}")
    return verdict(failures, f"probe/cache/decode/frame ok on '{video.name}' ({frames} frames, OUT '{run.out.name}')")


@kat("O3", "output", quick=True)
def o3_stdout_is_report(ctx):
    """analyze stdout is exactly report.md (with --json, analysis.json); stderr empty; on a Korean file name."""
    missing = [m for m in O3_MODULES if importlib.util.find_spec(f"vl.{m}") is None]
    if missing:
        return SKIP, "needs " + ", ".join(f"vl/{m}.py" for m in missing) + " (motion builder, integrator)"
    video = korean_named(ctx)
    out = ctx.out_dir("O3")
    report = ctx.vl("analyze", video, "--out", out)
    written = (out / "report.md").read_text() if (out / "report.md").is_file() else None
    as_json = ctx.vl("analyze", video, "--out", out, "--json")
    failures = []
    if report.returncode != 0 or report.stderr:
        failures.append(f"analyze rc {report.returncode} stderr {report.stderr.strip()!r}")
    elif report.stdout != written:
        failures.append("stdout differs from report.md")
    elif fx.KOREAN_NAME not in report.stdout:
        failures.append("report does not name the Korean file")
    if as_json.returncode != 0 or as_json.stderr:
        failures.append(f"--json rc {as_json.returncode} stderr {as_json.stderr.strip()!r}")
    elif json.loads(as_json.stdout) != json.loads((out / "analysis.json").read_text()):
        failures.append("--json stdout differs from analysis.json")
    return verdict(failures, f"report {len(report.stdout)} chars = report.md; --json = analysis.json; stderr empty")


@kat("X1", "cli", quick=True)
def x1_exit_codes(ctx):
    """Spec 5.3: exit 2/3/4 with one `video-lens:` stderr line and empty stdout; probe prints JSON only."""
    video = motion_cfr(ctx)
    audio_only = ctx.fixture("core_audio_only.wav", fx.build_audio_only)
    no_analysis = ctx.out_dir("X1-empty")
    cases = [("missing input", 3, ["analyze", ctx.work / "absent.mp4"]),
             ("audio only", 3, ["analyze", audio_only]),
             ("unknown flag", 2, ["analyze", video, "--bogus"]),
             ("end before start", 2, ["analyze", video, "--start", "2", "--end", "1"]),
             ("bad time", 2, ["analyze", video, "--start", "1:xx"]),
             ("start after end of video", 4, ["analyze", video, "--start", "9"]),
             ("roi outside frame", 2, ["analyze", video, "--roi", "0,0,4000,10"]),
             ("frame without analysis", 3, ["frame", no_analysis, "--t", "1"])]
    failures = []
    for label, code, argv in cases:
        result = ctx.vl(*argv)
        if result.returncode != code or not one_error_line(result):
            failures.append(f"{label}: rc {result.returncode} (want {code}), stderr {result.stderr.strip()!r}")
    probed = ctx.vl("probe", video)
    try:
        is_json = json.loads(probed.stdout)["video"]["frames"] > 0
    except (json.JSONDecodeError, KeyError):
        is_json = False
    if probed.returncode != 0 or probed.stderr or not is_json:
        failures.append(f"probe rc {probed.returncode}, stderr {probed.stderr!r}")
    return verdict(failures, f"{len(cases)} error cases give the right code and one line; probe prints JSON only")


@kat("X5", "cli", quick=True)
def x5_out_belongs_to_one_video(ctx):
    """Parallel sessions sharing one --out never mix: another video is refused (exit 2), the same video reruns,
    and a folder analysed before OUT claims existed is refused without being claimed."""
    video, other = motion_cfr(ctx), motion_vfr(ctx)
    shared = ctx.out_dir("X5-shared")
    failures = []
    runs = [("first video", video, 0), ("other video", other, 2), ("first video again", video, 0)]
    for label, path, code in runs:
        result = ctx.vl("analyze", path, "--out", shared, "--mode", "motion", "--sheets", "0")
        if result.returncode != code or (code and not one_error_line(result)):
            failures.append(f"{label}: rc {result.returncode} (want {code}), stderr {result.stderr.strip()!r}")
    legacy = ctx.work / "out" / "X5-legacy"
    shutil.copytree(shared, legacy, dirs_exist_ok=True)
    (legacy / ".vl-input.json").unlink()
    refused = ctx.vl("analyze", other, "--out", legacy, "--mode", "motion", "--sheets", "0")
    if refused.returncode != 2 or (legacy / ".vl-input.json").exists():
        failures.append(f"legacy folder: rc {refused.returncode}, claimed {(legacy / '.vl-input.json').exists()}")
    return verdict(failures, "another video refused with exit 2, same video reruns, legacy folder refused unclaimed")


@kat("X2", "cache", quick=True)
def x2_cache(ctx):
    """Key survives copy and rename; stage hit/miss by param hash; resumable .part; probe reuse; LRU and purge."""
    video = motion_cfr(ctx)
    failures = []
    copy = ctx.work / "renamed copy.mp4"
    shutil.copyfile(video, copy)
    if file_key(copy) != file_key(video) or file_key(copy) == file_key(motion_vfr(ctx)):
        failures.append("key does not survive copy, or two files share one")
    failures += stage_entry_failures(ctx.work / "cache-stage")
    run = prepare_run(["analyze", str(copy), "--out", str(ctx.out_dir("X2"))])
    again = prepare_run(["analyze", str(copy), "--out", str(ctx.out_dir("X2"))])
    if again.cache.load_probe(versions_of(run)) is None or again.probe["path"] != str(copy):
        failures.append("probe not reused from meta.json")
    first = decode.cached_frames(run, run.packet_times[:3], width=200)
    stamps = [p.stat().st_mtime_ns for p in first]
    again_paths = decode.cached_frames(again, run.packet_times[:3], width=200)
    if again_paths != first or [p.stat().st_mtime_ns for p in again_paths] != stamps or cv2.imread(str(first[0])).shape[1] != 200:
        failures.append("cached_frames did not reuse frames/<t_ms>_<w>.jpg")
    failures += lru_failures(ctx.work / "cache-lru")
    return verdict(failures, "key, stage entries, .part resume, probe reuse, version wipe, LRU cap and purge behave")


def versions_of(run):
    return run.cache.read_meta()["versions"]


def stage_entry_failures(root):
    failures = []
    cache = Cache("00000000000000aa", root=root)
    first = cache.entry("survey", {"pix": 20}, "json")
    first.save_json({"ok": 1})
    second = Cache("00000000000000aa", root=root).entry("survey", {"pix": 20}, "json")
    other = Cache("00000000000000aa", root=root).entry("survey", {"pix": 21}, "json")
    fresh = Cache("00000000000000aa", root=root, is_refresh=True).entry("survey", {"pix": 20}, "json")
    if first.is_hit or not second.is_hit or other.is_hit or fresh.is_hit or cache.misses != ["survey"]:
        failures.append("stage entry hit/miss by params or --refresh is wrong")
    if second.load_json() != {"ok": 1}:
        failures.append("stage json round trip")
    ocr = cache.entry("ocr", {"n": 1}, "jsonl")
    ocr.append_partial([{"i": 1}, {"i": 2}])
    with open(ocr.partial_path, "a") as torn:
        torn.write('{"i": 3')
    resumed = Cache("00000000000000aa", root=root).entry("ocr", {"n": 1}, "jsonl")
    if resumed.is_hit or resumed.read_partial() != [{"i": 1}, {"i": 2}]:
        failures.append("partial resume did not return the complete rows")
    resumed.finish_partial()
    done = Cache("00000000000000aa", root=root).entry("ocr", {"n": 1}, "jsonl")
    if not done.is_hit or done.read_jsonl() != [{"i": 1}, {"i": 2}] or resumed.partial_path.exists():
        failures.append("finish_partial did not promote .part")
    cache.write_meta({"versions": {"tool": "old"}})
    cache.save_probe({"path": "x", "size_bytes": 1}, np.zeros(2), {"tool": "new"})
    if any(p.name.startswith(("survey.", "ocr.")) for p in cache.dir.iterdir()):
        failures.append("stage files survived a tool version change")
    return failures


def lru_failures(root):
    failures = []
    now = time.time()
    sizes = {"aaaaaaaaaaaaaaa1": 3, "aaaaaaaaaaaaaaa2": 2, "aaaaaaaaaaaaaaa3": 1}
    for age, (key, megabytes) in enumerate(sizes.items()):
        folder = root / key
        folder.mkdir(parents=True)
        (folder / "blob").write_bytes(b"\0" * megabytes * 1_000_000)
        (folder / "meta.json").write_text("{}")
        os.utime(folder / "meta.json", (now - 1000 * (3 - age), now - 1000 * (3 - age)))
    (root / "downloads").mkdir()
    (root / "downloads" / "0123456789abcdef.mp4").write_bytes(b"\0" * 500_000)
    os.utime(root / "downloads" / "0123456789abcdef.mp4", (now - 5000, now - 5000))
    oldest = root / "aaaaaaaaaaaaaaa1"
    with Cache("aaaaaaaaaaaaaaa2", root=root).locked():
        removed = enforce_lru(root, keep=[oldest], cap_bytes=4_200_000)
    names = sorted(p.name for p in removed)
    if names != ["0123456789abcdef.mp4", "aaaaaaaaaaaaaaa3"]:
        failures.append(f"LRU removed {names} (want the old download and the unlocked, unkept aaa3)")
    os.utime(oldest / "meta.json", (now - 40 * 86400, now - 40 * 86400))
    purged = purge_older(root, 30)
    if [p.name for p in purged] != ["aaaaaaaaaaaaaaa1"]:
        failures.append(f"purge-older removed {[p.name for p in purged]}")
    return failures


FAKE_YTDLP = """#!/usr/bin/env python3
import pathlib, sys
args = sys.argv[1:]
stem = args[args.index("-o") + 1].replace(".%(ext)s", "")
with open(pathlib.Path(__file__).with_name("calls.log"), "a") as log:
    log.write(" ".join(args[:-1]) + "\\n")
pathlib.Path(stem + ".info.json").write_text("{}")
if "youtube:player_client=web_embedded" in args and args[-1].endswith("/ok"):
    pathlib.Path(stem + ".mp4").write_bytes(b"video")
    sys.exit(0)
pathlib.Path(stem + ".f137.mp4.part").write_bytes(b"part")
pathlib.Path(stem + ".f140.m4a").write_bytes(b"audio")
sys.exit("ERROR: unable to download video data: HTTP Error 403: Forbidden")
"""


@kat("X6", "cache", quick=True)
def x6_download_retry(ctx):
    """A URL download refused mid-way (YouTube's 403 on the default clients) is retried once with the web_embedded
    client and keeps its info JSON; one that fails twice leaves no file behind and names the hand download."""
    fake_bin, root = ctx.work / "fake-ytdlp", ctx.work / "cache-download"
    fake_bin.mkdir(parents=True, exist_ok=True)
    (fake_bin / "yt-dlp").write_text(FAKE_YTDLP)
    (fake_bin / "yt-dlp").chmod(0o755)
    path = os.environ["PATH"]
    os.environ["PATH"] = f"{fake_bin}{os.pathsep}{path}"
    failures = []
    try:
        video = download_url("https://example.com/ok", root=root)
        names = sorted(p.name for p in (root / "downloads").iterdir())
        if video.suffix != ".mp4" or names != sorted([video.name, video.with_suffix(".info.json").name]):
            failures.append(f"retried download left {names}")
        try:
            download_url("https://example.com/fail", root=root)
            failures.append("a download that failed twice returned a file")
        except VlError as error:
            left = sorted(p.name for p in (root / "downloads").iterdir() if not p.name.startswith(video.stem))
            if error.code != EXIT_INPUT or "403" not in error.what or "--write-info-json" not in error.todo or left:
                failures.append(f"failed twice: code {error.code}, {error.line()!r}, left {left}")
    finally:
        os.environ["PATH"] = path
    calls = (fake_bin / "calls.log").read_text().splitlines()
    retried = ["web_embedded" in call for call in calls]
    if retried != [False, True, False, True]:
        failures.append(f"yt-dlp calls used web_embedded {retried}, want a plain try then one retry per URL")
    return verdict(failures, "403 retried once with web_embedded (video and info JSON kept, partial streams gone); "
                             "two failures leave nothing and hint at yt-dlp --write-info-json")


@kat("X3", "swift")
def x3_swift_build(ctx):
    """First use compiles into bin/<name>-<sha8>; reuse skips swiftc; an edit rebuilds; a broken source is exit 5."""
    source = ctx.work / "swift" / "vlhello.swift"
    source.parent.mkdir(parents=True, exist_ok=True)
    bin_dir = ctx.work / "bin"
    source.write_text('@main struct Hello { static func main() { print("hello video-lens") } }\n')
    started = time.monotonic()
    first = build_swift(source, "vlhello", bin_dir)
    compile_s = time.monotonic() - started
    started = time.monotonic()
    again = build_swift(source, "vlhello", bin_dir)
    reuse_s = time.monotonic() - started
    output = subprocess.run([str(first)], capture_output=True, text=True).stdout.strip()
    source.write_text('@main struct Hello { static func main() { print("hello again") } }\n')
    edited = build_swift(source, "vlhello", bin_dir)
    failures = []
    if first != again or output != "hello video-lens" or reuse_s > 0.1:
        failures.append(f"reuse: same path {first == again}, output {output!r}, {reuse_s:.3f} s")
    if edited == first or first.exists() or not edited.exists():
        failures.append("edited source did not replace the old build")
    source.write_text("@main struct Broken { static func main() { undefined_call() } }\n")
    try:
        build_swift(source, "vlhello", bin_dir)
        failures.append("broken source built")
    except VlError as error:
        if error.code != EXIT_DEPENDENCY or "failed to build" not in error.what:
            failures.append(f"broken source: code {error.code} {error.what}")
    return verdict(failures, f"compiled {compile_s:.1f} s, reused {reuse_s * 1000:.0f} ms, rebuilt on edit, broken -> exit 5")


@kat("X4", "views", quick=True)
def x4_view_limits(ctx):
    """<= 1932 px per side and <= 4,761 tokens; recorded w/h/tokens equal the written file; ASCII labels."""
    run = written_run(ctx, motion_cfr(ctx), "X4")
    failures = []
    if views.visual_tokens(1932, 1932) != 4761 or views.visual_tokens(1040, 1150) != 1596:
        failures.append("token formula")
    wide = np.zeros((1000, 4000, 3), np.uint8)
    try:
        views.write_view(run, wide, "too_wide.png", level="L2", kind="test")
        failures.append("4000 px image was written")
    except AssertionError:
        pass
    fitted = views.fit_to_limits(wide)
    for name in ("fit.jpg", "fit.png"):
        view = views.write_view(run, fitted, name, level="L2", kind="test")
        data = (run.out / name).read_bytes()
        image = cv2.imread(str(run.out / name))
        is_format_right = data[:3] == b"\xff\xd8\xff" if name.endswith(".jpg") else data[:8] == b"\x89PNG\r\n\x1a\n"
        if (image.shape[1], image.shape[0]) != (view["w"], view["h"]) or not is_format_right:
            failures.append(f"{name}: file {image.shape[1]}x{image.shape[0]} vs record {view['w']}x{view['h']}")
        if max(view["w"], view["h"]) > views.MAX_SIDE_PX or view["visual_tokens"] > views.MAX_VISUAL_TOKENS:
            failures.append(f"{name}: {view['w']}x{view['h']} {view['visual_tokens']} tok over the limit")
    if views.ascii_label("한글 K012 03:12.4") != "?? K012 03:12.4":
        failures.append("ascii_label")
    return verdict(failures, f"4000x1000 refused, fitted to {fitted.shape[1]}x{fitted.shape[0]}; jpg/png records match files")
