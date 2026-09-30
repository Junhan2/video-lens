"""Declared-timing KATs (spec 7.9, 10.1 M9 and M16): scripts/declared.mjs against fixture pages in headless Chrome.

Both rows run only with --chrome and SKIP when Node or Chrome is missing (declared.mjs exits 5). Render accuracy is
checked by locating the moving box in every frame of render.mp4 and comparing it with the declared curve, so a clip
seeked to the wrong time fails even before the motion module exists.
"""
import importlib.util
import json
import os
import shutil
import subprocess

import numpy as np

from katlib import SCRIPTS_DIR, SKIP, kat, verdict
from vl import decode

DECLARED = SCRIPTS_DIR / "declared.mjs"
VIEWPORT = "480x240"
RENDER_FPS = 60
FRAME_S = 1 / RENDER_FPS
FRAME_TIME_TOLERANCE_S = 0.0001
POSITION_TOLERANCE_PX = 1.0     # at peak speed the M9 card moves 2.4 px per ms, so 1 px is well under a frame
CLIP_START_TOLERANCE_MS = 0.5
EXIT_BAD_ARGS = 2
EXIT_DEPENDENCY = 5
MEASURE_MODULES = ("detect", "sheets", "report")
BOX_WIDTH_PX = 120
BOX_START_X = 40

SLIDE_PAGE = """<!doctype html><html><head><style>
html,body{margin:0;background:#f5f5f7;width:480px;height:240px;overflow:hidden}
.card{position:absolute;left:40px;top:80px;width:120px;height:80px;background:#1f6feb;
 animation:slide 300ms cubic-bezier(0.33,1,0.68,1) 200ms both}
@keyframes slide{from{transform:translateX(0)}to{transform:translateX(240px)}}
</style></head><body><div class="card"></div></body></html>
"""
SLIDE = {"delay_ms": 200, "duration_ms": 300, "easing": "cubic-bezier(0.33, 1, 0.68, 1)", "curve": (0.33, 1, 0.68, 1),
         "travel_px": 240, "band": (90, 150), "render_s": 0.7}

# .lane is the hover target and never moves, so the pointer stays over it while .knob slides away.
HOVER_PAGE = """<!doctype html><html><head><style>
html,body{margin:0;background:#f5f5f7;width:480px;height:240px;overflow:hidden}
.lane{position:absolute;left:0;top:60px;width:480px;height:120px}
.knob{position:absolute;left:40px;top:20px;width:120px;height:80px;background:#1f6feb;
 transition:transform 250ms cubic-bezier(0.2,0,0,1) 50ms}
.lane:hover .knob{transform:translateX(200px)}
.dot{position:absolute;left:400px;top:200px;width:30px;height:30px;background:#3b82f6;transition:background-color 150ms linear}
.lane:hover ~ .dot{background-color:#ef4444}
.panel{position:absolute;left:40px;top:10px;width:60px;height:30px;background:#16a34a;opacity:0.2;transition:opacity 180ms ease-in}
.panel.open{opacity:1}
.opener{position:absolute;left:300px;top:10px;width:80px;height:30px;border:0;background:#999}
</style></head><body>
<div class="panel"></div><button class="opener"></button>
<div class="lane"><div class="knob"></div></div><div class="dot"></div>
<script>document.querySelector('.opener').addEventListener('click', () => document.querySelector('.panel').classList.add('open'))</script>
</body></html>
"""
KNOB = {"delay_ms": 50, "duration_ms": 250, "easing": "cubic-bezier(0.2, 0, 0, 1)", "curve": (0.2, 0, 0, 1),
        "travel_px": 200, "band": (90, 150), "render_s": 0.5}


def page_fixture(ctx, name, html):
    return ctx.fixture(name, lambda path: path.write_text(html, encoding="utf-8"))


def run_declared(ctx, page, out_name, *flags, env_extra=None):
    """(CompletedProcess, OUT) of `node declared.mjs PAGE --out OUT --viewport 480x240 FLAGS`."""
    out = ctx.out_dir(out_name)
    env = {**os.environ, **(env_extra or {})}
    proc = subprocess.run(["node", str(DECLARED), str(page), "--out", str(out), "--viewport", VIEWPORT, *map(str, flags)],
                          capture_output=True, text=True, timeout=300, env=env)
    return proc, out


def run_failure(label, proc, expected_code=0):
    """None when the run exited as expected with the output contract of INTERFACES 13, else a failure message."""
    if proc.returncode != expected_code:
        return f"{label}: exit {proc.returncode} (expected {expected_code}), stderr {proc.stderr.strip()!r}"
    if expected_code == 0 and not proc.stdout.startswith("declared.json: "):
        return f"{label}: stdout is not the declared.json summary ({proc.stdout[:80]!r})"
    if expected_code and proc.stdout:
        return f"{label}: stdout not empty on failure ({proc.stdout[:80]!r})"
    if expected_code == 0 and proc.stderr:
        return f"{label}: stderr not empty ({proc.stderr.strip()!r})"
    lines = proc.stderr.splitlines()
    if expected_code and (len(lines) != 1 or not lines[0].startswith("video-lens: ")):
        return f"{label}: stderr is not one 'video-lens:' line ({proc.stderr!r})"
    return None


def unavailable_reason(proc):
    return proc.stderr.strip() if proc.returncode == EXIT_DEPENDENCY else None


def load_declared(out):
    return json.loads((out / "declared.json").read_text(encoding="utf-8"))


def find_row(rows, name, kind):
    return next((row for row in rows if row["name"] == name and row["kind"] == kind), None)


def row_failures(label, row, truth, easing_key):
    """Declared delay, duration and easing (`keyframe_easings` for CSS animations, `effect_easing` for transitions)."""
    if row is None:
        return [f"{label}: not recorded"]
    failures = [f"{label}: {key} {row[key]} != {truth[key]}" for key in ("delay_ms", "duration_ms") if row[key] != truth[key]]
    easings = row[easing_key] if isinstance(row[easing_key], list) else [row[easing_key]]
    if not easings or any(easing != truth["easing"] for easing in easings):
        failures.append(f"{label}: {easing_key} {row[easing_key]} != {truth['easing']}")
    return failures


def cubic_bezier(x1, y1, x2, y2, progress):
    """CSS cubic-bezier y at x = progress (clipped to [0, 1]), by bisection on the monotone x(s)."""
    x = np.clip(np.asarray(progress, float), 0.0, 1.0)
    low, high = np.zeros_like(x), np.ones_like(x)
    for _ in range(60):
        s = (low + high) / 2
        is_left = 3 * (1 - s) ** 2 * s * x1 + 3 * (1 - s) * s * s * x2 + s ** 3 < x
        low, high = np.where(is_left, s, low), np.where(is_left, high, s)
    s = (low + high) / 2
    return 3 * (1 - s) ** 2 * s * y1 + 3 * (1 - s) * s * s * y2 + s ** 3


def box_left_edge(grey_band):
    """Sub-pixel left edge of the dark box in a band of rows: centroid of per-column coverage minus half its width.

    The box covers a quarter of the band, so the median is the background and the minimum the box colour.
    """
    band = grey_band.astype(np.float64)
    background, box = float(np.median(band)), float(band.min())
    coverage = np.clip((background - band) / max(background - box, 1.0), 0.0, 1.0).mean(axis=0)
    centroid = float((coverage * (np.arange(coverage.size) + 0.5)).sum() / coverage.sum())
    return centroid - BOX_WIDTH_PX / 2


def render_failures(video, truth, clip_start_ms):
    """Frame count and times of render.mp4, and the box position in every frame against the declared curve."""
    expected_frames = round(RENDER_FPS * truth["render_s"])
    top, bottom = truth["band"]
    errors, mistimed, failures = [], [], []
    for frame in decode.FrameStream(video, "null", pix_fmt="gray"):
        if abs(frame.t - frame.index * FRAME_S) > FRAME_TIME_TOLERANCE_S:
            mistimed.append(frame.index)
        progress = (frame.t - clip_start_ms / 1000) / (truth["duration_ms"] / 1000)
        expected_x = BOX_START_X + truth["travel_px"] * float(cubic_bezier(*truth["curve"], progress))
        errors.append(abs(box_left_edge(frame.image[top:bottom]) - expected_x))
    if mistimed:
        failures.append(f"{len(mistimed)} frame(s) not at index/{RENDER_FPS} s, first #{mistimed[0]}")
    if len(errors) != expected_frames:
        failures.append(f"render.mp4 has {len(errors)} frames, expected {expected_frames}")
    worst = max(errors, default=float("inf"))
    if worst > POSITION_TOLERANCE_PX:
        failures.append(f"box off the declared curve by {worst:.2f} px (> {POSITION_TOLERANCE_PX} px)")
    return failures, worst, len(errors)


def contract_failures(ctx, page):
    """Exit 2 on bad arguments or an unmatched selector, exit 5 without Chrome; one stderr line each."""
    cases = [("no --out", EXIT_BAD_ARGS, ["node", str(DECLARED), str(page)], None),
             ("bad --trigger", EXIT_BAD_ARGS, ["node", str(DECLARED), str(page), "--out", str(ctx.out_dir("M9-args")),
                                               "--trigger", "drag .card"], None),
             ("unmatched selector", EXIT_BAD_ARGS, ["node", str(DECLARED), str(page), "--out", str(ctx.out_dir("M9-sel")),
                                                    "--trigger", "hover .absent"], None),
             ("no Chrome", EXIT_DEPENDENCY, ["node", str(DECLARED), str(page), "--out", str(ctx.out_dir("M9-dep"))],
              {"VIDEO_LENS_CHROME": str(ctx.work / "no-such-chrome")})]
    failures = []
    for label, code, command, env_extra in cases:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=120, env={**os.environ, **(env_extra or {})})
        failures.append(run_failure(label, proc, code))
    return [failure for failure in failures if failure]


def measured_element(analysis):
    """The motion element with the largest horizontal travel in an analysis.json document."""
    elements = [element for event in (analysis.get("motion") or {}).get("events", []) for element in event["elements"]]
    return max(elements, key=lambda element: abs(element["geometry"]["translate_px"][0]), default=None)


def measured_failures(ctx, video, clip_start_ms, truth):
    """spec M9: measured start and duration = declared +-1 frame, curve named easeOutCubic."""
    result = ctx.vl("analyze", video, "--out", ctx.out_dir("M9-analyze"), "--mode", "motion", "--json")
    if result.returncode != 0:
        return [f"analyze rc {result.returncode}: {result.stderr.strip()}"], "not measured"
    element = measured_element(json.loads(result.stdout))
    if element is None:
        return ["analyze found no motion element"], "not measured"
    start_s, duration_ms, name = element["timing"]["start_s"], element["timing"]["duration_ms"], element["easing"]["name"]
    failures = []
    if abs(start_s - clip_start_ms / 1000) > FRAME_S:
        failures.append(f"measured start {start_s:.4f} s vs declared {clip_start_ms / 1000:.4f} s")
    if abs(duration_ms - truth["duration_ms"]) > FRAME_S * 1000:
        failures.append(f"measured duration {duration_ms:.1f} ms vs declared {truth['duration_ms']} ms")
    if name != "easeOutCubic":
        failures.append(f"measured curve {name}, expected easeOutCubic")
    return failures, f"measured start {start_s:.4f} s, {duration_ms:.1f} ms, {name}"


@kat("M9", "declared", chrome=True)
def m9_declared_matches_render(ctx):
    if shutil.which("node") is None:
        return SKIP, "node not found"
    page = page_fixture(ctx, "declared_slide.html", SLIDE_PAGE)
    proc, out = run_declared(ctx, page, "M9", "--render", RENDER_FPS, SLIDE["render_s"])
    if unavailable_reason(proc):
        return SKIP, unavailable_reason(proc)
    failure = run_failure("render run", proc)
    if failure:
        return verdict([failure], "")
    slide = find_row(load_declared(out)["animations"], "slide", "CSSAnimation")
    failures = row_failures("slide", slide, SLIDE, "keyframe_easings")
    clip_start_ms = slide.get("clip_start_ms") if slide else None
    if clip_start_ms is None or abs(clip_start_ms - SLIDE["delay_ms"]) > CLIP_START_TOLERANCE_MS:
        failures.append(f"slide clip_start_ms {clip_start_ms} != {SLIDE['delay_ms']}")
        clip_start_ms = SLIDE["delay_ms"]
    frame_failures, worst_px, frames = render_failures(out / "render.mp4", SLIDE, clip_start_ms)
    failures += frame_failures + contract_failures(ctx, page)
    declared = (f"declared delay 200, duration 300, keyframe easing read; render {frames} frames on the curve within "
                f"{worst_px:.2f} px; exit 2/5 contract ok")
    if failures:
        return verdict(failures, "")
    missing = [name for name in MEASURE_MODULES if importlib.util.find_spec(f"vl.{name}") is None]
    if missing:
        return SKIP, f"{declared}; measuring render.mp4 needs " + ", ".join(f"vl/{name}.py" for name in missing)
    measured, measured_detail = measured_failures(ctx, out / "render.mp4", clip_start_ms, SLIDE)
    return verdict(measured, f"{declared}; {measured_detail}")


@kat("M16", "declared", chrome=True)
def m16_trigger_records_transitions(ctx):
    if shutil.which("node") is None:
        return SKIP, "node not found"
    page = page_fixture(ctx, "declared_hover.html", HOVER_PAGE)
    control, control_out = run_declared(ctx, page, "M16-control", "--wait-ms", 400)
    if unavailable_reason(control):
        return SKIP, unavailable_reason(control)
    hover, hover_out = run_declared(ctx, page, "M16-hover", "--trigger", "hover .lane", "--wait-ms", 800)
    click, click_out = run_declared(ctx, page, "M16-click", "--trigger", "click .opener", "--wait-ms", 500)
    render, render_out = run_declared(ctx, page, "M16-render", "--trigger", "hover .lane", "--render", RENDER_FPS, KNOB["render_s"])
    failures = [f for f in (run_failure("control", control), run_failure("hover", hover), run_failure("click", click),
                            run_failure("render", render)) if f]
    if failures:
        return verdict(failures, "")
    control_rows = load_declared(control_out)["animations"]
    if control_rows:
        failures.append(f"without --trigger {len(control_rows)} animation(s) recorded, expected none")
    hover_rows = load_declared(hover_out)["animations"]
    knob = find_row(hover_rows, "transform", "CSSTransition")
    failures += row_failures("hover transform", knob, KNOB, "effect_easing")
    failures += row_failures("hover background-color", find_row(hover_rows, "background-color", "CSSTransition"),
                             {"delay_ms": 0, "duration_ms": 150, "easing": "linear"}, "effect_easing")
    if knob and knob["play_state"] != "finished":
        failures.append(f"hover transform play_state {knob['play_state']}: not read after it finished")
    failures += row_failures("click opacity", find_row(load_declared(click_out)["animations"], "opacity", "CSSTransition"),
                             {"delay_ms": 0, "duration_ms": 180, "easing": "ease-in"}, "effect_easing")
    rendered_knob = find_row(load_declared(render_out)["animations"], "transform", "CSSTransition")
    clip_start_ms = rendered_knob.get("clip_start_ms") if rendered_knob else None
    if clip_start_ms is None or abs(clip_start_ms - KNOB["delay_ms"]) > CLIP_START_TOLERANCE_MS:
        failures.append(f"rendered transform clip_start_ms {clip_start_ms} != {KNOB['delay_ms']}")
        clip_start_ms = KNOB["delay_ms"]
    frame_failures, worst_px, frames = render_failures(render_out / "render.mp4", KNOB, clip_start_ms)
    failures += frame_failures
    return verdict(failures, "no trigger: 0 recorded; hover: transform 50+250 ms cubic-bezier(0.2, 0, 0, 1) and "
                             "background-color 150 ms linear recorded after finishing; click: opacity 180 ms ease-in; "
                             f"hover render {frames} frames within {worst_px:.2f} px")
