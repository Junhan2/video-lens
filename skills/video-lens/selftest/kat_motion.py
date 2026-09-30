"""Motion KATs (spec 10.1): M0-M8, M10-M15, M17, M18, M19-M27. M9 and M16 (Chrome) live in kat_declared.py.

Each KAT analyses a synthetic fixture with known truth. Until the integrator's vl/sheets.py and vl/report.py exist,
`analyze` cannot run end to end, so the motion stage runs in-process on a `prepare_run` Run (the same object and
defaults `vl.py analyze` builds); afterwards the KATs go through `vl.py analyze --json` like a user would.
"""
import importlib.util
import json
import os
from pathlib import Path

import numpy as np

import fixtures_motion as fm
from katlib import SKIP, kat, verdict
from vl import detect, easing
from vl.cache import json_default
from vl.cli import prepare_run

ORBIT = Path(os.environ.get("VIDEO_LENS_REAL_SAMPLE", "/nonexistent"))   # a real 3D carousel recording; KATs skip without it
E2E_MODULES = ("sheets", "report")
M0_NOISE = 0.01
M0_CONFIGS = ((60, 0.3), (60, 0.2), (30, 0.3))
M0_CLOSE = 0.05
CURVE_CLOSE = 0.05


def motion_of(ctx, video, name, *flags):
    """The analysis.json motion section of `analyze VIDEO --mode motion FLAGS`."""
    out = ctx.out_dir(name)
    if all(importlib.util.find_spec(f"vl.{m}") for m in E2E_MODULES):
        result = ctx.vl("analyze", video, "--out", out, "--mode", "motion", "--json", *flags)
        if result.returncode != 0:
            raise AssertionError(f"analyze exit {result.returncode}: {result.stderr.strip()}")
        return json.loads(result.stdout)["motion"]
    run = prepare_run(["analyze", str(video), "--out", str(out), "--mode", "motion", *map(str, flags)])
    run.set_mode("motion", "--mode motion")
    return json.loads(json.dumps(detect.analyze_motion(run), default=json_default))


def fixture(ctx, name, build, vfr=False):
    cfr = ctx.fixture(f"motion_{name}_cfr.mp4", build)
    return ctx.fixture(f"motion_{name}_vfr.mp4", fm.build_vfr(cfr)) if vfr else cfr


def analysed(motion):
    return [e for e in motion["events"] if e["analysed"]]


def only_element(motion, label, failures):
    events = analysed(motion)
    if len(events) != 1 or len(events[0]["elements"]) != 1:
        failures.append(f"{label}: expected 1 event with 1 element, got {[len(e['elements']) for e in events]}")
        return None
    return events[0]["elements"][0]


def check_timing(element, truth, label, failures, start_ms, duration_pct):
    timing = element["timing"]
    start_error = (timing["start_s"] - truth["start"]) * 1000
    duration_error = (timing["duration_ms"] / (truth["duration"] * 1000) - 1) * 100
    if abs(start_error) > start_ms:
        failures.append(f"{label} start {start_error:+.1f} ms (limit {start_ms})")
    if abs(duration_error) > duration_pct:
        failures.append(f"{label} duration {duration_error:+.1f} % (limit {duration_pct})")
    return start_error, duration_error


def check_name(element, name, label, failures):
    if element["easing"]["name"] != name:
        failures.append(f"{label} easing {element['easing']['name']} (truth {name})")


def curve_distance(bezier_a, bezier_b):
    x = np.linspace(0, 1, 2001)
    return float(np.max(np.abs(fm.bezier(*bezier_a)(x) - fm.bezier(*bezier_b)(x))))


@kat("M0", "motion", quick=True)
def m0_fitter_alone(ctx):
    """Every named curve x {60 fps 300 ms, 60 fps 200 ms, 30 fps 300 ms}, 1 % noise, timing pinned as in D1's check:
    exact name unless another named curve lies within 0.05 of the truth, then within 0.05."""
    curves = easing.named_curves()
    rng = np.random.default_rng(0)
    failures, total = [], 0
    for truth in curves:
        has_close = any(c.name != truth.name and easing.curve_distance(c, truth) <= M0_CLOSE for c in curves)
        for fps, duration in M0_CONFIGS:
            dt = 1 / fps
            start = 0.1 + 0.37 * dt
            t = np.arange(0, start + duration + 6 * dt, dt)
            values = fm.bezier(*truth.bezier)(np.clip((t - start) / duration, 0, 1)) + rng.normal(0, M0_NOISE, len(t))
            channel = easing.Channel("progress", values, np.ones_like(values), 0.0, 1.0)
            fit = easing.fit_channels(t, [channel], dt, curves, timing=(start, duration))
            distance = curve_distance(fit.curve.bezier, truth.bezier)
            is_ok = distance <= M0_CLOSE if has_close else fit.curve.name == truth.name
            total += 1
            if not is_ok:
                failures.append(f"{truth.name} {fps}fps {duration * 1000:.0f}ms -> {fit.curve.name} ({distance:.3f})")
    return verdict(failures, f"{total}/{total} named curves ({len(curves)} x 3 configs, 1 % noise, timing pinned)")


@kat("M1", "motion", quick=True)
def m1_translate(ctx):
    failures, notes = [], []
    for label, vfr in (("cfr", False), ("vfr", True)):
        element = only_element(motion_of(ctx, fixture(ctx, "translate", fm.build_translate, vfr), f"M1-{label}"), label, failures)
        if element is None:
            continue
        check_name(element, fm.TRANSLATE["name"], label, failures)
        start_error, duration_error = check_timing(element, fm.TRANSLATE, label, failures, 2, 2)
        travel = float(np.hypot(*element["geometry"]["translate_px"]))
        if abs(travel - fm.TRANSLATE["dx"]) > 0.5:
            failures.append(f"{label} travel {travel:.2f} px (truth {fm.TRANSLATE['dx']})")
        notes.append(f"{label} {element['easing']['name']} start {start_error:+.1f} ms dur {duration_error:+.1f} % "
                     f"travel {travel:.1f} px rmse {element['easing']['fit_rmse']}")
    return verdict(failures, "; ".join(notes))


@kat("M2", "motion")
def m2_integer_pixels(ctx):
    failures = []
    element = only_element(motion_of(ctx, fixture(ctx, "translate_snap", fm.build_translate_snap), "M2"), "snap", failures)
    if element is None:
        return verdict(failures, "")
    check_name(element, fm.TRANSLATE_SNAP["name"], "snap", failures)
    _, duration_error = check_timing(element, fm.TRANSLATE_SNAP, "snap", failures, 1000, 2)
    timing = element["timing"]
    if not timing["duration_visible_ms"] < timing["duration_ms"]:
        failures.append(f"visible {timing['duration_visible_ms']} ms is not below fitted {timing['duration_ms']} ms")
    return verdict(failures, f"{element['easing']['name']} {timing['duration_ms']} ms ({duration_error:+.1f} %), "
                             f"visible {timing['duration_visible_ms']} ms")


@kat("M3", "motion", quick=True)
def m3_scale_fade(ctx):
    failures, notes = [], []
    for label, vfr in (("cfr", False), ("vfr", True)):
        element = only_element(motion_of(ctx, fixture(ctx, "scalefade", fm.build_scalefade, vfr), f"M3-{label}"), label, failures)
        if element is None:
            continue
        check_name(element, fm.SCALEFADE["name"], label, failures)
        if sorted(element["types"]) != ["fade", "scale"]:
            failures.append(f"{label} types {element['types']}")
        start_error, duration_error = check_timing(element, fm.SCALEFADE, label, failures, 2, 3)
        scale_from = element["geometry"]["scale_from"]
        if abs(scale_from - fm.SCALEFADE["scale_from"]) > 0.01:
            failures.append(f"{label} scale_from {scale_from}")
        notes.append(f"{label} {element['easing']['name']} start {start_error:+.1f} ms dur {duration_error:+.1f} % scale_from {scale_from}")
    return verdict(failures, "; ".join(notes))


@kat("M4", "motion")
def m4_rotate(ctx):
    failures, notes = [], []
    for label, vfr in (("cfr", False), ("vfr", True)):
        element = only_element(motion_of(ctx, fixture(ctx, "rotate", fm.build_rotate, vfr), f"M4-{label}"), label, failures)
        if element is None:
            continue
        check_name(element, fm.ROTATE["name"], label, failures)
        start_error, duration_error = check_timing(element, fm.ROTATE, label, failures, 2, 1)
        angle = element["geometry"]["rotate_deg"]
        if abs(angle + fm.ROTATE["degrees"]) > 0.1:         # cv2 turns counter-clockwise: CSS rotate(-20deg)
            failures.append(f"{label} angle {angle} deg (truth -{fm.ROTATE['degrees']} in CSS terms)")
        notes.append(f"{label} start {start_error:+.1f} ms dur {duration_error:+.2f} % angle {angle}")
    return verdict(failures, "; ".join(notes))


@kat("M5", "motion", quick=True)
def m5_colour(ctx):
    failures, notes = [], []
    for label, vfr in (("cfr", False), ("vfr", True)):
        motion = motion_of(ctx, fixture(ctx, "color", fm.build_color, vfr), f"M5-{label}")
        element = only_element(motion, label, failures)
        if element is None:
            continue
        event = analysed(motion)[0]
        if event["class"] not in ("motion", "subtle"):
            failures.append(f"{label} class {event['class']}")
        if element["types"] != ["color"]:
            failures.append(f"{label} types {element['types']}")
        check_name(element, fm.COLOR["name"], label, failures)
        _, duration_error = check_timing(element, fm.COLOR, label, failures, 1000, 1)
        notes.append(f"{label} class {event['class']} (max diff {event['features']['peak_maxdiff']}) "
                     f"{element['easing']['name']} dur {duration_error:+.2f} %")
    return verdict(failures, "; ".join(notes))


def stagger_check(motion, label, failures):
    events = analysed(motion)
    if len(events) != 1 or len(events[0]["elements"]) != 5:
        failures.append(f"{label}: expected 1 event with 5 elements, got {[len(e['elements']) for e in events]}")
        return None
    event = events[0]
    ordered = sorted(event["elements"], key=lambda e: e["timing"]["start_s"])
    if [e["bbox_src"][1] for e in ordered] != sorted(e["bbox_src"][1] for e in ordered):
        failures.append(f"{label}: start order is not top to bottom")
    worst_start = worst_duration = 0.0
    for element, start in zip(ordered, fm.STAGGER["starts"]):
        check_name(element, fm.STAGGER["name"], f"{label} {element['id']}", failures)
        start_error, duration_error = check_timing(element, {"start": start, "duration": fm.STAGGER["duration"]},
                                                   f"{label} {element['id']}", failures, 3, 8)
        worst_start, worst_duration = max(worst_start, abs(start_error)), max(worst_duration, abs(duration_error))
    step = (event["stagger"] or {}).get("step_ms_median")
    if step is None or abs(step - fm.STAGGER["step_ms"]) > 3:
        failures.append(f"{label} stagger step {step} ms")
    return f"{label} 5/5 {fm.STAGGER['name']}, |start| <= {worst_start:.1f} ms, |dur| <= {worst_duration:.1f} %, step {step} ms"


@kat("M6", "motion", quick=True)
def m6_stagger(ctx):
    failures, notes = [], []
    for label, vfr in (("cfr", False),) + ((() if ctx.is_quick else (("vfr", True),))):
        note = stagger_check(motion_of(ctx, fixture(ctx, "stagger", fm.build_stagger, vfr), f"M6-{label}"), label, failures)
        notes += [note] if note else []
    return verdict(failures, "; ".join(notes))


def ui_check(motion, label, failures):
    events = analysed(motion)
    if len(events) != 2:
        failures.append(f"{label}: expected 2 events (title, cards), got {len(events)}")
        return None
    title_event, card_event = events
    notes = []
    if len(title_event["elements"]) != 1:
        failures.append(f"{label} title: {len(title_event['elements'])} elements")
    else:
        title, truth = title_event["elements"][0], fm.UI["title"]
        distance = curve_distance(title["easing"]["cubic_bezier"], truth["bezier"])
        start_error = (title["timing"]["start_s"] - truth["start"]) * 1000
        duration_error = (title["timing"]["duration_ms"] / (truth["duration"] * 1000) - 1) * 100
        low, high = title["timing"]["duration_ms_range"]
        if distance > 0.05:
            failures.append(f"{label} title curve {title['easing']['name']} is {distance:.3f} from the truth")
        if abs(start_error) > 10:
            failures.append(f"{label} title start {start_error:+.1f} ms")
        if abs(duration_error) > 10 and not low <= truth["duration"] * 1000 <= high:
            failures.append(f"{label} title duration {duration_error:+.1f} %, range {low}-{high}")
        if title["confidence"] == "high" and title["easing"]["near_ties"]:
            failures.append(f"{label} title confidence high despite near ties")
        notes.append(f"title {title['easing']['name']} (dist {distance:.3f}) start {start_error:+.1f} ms dur {duration_error:+.1f} % {title['confidence']}")
    cards = sorted(card_event["elements"], key=lambda e: e["timing"]["start_s"])
    if len(cards) != 3:
        failures.append(f"{label} cards: {len(cards)} elements")
        return "; ".join(notes)
    travels = []
    for card, truth in zip(cards, fm.UI["cards"]):
        check_name(card, "md2-standard", f"{label} {card['id']}", failures)
        check_timing(card, truth, f"{label} {card['id']}", failures, 5, 3)
        travel = float(np.hypot(*card["geometry"]["translate_px"]))
        travels.append(travel)
        if abs(travel - truth["dy"]) > 1.5:
            failures.append(f"{label} {card['id']} travel {travel:.1f} px (fitted amplitude)")
    step = (card_event["stagger"] or {}).get("step_ms_median")
    if step is None or abs(step - 100) > 5:
        failures.append(f"{label} card stagger {step} ms")
    notes.append(f"cards 3x md2-standard, step {step} ms, travel {min(travels):.1f}-{max(travels):.1f} px")
    return f"{label}: " + ", ".join(notes)


@kat("M7", "motion", quick=True)
def m7_ui_clip(ctx):
    failures, notes = [], []
    for label, vfr in (("cfr", False),) + ((() if ctx.is_quick else (("vfr", True),))):
        note = ui_check(motion_of(ctx, fixture(ctx, "ui", fm.build_ui, vfr), f"M7-{label}"), label, failures)
        notes += [note] if note else []
    return verdict(failures, "; ".join(notes))


@kat("M8", "motion", quick=True)
def m8_frozen_frames(ctx):
    failures = []
    element = only_element(motion_of(ctx, ctx.fixture("motion_frozen_cfr.mp4", fm.build_frozen), "M8"), "frozen", failures)
    if element is None:
        return verdict(failures, "")
    stalled = element["timing"]["stalled_frames_s"]
    if len(stalled) != len(fm.FROZEN_TIMES) or any(abs(a - b) > 0.0005 for a, b in zip(stalled, fm.FROZEN_TIMES)):
        failures.append(f"stalled {stalled} (truth {list(fm.FROZEN_TIMES)})")
    _, duration_error = check_timing(element, fm.TRANSLATE, "refit", failures, 1000, 1)
    return verdict(failures, f"stalled {stalled}, refit {element['easing']['name']} {element['timing']['duration_ms']} ms ({duration_error:+.2f} %)")


def orbit_motion(ctx, name, *flags):
    return motion_of(ctx, ORBIT, name, *flags) if ORBIT.is_file() else None


@kat("M10", "motion-real")
def m10_orbit_classes(ctx):
    motion = orbit_motion(ctx, "M10")
    if motion is None:
        return SKIP, "no real sample (set VIDEO_LENS_REAL_SAMPLE to a carousel recording)"
    summary, failures = motion["summary"], []
    if summary["motion_events"] != 11:
        failures.append(f"{summary['motion_events']} motion events (expected 11)")
    if summary["subtle_events"] or summary["micro_events"]:
        failures.append(f"subtle {summary['subtle_events']}, micro {summary['micro_events']} (expected every other event noise)")
    loop = summary["loop"]
    if loop is None or abs(loop["period_s"] - 1.55) > 0.1:
        failures.append(f"loop {loop}")
    return verdict(failures, f"{summary['motion_events']} motion, {summary['noise_events']} noise, 0 micro, loop {loop}")


def is_s_shape(bezier):
    """Slow start and slow end: slope at 0 (y1/x1) and at 1 ((1-y2)/(1-x2)) both below 1."""
    x1, y1, x2, y2 = bezier
    start = y1 / x1 if x1 > 0 else (0.0 if y1 <= 0 else np.inf)
    end = (1 - y2) / (1 - x2) if x2 < 1 else (0.0 if y2 >= 1 else np.inf)
    return start < 1 and end < 1


@kat("M11", "motion-real")
def m11_orbit_scroll(ctx):
    motion = orbit_motion(ctx, "M11", "--motion-mode", "scroll")
    if motion is None:
        return SKIP, "no real sample (set VIDEO_LENS_REAL_SAMPLE to a carousel recording)"
    failures = []
    first = next((e for e in analysed(motion) if e["class"] == "motion"), None)
    if first is None or not first["elements"] or first["scroll"] is None:
        return verdict([f"no scroll result for the first transition: {first and first['notes']}"], "")
    element, scroll = first["elements"][0], first["scroll"]
    dx, dy = scroll["travel_px"]
    if dx <= 0 or abs(dy) > abs(dx):
        failures.append(f"travel {scroll['travel_px']} is not +x")
    fitted, stepped = element["geometry"]["peak_speed_px_s"], scroll["peak_step_speed_px_s"]
    if abs(fitted / stepped - 1) > 0.10:
        failures.append(f"peak speed {fitted} vs per-frame phase-corr max {stepped} px/s")
    if not is_s_shape(element["easing"]["cubic_bezier"]):
        failures.append(f"easing {element['easing']['name']} {element['easing']['cubic_bezier']} is not an in-out S")
    return verdict(failures, f"{first['id']}: travel {scroll['travel_px']} px, peak {fitted} vs {stepped} px/s, "
                             f"{element['easing']['name']} {element['easing']['cubic_bezier']} {element['timing']['duration_ms']} ms "
                             f"rmse {element['easing']['fit_rmse']}")


@kat("M11b", "motion-real")
def m11b_orbit_auto_scroll(ctx):
    motion = orbit_motion(ctx, "M10")
    if motion is None:
        return SKIP, "no real sample (set VIDEO_LENS_REAL_SAMPLE to a carousel recording)"
    transitions = [e for e in motion["events"] if e["class"] == "motion"]
    modes = [e["mode"] for e in transitions]
    failures = [] if transitions and all(m == "scroll" for m in modes) else [f"modes {modes}"]
    return verdict(failures, f"{modes.count('scroll')}/{len(modes)} transitions in scroll mode")


@kat("M12", "motion")
def m12_exit(ctx):
    failures = []
    element = only_element(motion_of(ctx, ctx.fixture("motion_exit_cfr.mp4", fm.build_exit), "M12"), "exit", failures)
    if element is None:
        return verdict(failures, "")
    if element["origin"] != "start" or not element["disappears"]:
        failures.append(f"origin {element['origin']}, disappears {element['disappears']}")
    check_name(element, fm.EXIT["name"], "exit", failures)
    _, duration_error = check_timing(element, fm.EXIT, "exit", failures, 1000, 3)
    return verdict(failures, f"origin start, disappears, {element['easing']['name']} {element['timing']['duration_ms']} ms "
                             f"({duration_error:+.1f} %), types {element['types']}")


@kat("M13", "motion", quick=True)
def m13_caret(ctx):
    summary = motion_of(ctx, ctx.fixture("motion_caret_cfr.mp4", fm.build_caret), "M13")["summary"]
    times = summary["micro_times_s"]
    expected = int(fm.CARET["seconds"] / fm.CARET["period_s"])
    failures = []
    if summary["motion_events"] or summary["subtle_events"]:
        failures.append(f"motion {summary['motion_events']}, subtle {summary['subtle_events']}")
    if len(times) != expected:
        failures.append(f"{len(times)} micro events (caret toggles {expected} times)")
    return verdict(failures, f"0 motion, {len(times)} micro at {times}")


@kat("M14", "motion")
def m14_spring_overshoot(ctx):
    failures = []
    element = only_element(motion_of(ctx, ctx.fixture("motion_spring_cfr.mp4", fm.build_spring), "M14"), "spring", failures)
    if element is None:
        return verdict(failures, "")
    fit = element["easing"]
    if not fit["overshoot"] or not fit["css_linear"]:
        return verdict([f"overshoot {fit['overshoot']}, css_linear {fit['css_linear']}"], "")
    positions, values = parse_linear(fit["css_linear"])
    x = np.linspace(0, 1, 2001)
    error = float(np.max(np.abs(np.interp(x, positions, values) - np.array([fm.spring_progress(v) for v in x]))))
    if error >= 0.03:
        failures.append(f"css_linear max error {error:.4f}")
    return verdict(failures, f"overshoot, css_linear {len(values)} stops, max error {error:.4f} vs truth, "
                             f"span {fit['css_linear_timing']}")


def parse_linear(text):
    """Stops of a CSS linear(): 'v' or 'v p%'; first and last default to 0 % and 100 %."""
    parts = [p.split() for p in text[text.index("(") + 1:text.rindex(")")].split(",")]
    values = [float(p[0]) for p in parts]
    positions = [float(p[1].rstrip("%")) / 100 if len(p) > 1 else (0.0 if i == 0 else 1.0) for i, p in enumerate(parts)]
    return positions, values


@kat("M15", "motion")
def m15_one_group(ctx):
    motion = motion_of(ctx, ctx.fixture("motion_adjacent_cfr.mp4", fm.build_adjacent), "M15")
    events = analysed(motion)
    failures = [] if len(events) == 1 and len(events[0]["elements"]) == 2 else [f"events {[len(e['elements']) for e in events]}"]
    groups = events[0]["timing_groups"] if events else []
    if len(groups) != 1:
        failures.append(f"{len(groups)} timing groups: {groups}")
    return verdict(failures, f"1 timing group of {len(groups[0]['elements']) if groups else 0} elements")


@kat("M17", "motion")
def m17_thirty_fps(ctx):
    failures = []
    element = only_element(motion_of(ctx, ctx.fixture("motion_translate30_cfr.mp4", fm.build_translate_30fps), "M17"), "30fps", failures)
    if element is None:
        return verdict(failures, "")
    duration, (low, high) = element["timing"]["duration_ms"], element["timing"]["duration_ms_range"]
    truth = fm.TRANSLATE["duration"] * 1000
    if abs(duration - truth) > 1000 / 30:
        failures.append(f"duration {duration} ms (truth {truth:.0f})")
    if not low <= truth <= high:
        failures.append(f"range {low}-{high} does not hold {truth:.0f}")
    return verdict(failures, f"{duration} ms, range {low}-{high}")


@kat("M18", "motion", quick=True)
def m18_encoder_noise(ctx):
    first = ctx.fixture("motion_static_gen1.mp4", fm.build_static)
    summary = motion_of(ctx, ctx.fixture("motion_static_gen2.mp4", fm.build_static_twice(first)), "M18")["summary"]
    failures = [] if summary["motion_events"] == 0 else [f"{summary['motion_events']} motion events"]
    return verdict(failures, f"0 motion events ({summary['noise_events']} noise, {summary['subtle_events']} subtle)")


def report_and_motion(ctx, video, name, *flags):
    """(report text, analysis.json motion section) of one `analyze VIDEO --mode motion FLAGS` run."""
    out = ctx.out_dir(name)
    result = ctx.vl("analyze", video, "--out", out, "--mode", "motion", *flags)
    if result.returncode != 0:
        raise AssertionError(f"analyze exit {result.returncode}: {result.stderr.strip()}")
    return result.stdout, json.loads((out / "analysis.json").read_text(encoding="utf-8"))["motion"]


def truth_t50(truth, start, duration):
    return start + easing.make_curve("truth", truth).x_at_half() * duration


def check_curve(element, name, bezier, label, failures):
    """Named truth: that name; custom truth: a reported curve within CURVE_CLOSE of it."""
    if name != "custom":
        return check_name(element, name, label, failures)
    distance = curve_distance(element["easing"]["cubic_bezier"], bezier)
    if distance > CURVE_CLOSE:
        failures.append(f"{label} curve {element['easing']['cubic_bezier']} is {distance:.3f} from the truth")


def edge_clip_video(ctx, key, vfr):
    if key in fm.EDGE_TOASTS:
        return fixture(ctx, f"edge_{key}", fm.build_edge_toast(key), vfr)
    if key in fm.SHADOW_SLIDES:
        return ctx.fixture(f"motion_{key}_cfr.mp4", fm.build_shadow_slide(key))
    return ctx.fixture("motion_panel_toast_cfr.mp4", fm.build_panel_toast)


def check_custom_timing(element, truth, label, failures):
    """A custom curve's start and t50 within 3 ms, its true duration inside the reported range (a flat tail leaves
    the duration itself loose)."""
    timing = element["timing"]
    start_error = (timing["start_s"] - truth["start"]) * 1000
    t50_error = (timing["t50_s"] - truth_t50(truth["bezier"], truth["start"], truth["duration"])) * 1000
    low, high = timing["duration_ms_range"]
    if abs(start_error) > 3 or abs(t50_error) > 3 or not low <= truth["duration"] * 1000 <= high:
        failures.append(f"{label} start {start_error:+.1f} ms, t50 {t50_error:+.1f} ms, duration range {low}-{high} "
                        f"(truth {truth['duration'] * 1000:g} ms)")
    return start_error, (timing["duration_ms"] / (truth["duration"] * 1000) - 1) * 100


def edge_clip_check(ctx, key, truth, vfr, failures):
    """One toast crossing an edge: pure translate, clipped, start/duration/curve of the truth, travel at least the
    hidden part plus the visible part, start state assumed at the edge (no `?`). A slide with a box-shadow shows a
    strip of it inside the edge at its hidden rest state: that strip places the start state (reading seen) and is no
    element of its own. No shape tie: the free curve fits these clean slides far better than any named one."""
    label = f"{key}{' vfr' if vfr else ''}"
    element = only_element(motion_of(ctx, edge_clip_video(ctx, key, vfr), f"M19-{label.replace(' ', '-')}"), label, failures)
    if element is None:
        return None
    if element["types"] != ["translate"]:
        failures.append(f"{label} types {element['types']} (a slide across an edge is translate only)")
    clip = element.get("clip") or {}
    container = "inner" if key == "panel" else "frame"
    reading = "seen" if "shadow" in truth else "at-edge"
    if clip.get("edge") != truth["edge"] or clip.get("container") != container or clip.get("reading") != reading:
        where = "a clipping panel" if container == "inner" else "the frame"
        failures.append(f"{label} clip {clip or None} (truth: {truth['edge']} edge of {where}, reading {reading})")
    if truth["name"] == "custom":
        start_error, duration_error = check_custom_timing(element, truth, label, failures)
    else:
        start_error, duration_error = check_timing(element, truth, label, failures, 3, 3)
    check_curve(element, truth["name"], truth["bezier"], label, failures)
    if element["easing"]["shape_tie"]:
        failures.append(f"{label} shape tie {[tie['name'] for tie in element['easing']['near_ties']]}")
    (rx, ry), (hx, hy) = truth["rest"], truth["hidden"]
    travel_truth, travel = float(np.hypot(hx - rx, hy - ry)), float(np.hypot(*element["geometry"]["translate_px"]))
    if abs(travel - travel_truth) > 1.5 or abs(clip.get("travel_min_px", 0) - travel_truth) > 1.5:
        failures.append(f"{label} travel {travel:.1f} px, at least {clip.get('travel_min_px')} (truth {travel_truth:g})")
    if not all(c["amplitude_fixed"] for c in element["channels"].values()):
        failures.append(f"{label} start state left unknown: the easing is marked '?'")
    return (f"{label} {element['easing']['name']} {start_error:+.1f} ms {duration_error:+.1f} % travel >= "
            f"{clip.get('travel_min_px')} px")


@kat("M19", "motion", quick=True)
def m19_edge_clipped(ctx):
    """D1: toasts entering from beyond the right and bottom frame edges, leaving past the left one, and entering a
    clipping panel read as pure slides clipped by the edge, timed from the part that stays visible; so do a toast and a
    flush full-height drawer whose box-shadow still shows inside the edge before they enter."""
    failures, notes = [], []
    cases = ([(key, fm.EDGE_TOASTS[key], key == "bottom") for key in fm.EDGE_TOASTS] + [("panel", fm.PANEL_TOAST, False)]
             + [(key, truth, False) for key, truth in fm.SHADOW_SLIDES.items()])
    for key, truth, vfr in cases:
        note = edge_clip_check(ctx, key, truth, vfr, failures)
        notes += [note] if note else []
    return verdict(failures, "; ".join(notes))


def rows_check(report, motion, failures):
    """D2: one row per list row, one shared custom curve, starts within half a frame, t50 within 3 ms, the true
    duration inside the range, no channel split, no per-channel fits listed."""
    events = analysed(motion)
    if len(events) != 1 or len(events[0]["elements"]) != fm.ROWS["count"]:
        failures.append(f"expected 1 event with {fm.ROWS['count']} rows, got {[len(e['elements']) for e in events]}")
        return "rows"
    event, dpr, frame = events[0], fm.ROWS["dpr"], 1 / fm.ROWS["fps"]
    rows = sorted(event["elements"], key=lambda e: e["bbox_src"][1])
    worst_t50 = worst_start = worst_curve = 0.0
    for index, element in enumerate(rows):
        label, start = element["id"], fm.ROWS["start"] + index * fm.ROWS["step"]
        timing, own = element["timing"], [n for n, c in element["channels"].items() if "own_fit" in c]
        if sorted(element["types"]) != ["fade", "translate"] or element["property_timing_differs"] or own:
            failures.append(f"{label} types {element['types']}, split {element['property_timing_differs']}, own fits {own}")
        t50_error = (timing["t50_s"] - truth_t50(fm.ROWS["bezier"], start, fm.ROWS["duration"])) * 1000
        start_error = (timing["start_s"] - start) * 1000
        distance = curve_distance(element["easing"]["cubic_bezier"], fm.ROWS["bezier"])
        low, high = timing["duration_ms_range"]
        if abs(t50_error) > 3 or abs(start_error) > 500 * frame or element["easing"]["source"] != "custom" or distance > 0.1:
            failures.append(f"{label} t50 {t50_error:+.1f} ms, start {start_error:+.1f} ms, {element['easing']['name']} "
                            f"{element['easing']['cubic_bezier']} ({distance:.3f} from the truth)")
        if not low <= fm.ROWS["duration"] * 1000 <= high:
            failures.append(f"{label} duration range {low}-{high} misses {fm.ROWS['duration'] * 1000:g} ms")
        if element["easing"]["shape_tie"]:
            failures.append(f"{label} shape tie {[tie['name'] for tie in element['easing']['near_ties']]} although the "
                            f"free curve fits better (rmse {element['easing']['fit_rmse']})")
        travel = abs(element["geometry"]["translate_css_px"][0])
        if abs(travel - abs(fm.ROWS["dx_css"])) > 1.5:
            failures.append(f"{label} travel {travel} css px at dpr {dpr} (truth {abs(fm.ROWS['dx_css'])})")
        worst_t50, worst_start, worst_curve = max(worst_t50, abs(t50_error)), max(worst_start, abs(start_error)), max(worst_curve, distance)
    step = (event["stagger"] or {}).get("step_ms_median")
    if step is None or abs(step - fm.ROWS["step"] * 1000) > 2:
        failures.append(f"stagger step {step} ms (truth {fm.ROWS['step'] * 1000:g})")
    table = [line for line in report.splitlines() if line.startswith("| M01.")]
    if len(table) != fm.ROWS["count"] or "split" in report:
        failures.append(f"report lists {len(table)} rows for {fm.ROWS['count']} elements (split mentioned: {'split' in report})")
    return (f"6 rows one row each, custom curve within {worst_curve:.3f}, |t50| <= {worst_t50:.1f} ms, |start| <= "
            f"{worst_start:.1f} ms, step {step} ms")


def split_check(report, motion, failures):
    """The control: transform easeOutCubic 500 ms and opacity linear 150 ms must split, each channel keeping its own
    fit, with the reason in its report row."""
    element = only_element(motion, "split", failures)
    if element is None:
        return "split"
    opacity = element["channels"].get("opacity", {}).get("own_fit")
    if (element.get("split") or {}).get("channel") != "opacity" or opacity is None:
        failures.append(f"split control not split on opacity: {element.get('split')}")
        return "split"
    fade = fm.SPLIT_CARD["fade"]
    if curve_distance(opacity["cubic_bezier"], fade["bezier"]) > CURVE_CLOSE or abs(opacity["duration_ms"] / (fade["duration"] * 1000) - 1) > 0.1 \
            or abs(opacity["start_s"] - fm.SPLIT_CARD["start"]) > 1 / fm.FPS:
        failures.append(f"split control opacity own fit {opacity} (truth linear 150 ms from {fm.SPLIT_CARD['start']})")
    rows = [line for line in report.splitlines() if line.startswith("| M01.1 ")]
    if len(rows) != 2 or "split: one timing misses opacity" not in rows[0]:
        failures.append(f"split control report rows {rows}")
    shared_notes = [line for line in report.splitlines() if line.startswith("- M01.1:") and "named curves tie" in line]
    if shared_notes:
        failures.append(f"split control notes a tie of the shared fit its rows do not show: {shared_notes}")
    return f"control split on opacity (own {opacity['name']} {opacity['duration_ms']:.0f} ms), 2 report rows with the reason"


def flat_list_check(motion, failures):
    """A list without row backgrounds (D2 follow-up): each row's parts start together; the stagger steps between start
    clusters, each within 5 ms of the truth, and no part reads as scaled or rotated (sub-pixel noise on small parts)."""
    events = analysed(motion)
    if len(events) != 1:
        failures.append(f"flat list: {len(events)} analysed events")
        return "flat list"
    event, truth = events[0], fm.FLAT_LIST
    steps, step = (event["stagger"] or {}).get("step_ms") or [], truth["step"] * 1000
    if len(steps) != truth["count"] - 1 or any(abs(s - step) > 5 for s in steps):
        failures.append(f"flat list stagger steps {steps} ms (truth {truth['count'] - 1} steps of {step:g})")
    turned = [el["id"] for el in event["elements"] if {"scale", "rotate"} & set(el["types"])]
    if turned:
        failures.append(f"flat list parts read as scaled or rotated: {turned}")
    return f"flat list {len(event['elements'])} parts, steps {steps} ms"


@kat("M20", "motion")
def m20_shared_channel_timing(ctx):
    """D2: a 6-row overlapping stagger (translateX + opacity, one custom curve, 30 fps, DPR 2) reads as one row per
    element with one shared timing and curve; a card whose opacity and transform run as two transitions splits; a flat
    list's stagger is measured between its rows, not between the parts of one row."""
    failures = []
    report, motion = report_and_motion(ctx, ctx.fixture("motion_rows_cfr.mp4", fm.build_rows), "M20-rows", "--dpr", fm.ROWS["dpr"])
    notes = [rows_check(report, motion, failures)]
    report, motion = report_and_motion(ctx, ctx.fixture("motion_split_card_cfr.mp4", fm.build_split_card), "M20-split")
    notes.append(split_check(report, motion, failures))
    _, motion = report_and_motion(ctx, ctx.fixture("motion_flat_list_cfr.mp4", fm.build_flat_list), "M20-flat", "--dpr",
                                  fm.FLAT_LIST["dpr"])
    notes.append(flat_list_check(motion, failures))
    return verdict(failures, "; ".join(notes))


@kat("M21", "motion", quick=True)
def m21_accelerate_ties(ctx):
    """D3: short modal exits (scale + opacity, 160-200 ms) under three accelerate curves. The report names the true
    curve (or one within 0.05 of it), or, when named curves of different shapes tie, the free fit with every tied name
    listed, the truth among them; t50 within one frame, one shared timing."""
    failures, notes = [], []
    for name, truth in fm.MODAL_EXITS.items():
        report, motion = report_and_motion(ctx, ctx.fixture(f"motion_modal_{name}_cfr.mp4", fm.build_modal_exit(name)), f"M21-{name}")
        element = only_element(motion, name, failures)
        if element is None:
            continue
        easing_ = element["easing"]
        ties = [tie["name"] for tie in easing_["near_ties"]]
        distance = curve_distance(easing_["cubic_bezier"], truth["bezier"])
        is_named_right = distance <= CURVE_CLOSE
        is_tie_listed = easing_.get("shape_tie") and name in ties
        row = next((line for line in report.splitlines() if line.startswith(f"| {element['id']} ")), "")
        if not (is_named_right or is_tie_listed):
            failures.append(f"{name}: reported {easing_['name']} {easing_['cubic_bezier']} ({distance:.3f} away), ties {ties}")
        elif is_tie_listed and not all(tie in row for tie in ties[:4]):
            failures.append(f"{name}: report row does not list the tied curves {ties}: {row}")
        t50_error = (element["timing"]["t50_s"] - truth_t50(truth["bezier"], truth["start"], truth["duration"])) * 1000
        if abs(t50_error) > 1000 / fm.FPS:
            failures.append(f"{name}: t50 {t50_error:+.1f} ms")
        if element["property_timing_differs"] or sorted(element["types"]) != ["fade", "scale"]:
            failures.append(f"{name}: types {element['types']}, split {element.get('split')}")
        notes.append(f"{name} -> {easing_['name'] if not is_tie_listed else 'free fit ~ ' + '/'.join(ties)} t50 {t50_error:+.1f} ms")
    return verdict(failures, "; ".join(notes))


def main_of(event):
    """The event's main element: the scroll element, else the largest box (a cut card's corner can add a small one)."""
    return max(event["elements"], key=lambda e: ("scroll" in e["channels"], e["bbox_src"][2] * e["bbox_src"][3]))


@kat("M22", "motion")
def m22_repeat_shared_fit(ctx):
    """D4: 9 identical carousel transitions (custom curve, sub-frame phases, pixel noise) read as one repeat group
    with one curve and duration and consistent starts: every start within 1 ms of truth, no outliers."""
    report, motion = report_and_motion(ctx, ctx.fixture("motion_carousel_cfr.mp4", fm.build_carousel), "M22")
    truth, failures = fm.CAROUSEL, []
    mains = [main_of(e) for e in analysed(motion) if e["elements"]]
    true_starts = truth["first"] + truth["period"] * np.arange(truth["count"])
    if len(mains) != truth["count"]:
        return verdict([f"{len(mains)} fitted transitions (truth {truth['count']})"], "")
    errors = (np.array([m["timing"]["start_s"] for m in mains]) - true_starts) * 1000
    if np.max(np.abs(errors)) > 1.0:
        failures.append(f"starts off by {np.round(errors, 1).tolist()} ms (spread {np.ptp(errors):.1f} ms)")
    curves = {tuple(m["easing"]["cubic_bezier"]) for m in mains}
    durations = sorted({m["timing"]["duration_ms"] for m in mains})
    if len(curves) != 1 or len(durations) != 1:
        failures.append(f"{len(curves)} curves and {len(durations)} durations ({durations[0]}-{durations[-1]} ms) across "
                        "identical repeats")
    repeats = motion["summary"].get("repeats") or []
    if len(repeats) != 1 or repeats[0]["count"] != truth["count"] or repeats[0].get("outliers"):
        failures.append(f"repeat groups {[(r['events'][0], r['count'], r.get('outliers')) for r in repeats]} (truth one "
                        f"group of {truth['count']}, no outliers)")
        return verdict(failures, "")
    repeat = repeats[0]
    duration, distance = repeat["duration_ms"], curve_distance(repeat["cubic_bezier"], truth["bezier"])
    if abs(duration / (truth["duration"] * 1000) - 1) > 0.03 or distance > CURVE_CLOSE:
        failures.append(f"shared {duration} ms {repeat['cubic_bezier']} ({distance:.3f} from the truth)")
    if abs(repeat["period_s"] - truth["period"]) > 0.001:
        failures.append(f"period {repeat['period_s']} s (truth {truth['period']:.4f})")
    if "one curve and duration fitted across" not in report:
        failures.append("report has no shared repeat line")
    return verdict(failures, f"{repeat['count']} repeats, one fit {duration} ms {repeat['cubic_bezier']} ({distance:.3f} "
                             f"from the truth), starts within {np.max(np.abs(errors)):.2f} ms, period {repeat['period_s']} s")


M23_CASES = (("bottom_vaul", False), ("bottom_vaul", True), ("right_expo", False), ("right_vaul", True))
M24_CASES = (("nav_badge", False), ("nav_badge", True), ("page_badge", True), ("chip", True))


def main_element(motion, label, failures):
    """The largest element of the one analysed event, or None with a failure."""
    events = analysed(motion)
    if len(events) != 1 or not events[0]["elements"]:
        failures.append(f"{label}: expected 1 event with elements, got {[len(e['elements']) for e in events]}")
        return None
    return max(events[0]["elements"], key=lambda e: e["bbox_src"][2] * e["bbox_src"][3])


def sheet_check(ctx, key, vfr, failures):
    """One long-tail sheet: pure translate across its edge, the start state just beyond it, the curve, start, t50 and
    duration of the truth, travel = the sheet's size, and nothing else read as moving (the strip of a top bar the sheet
    covers is at most an `other` exit)."""
    truth, label = fm.SHEETS[key], f"{key}{' vfr' if vfr else ''}"
    motion = motion_of(ctx, fixture(ctx, f"sheet_{key}", fm.build_sheet(key), vfr), f"M23-{label.replace(' ', '-')}")
    element = main_element(motion, label, failures)
    if element is None:
        return None
    moving = [e["id"] for e in analysed(motion)[0]["elements"] if e is not element and {"translate", "scale"} & set(e["types"])]
    clip = element.get("clip") or {}
    if element["types"] != ["translate"] or moving or clip.get("edge") != truth["edge"] or clip.get("reading") != "at-edge":
        failures.append(f"{label} types {element['types']}, other moving {moving}, clip {clip.get('edge')}/{clip.get('reading')}")
    check_curve(element, truth["name"], truth["bezier"], label, failures)
    start_error, duration_error = check_timing(element, truth, label, failures, 1, 0.5)
    t50_error = (element["timing"]["t50_s"] - truth_t50(truth["bezier"], truth["start"], truth["duration"])) * 1000
    travel_truth = float(np.hypot(*np.subtract(truth["hidden"], truth["rest"])))
    travel = float(np.hypot(*element["geometry"]["translate_px"]))
    if abs(t50_error) > 1 or abs(travel - travel_truth) > 1.5:
        failures.append(f"{label} t50 {t50_error:+.1f} ms, travel {travel:.1f} px (truth {travel_truth:g})")
    return f"{label} {element['easing']['name']} {start_error:+.1f} ms {duration_error:+.1f} %"


@kat("M23", "motion", quick=True)
def m23_long_tail_sheets(ctx):
    """W1: white sheets entering across the bottom and right frame edges under long-tail curves (the last 10 to 20 %
    below a pixel), CFR and VFR: a full-width sheet taller than half the frame, full-height ones beside a white top bar
    of their own colour. One pure slide each, timed to 1 ms and 0.5 %: a free curve that ends on a slope nobody sees
    fitted the bottom sheet 0.8 % short here (a Chrome-rendered drawer 2.6 %)."""
    failures, notes = [], []
    for key, vfr in M23_CASES:
        note = sheet_check(ctx, key, vfr, failures)
        notes += [note] if note else []
    return verdict(failures, "; ".join(notes))


def pop_check(ctx, key, vfr, failures):
    """One overshooting pop: a scale only, from its true start scale; easeOutBack by name, or a custom curve within
    0.05 of it with the true duration inside the range; start, t50 and duration of the truth. A badge growing from 0 is
    read as grown, not faded."""
    truth, label = fm.POPS[key], f"{key}{' vfr' if vfr else ''}"
    element = only_element(motion_of(ctx, fixture(ctx, f"pop_{key}", fm.build_pop(key), vfr), f"M24-{label.replace(' ', '-')}"),
                           label, failures)
    if element is None:
        return None
    scale_from = element["geometry"]["scale_from"]
    if element["types"] != ["scale"] or element["split"] or not element["easing"]["overshoot"] or abs(scale_from - truth["scale_from"]) > 0.02:
        failures.append(f"{label} types {element['types']}, split {bool(element['split'])}, overshoot "
                        f"{element['easing']['overshoot']}, scale from {scale_from} (truth {truth['scale_from']})")
    if truth["scale_from"] == 0 and not any(note.startswith("grew from nothing") for note in element["notes"]):
        failures.append(f"{label} growth from scale 0 not noted: {element['notes']}")
    check_curve(element, "custom" if element["easing"]["source"] == "custom" else truth["name"], truth["bezier"], label, failures)
    start_error, duration_error = check_timing(element, truth, label, failures, 3, 3)
    t50_error = (element["timing"]["t50_s"] - truth_t50(truth["bezier"], truth["start"], truth["duration"])) * 1000
    low, high = element["timing"]["duration_ms_range"]
    if abs(t50_error) > 2 or not low <= truth["duration"] * 1000 <= high:
        failures.append(f"{label} t50 {t50_error:+.1f} ms, duration range {low}-{high} (truth {truth['duration'] * 1000:g} ms)")
    return f"{label} {element['easing']['name']} {start_error:+.1f} ms {duration_error:+.1f} % t50 {t50_error:+.1f} ms"


@kat("M24", "motion", quick=True)
def m24_overshoot_pops(ctx):
    """W2: pops with an overshoot about the centre (easeOutBack, the peak between frames), CFR and VFR: badges growing
    from scale 0 (one in a nav bar 11 px from outline icons) and a chip growing from 0.6. Each reads as one scale from
    its true start, never as a fade or a rotation, and its reading holds the truth: without the flat-end refit the VFR
    badge and chip settled on curves ending at y2 0.97, 3.3 and 3.9 % long."""
    failures, notes = [], []
    for key, vfr in M24_CASES:
        note = pop_check(ctx, key, vfr, failures)
        notes += [note] if note else []
    return verdict(failures, "; ".join(notes))


M25_CASES = (("text_sheet", fm.TEXT_SHEET, fm.build_text_sheet), ("shade_quint", fm.SHADES["quint"], fm.build_shade("quint")),
             ("shade_expo", fm.SHADES["expo"], fm.build_shade("expo")), ("scrim_sheet", fm.SCRIM_SHEET, fm.build_scrim_sheet),
             ("exit_sheet", fm.EXIT_SHEET, fm.build_exit_sheet))
# the exit's last 38 px leave view too thin to track: its curve is not pinned there, the range holds the truth
EXIT_CURVE_UNPINNED = ("exit_sheet",)
SCRIM_TRAVEL_PX = 2.5       # a page text line touching the sheet's rest edge flickers with it and joins its extent


def backdrop_check(elements_, truth, label, failures):
    """The scrim as one element of its own: a fade over the page, appearing, started with the dimming (within a frame)."""
    backdrops = [e for e in elements_ if any(note.startswith("backdrop (scrim)") for note in e["notes"])]
    if len(backdrops) != 1 or backdrops[0]["types"] != ["fade"] or not backdrops[0]["appears"]:
        failures.append(f"{label} backdrop {[(e['id'], e['types'], e['appears']) for e in backdrops]} (truth one fade in)")
        return
    start_error = (backdrops[0]["timing"]["start_s"] - truth["start"]) * 1000
    if abs(start_error) > 1000 / fm.FPS:
        failures.append(f"{label} backdrop start {start_error:+.1f} ms")


def page_sheet_check(ctx, key, truth, build, vfr, failures):
    """One sheet or shade over a page of text lines: the only element (plus the backdrop when the page dims), a pure
    slide across its edge read at-edge, timed like M23 (a named curve) or by start, t50 and range (a custom one), travel
    its size. The text lines it covers or uncovers are no elements of their own."""
    label = f"{key}{' vfr' if vfr else ''}"
    motion = motion_of(ctx, fixture(ctx, key, build, vfr), f"M25-{label.replace(' ', '-')}")
    events = analysed(motion)
    elements_ = events[0]["elements"] if len(events) == 1 else []
    expected = 2 if "scrim" in truth else 1
    if len(events) != 1 or len(elements_) != expected:
        failures.append(f"{label}: expected 1 event with {expected} element(s), got "
                        f"{[[(el['id'], el['types'], el['bbox_src']) for el in e['elements']] for e in events]}")
        return None
    if "scrim" in truth:
        backdrop_check(elements_, truth, label, failures)
    element = max(elements_, key=lambda e: ("translate" in e["types"], e["bbox_src"][2] * e["bbox_src"][3]))
    clip = element.get("clip") or {}
    if element["types"] != ["translate"] or clip.get("edge") != truth["edge"] or clip.get("reading") != "at-edge":
        failures.append(f"{label} types {element['types']}, clip {clip.get('edge')}/{clip.get('reading')}")
    if truth["name"] == "custom":
        start_error, duration_error = check_custom_timing(element, truth, label, failures)
        if key not in EXIT_CURVE_UNPINNED:
            check_curve(element, "custom", truth["bezier"], label, failures)
    else:
        check_curve(element, truth["name"], truth["bezier"], label, failures)
        start_error, duration_error = check_timing(element, truth, label, failures, 1, 0.5)
        t50_error = (element["timing"]["t50_s"] - truth_t50(truth["bezier"], truth["start"], truth["duration"])) * 1000
        if abs(t50_error) > 1:
            failures.append(f"{label} t50 {t50_error:+.1f} ms")
    travel_truth = float(np.hypot(*np.subtract(truth["hidden"], truth["rest"])))
    travel = float(np.hypot(*element["geometry"]["translate_px"]))
    if abs(travel - travel_truth) > (SCRIM_TRAVEL_PX if "scrim" in truth else 1.5):
        failures.append(f"{label} travel {travel:.1f} px (truth {travel_truth:g})")
    return f"{label} {element['easing']['name']} {start_error:+.1f} ms {duration_error:+.1f} %"


@kat("M25", "motion")
def m25_sheets_over_content(ctx):
    """W1 follow-ups: a sheet taller than half the frame and two top shades (easeOutQuint, easeOutExpo; their leading
    76 px hold only a grab handle) slide over a page of text lines, a sheet slides in while the page dims to 45 % black
    on its curve, and a drawer closes across the bottom edge under a fast-start long-tail curve; CFR and VFR. One slide
    each (and the backdrop), the covered lines no elements: they read as scale to 0, fades and a stagger, the shade's
    duration as capped short, the dimmed sheet as its own avatars, and the closing drawer not at all."""
    failures, notes = [], []
    for key, truth, build in M25_CASES:
        for vfr in (False, True):
            note = page_sheet_check(ctx, key, truth, build, vfr, failures)
            notes += [note] if note else []
    return verdict(failures, "; ".join(notes))


M26_CASES = (("avatar_badge", fm.ANCHORED_POPS["avatar_badge"], fm.build_anchored_pop("avatar_badge")),
             ("icon_badge_exit", fm.ANCHORED_POPS["icon_badge_exit"], fm.build_anchored_pop("icon_badge_exit")),
             ("card_dot", fm.CARD_DOT, fm.build_card_dot))


def event_near(motion, centre):
    """The analysed event whose element lies nearest `centre`, and that element."""
    pairs = [(e, el) for e in analysed(motion) for el in e["elements"]]
    return min(pairs, key=lambda pair: np.hypot(pair[1]["bbox_src"][0] + pair[1]["bbox_src"][2] / 2 - centre[0],
                                                pair[1]["bbox_src"][1] + pair[1]["bbox_src"][3] / 2 - centre[1]), default=(None, None))


def anchored_pop_check(ctx, key, truth, build, vfr, failures):
    """A pop that covers content (an avatar's or icon's corner, a card): one element in its event, a scale only from
    (to) 0 with the grew-from-nothing note, the curve within 0.05, start within 3 ms, t50 within 2 ms, the true duration
    inside the range."""
    label = f"{key}{' vfr' if vfr else ''}"
    motion = motion_of(ctx, fixture(ctx, key, build, vfr), f"M26-{label.replace(' ', '-')}")
    event, element = event_near(motion, truth["centre"])
    if event is None or len(event["elements"]) != 1:
        failures.append(f"{label}: expected its event to hold 1 element, got "
                        f"{None if event is None else [(el['id'], el['types']) for el in event['elements']]}")
        return None
    geometry = element["geometry"]
    scale_to = truth.get("scale_to", 1.0)
    if element["types"] != ["scale"] or abs(geometry["scale_from"] - truth["scale_from"]) > 0.02 \
            or abs(geometry["scale_to"] - scale_to) > 0.02:
        failures.append(f"{label} types {element['types']}, scale {geometry['scale_from']}->{geometry['scale_to']} "
                        f"(truth {truth['scale_from']}->{scale_to})")
    if not any(note.startswith("grew from nothing") for note in element["notes"]):
        failures.append(f"{label} scale 0 not noted: {[note[:50] for note in element['notes']]}")
    name = "custom" if element["easing"]["source"] == "custom" else truth["name"]
    check_curve(element, name, truth["bezier"], label, failures)
    start_error, duration_error = check_timing(element, truth, label, failures, 3, 100)
    t50_error = (element["timing"]["t50_s"] - truth_t50(truth["bezier"], truth["start"], truth["duration"])) * 1000
    low, high = element["timing"]["duration_ms_range"]
    if abs(t50_error) > 2 or not low <= truth["duration"] * 1000 <= high:
        failures.append(f"{label} t50 {t50_error:+.1f} ms, duration range {low}-{high} (truth {truth['duration'] * 1000:g} ms)")
    return f"{label} {element['easing']['name']} {start_error:+.1f} ms {duration_error:+.1f} % t50 {t50_error:+.1f} ms"


def tooltip_check(ctx, vfr, failures):
    """A scale + fade tooltip over a text line: both channels, scale from its true value (the text line under it once
    held the template's pad), no growth from 0, a curve within 0.05, t50 and duration within a frame."""
    truth, label = fm.TOOLTIP, f"tooltip{' vfr' if vfr else ''}"
    element = only_element(motion_of(ctx, fixture(ctx, "tooltip", fm.build_tooltip, vfr), f"M26-{label.replace(' ', '-')}"),
                           label, failures)
    if element is None:
        return None
    scale_from = element["geometry"]["scale_from"]
    if sorted(element["types"]) != ["fade", "scale"] or abs(scale_from - truth["scale_from"]) > 0.02 \
            or any(note.startswith("grew from nothing") for note in element["notes"]):
        failures.append(f"{label} types {element['types']}, scale from {scale_from} (truth {truth['scale_from']})")
    if curve_distance(element["easing"]["cubic_bezier"], truth["bezier"]) > CURVE_CLOSE:
        failures.append(f"{label} curve {element['easing']['name']} {element['easing']['cubic_bezier']}")
    timing, frame_ms = element["timing"], 1000 / fm.FPS
    t50_error = (timing["t50_s"] - truth_t50(truth["bezier"], truth["start"], truth["duration"])) * 1000
    duration_error = timing["duration_ms"] - truth["duration"] * 1000
    if abs(t50_error) > frame_ms or abs(duration_error) > frame_ms:
        failures.append(f"{label} t50 {t50_error:+.1f} ms, duration {duration_error:+.1f} ms")
    return f"{label} {element['easing']['name']} scale from {scale_from} t50 {t50_error:+.1f} ms dur {duration_error:+.1f} ms"


@kat("M26", "motion")
def m26_pops_over_content(ctx):
    """W2 follow-ups: a badge growing on an avatar's corner, one shrinking off a nav icon's corner, a dot growing on a
    white card while a label fades in far away, CFR and VFR: each one scale from (to) 0, not split into translate, scale
    and rotation, not dropped as static; and a scale + fade tooltip over a text line."""
    failures, notes = [], []
    for key, truth, build in M26_CASES:
        for vfr in (False, True):
            note = anchored_pop_check(ctx, key, truth, build, vfr, failures)
            notes += [note] if note else []
    for vfr in (False, True):
        note = tooltip_check(ctx, vfr, failures)
        notes += [note] if note else []
    return verdict(failures, "; ".join(notes))


M27_CASES = (("iso_button", False), ("iso_badge", True))
# alone, CFR: a colour change in place never reads as a stretch, and a slow start is timed from the rest before it
M27_ALONE = (("bordered_button", fm.BORDERED_BUTTON), ("ghost_button", fm.GHOST_BUTTON), ("dark_button", fm.DARK_BUTTON))


def colour_button_check(motion, truth, label, failures):
    """(the button's event, a note): its event holds one element, a colour change, the truth's curve by name, confidence
    high, start within 5 ms and duration within 3 % of the truth."""
    x, y, w, h = truth["box"]
    event, element = event_near(motion, (x + w / 2, y + h / 2))
    if event is None or len(event["elements"]) != 1:
        failures.append(f"{label}: expected the button's event to hold 1 element, got "
                        f"{None if event is None else [(el['id'], el['types']) for el in event['elements']]}")
        return event, None
    if element["types"] != ["color"] or element["confidence"] != "high":
        failures.append(f"{label} button types {element['types']}, confidence {element['confidence']} (truth a colour "
                        "change only)")
    check_name(element, truth["name"], f"{label} button", failures)
    start_error, duration_error = check_timing(element, truth, f"{label} button", failures, 5, 3)
    return event, (f"{label} {'+'.join(element['types'])} {element['easing']['name']} {start_error:+.1f} ms "
                   f"{duration_error:+.1f} % {element['confidence']}")


def nearby_badge_check(motion, button_event, label, failures):
    """The badge popping beside the button later: a fitted motion event of its own (no micro events), one scale from
    0 noted as grown, an overshoot, the curve within 0.05, start within 3 ms, t50 within 2 ms, the true duration inside
    the range."""
    truth = fm.ISO_BADGE
    event, element = event_near(motion, truth["centre"])
    micro = motion["summary"]["micro_events"]
    if event is None or event is button_event or event["class"] != "motion" or micro:
        nearest = None if event is None else (event["id"], event["class"])
        failures.append(f"{label} badge not fitted as its own motion event: nearest {nearest}, micro events {micro}")
        return None
    grown = any(note.startswith("grew from nothing") for note in element["notes"])
    if element["types"] != ["scale"] or not element["easing"]["overshoot"] or not grown:
        failures.append(f"{label} badge types {element['types']}, overshoot {element['easing']['overshoot']}, "
                        f"grown {grown}")
    check_curve(element, "custom" if element["easing"]["source"] == "custom" else truth["name"], truth["bezier"],
                f"{label} badge", failures)
    start_error, duration_error = check_timing(element, truth, f"{label} badge", failures, 3, 3)
    t50_error = (element["timing"]["t50_s"] - truth_t50(truth["bezier"], truth["start"], truth["duration"])) * 1000
    low, high = element["timing"]["duration_ms_range"]
    if abs(t50_error) > 2 or not low <= truth["duration"] * 1000 <= high:
        failures.append(f"{label} badge t50 {t50_error:+.1f} ms, duration range {low}-{high} "
                        f"(truth {truth['duration'] * 1000:g} ms)")
    return f"badge {element['easing']['name']} {start_error:+.1f} ms {duration_error:+.1f} %"


@kat("M27", "motion", quick=True)
def m27_colour_in_place(ctx):
    """A button whose fill changes in place to a colour of the same BT.709 luma, alone and with a badge popping beside
    it later, CFR and VFR: one colour element, linear, confidence high, and the badge a fitted pop from scale 0. The
    ring of the button's edge looks alike at both rests; left out of its walk as static content, ECC read the colour
    change as a 1 to 3 % scale with a linear() curve. Alone, CFR: such a fill inside a static 6 px border (read as a
    stretch while the border's outer edge was left out), a ghost button filling (read as a stretch or a fade, one row
    per rest frame) and a dark button lightening under ease (measured from its window's first frame, 6 % into it: +19
    ms, easeOutCubic), each one colour element, the truth's curve, confidence high."""
    failures, notes = [], []
    for key, with_badge in M27_CASES:
        for vfr in (False, True):
            label = f"{key}{' vfr' if vfr else ''}"
            video = fixture(ctx, key, fm.build_iso_button(with_badge), vfr)
            motion = motion_of(ctx, video, f"M27-{label.replace(' ', '-')}")
            button_event, note = colour_button_check(motion, fm.ISO_BUTTON, label, failures)
            badge_note = nearby_badge_check(motion, button_event, label, failures) if with_badge else None
            notes.append(", ".join(filter(None, (note, badge_note))))
    for key, truth in M27_ALONE:
        motion = motion_of(ctx, fixture(ctx, key, fm.build_colour_button(truth)), f"M27-{key}")
        notes.append(colour_button_check(motion, truth, key, failures)[1])
    return verdict(failures, "; ".join(filter(None, notes)))
