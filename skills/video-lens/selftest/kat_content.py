"""Content KATs (spec 10.2): C1 survey on D2 synth_talk, C2 D3 talk10m slides, C3 Korean OCR exactness,
C4 subtitle legibility by glyph height. The stage runs directly on a prepare_run Run (INTERFACES 8)."""
import argparse

import fixtures_content as fx
from katlib import kat, verdict
from vl import decode, ocr, sheets_content, survey
from vl.cli import prepare_run
from vl.context import Run
from vl.probe import probe

SYNTH_FRAME_S = 1 / fx.SYNTH_FPS
TALK_KEY_WINDOW_S = 1.0
TALK_MAX_EXTRA = 6
TALK_SURVEY_MAX_S = 15.0
DECK_HOLD_TOLERANCE_S = 0.2
SUB_WIDTHS = (640, 960, 1280, 1920)
SUB_MIN_GLYPH_PX = 16
OCR_LANGS = ["ko-KR", "en-US"]


def synth_talk(ctx):
    return ctx.fixture("content_synth_talk.mp4", fx.build_synth_talk)


def content_run(ctx, video, name, *flags):
    """Runs the content stage as `analyze --mode content` would; returns (run, content section)."""
    run = prepare_run(["analyze", str(video), "--out", str(ctx.out_dir(name)), "--mode", "content", *flags])
    run.set_mode("content", "--mode content")
    run.analysis["content"] = survey.analyze_content(run)
    return run, run.analysis["content"]


def near(value, target, tolerance):
    return abs(value - target) <= tolerance + 1e-6


def synth_timeline_failures(content, frame_s):
    """Cuts {6.0, 15.5} +-1 frame, one transition inside [11.2, 11.8], one transient per flash."""
    failures = []
    cut_starts = [s["start_s"] for s in content["shots"][1:]]
    if len(cut_starts) != len(fx.SYNTH_CUTS) or not all(near(a, b, frame_s) for a, b in zip(cut_starts, fx.SYNTH_CUTS)):
        failures.append(f"cuts {cut_starts} != {list(fx.SYNTH_CUTS)} +-1 frame")
    transitions = [e for e in content["events"] if e["kind"] == "transition"]
    last_active = [e["end_s"] - frame_s for e in transitions]
    if len(transitions) != 1 or not (11.2 <= transitions[0]["start_s"] and last_active[0] <= 11.8 + 1e-6):
        failures.append(f"transitions {[(e['start_s'], e['end_s']) for e in transitions]}, want one inside [11.2, 11.8]")
    transients = [e["start_s"] for e in content["events"] if e["kind"] == "transient"]
    if len(transients) != len(fx.SYNTH_FLASHES) or not all(near(a, b, frame_s) for a, b in zip(transients, fx.SYNTH_FLASHES)):
        failures.append(f"transients at {transients}, want {list(fx.SYNTH_FLASHES)}")
    return failures


def synth_keyframe_failures(content):
    """Exactly 4 keyframes, none during a flash, K004 same_as K001 and no other repeat."""
    keyframes = content["keyframes"]
    failures = []
    if len(keyframes) != 4:
        failures.append(f"{len(keyframes)} keyframes at {[k['t'] for k in keyframes]}, want 4")
    in_flash = [k["id"] for k in keyframes if any(f <= k["t"] < f + fx.SYNTH_FLASH_S for f in fx.SYNTH_FLASHES)]
    if in_flash:
        failures.append(f"keyframes during a flash: {in_flash}")
    repeats = {k["id"]: k["same_as"] for k in keyframes if k["same_as"]}
    if repeats != {"K004": "K001"}:
        failures.append(f"same_as {repeats}, want K004 -> K001")
    return failures


def view_failures(run, content):
    """kf sheet written, overview cells in time order without repeats, zoom by segment and by range from OUT."""
    failures = []
    sheets = [v for v in run.views if v["kind"] == "keyframes"]
    shown = [k for k in content["keyframes"] if not k["same_as"]]
    if len(sheets) != 1 or sheets[0]["cells"] != len(shown) or any(k["sheet"] != sheets[0]["file"] for k in shown):
        failures.append(f"keyframe sheets {sheets}")
    run.write_analysis()
    reopened = Run.open_from_out(run.out, argparse.Namespace())
    cells = sheets_content.overview_cells(reopened)
    if len(cells) != len(shown) or any(label.split()[0] != k["id"] for (_, label), k in zip(cells, shown)):
        failures.append(f"overview cells {[label for _, label in cells]}")
    zoomed = sheets_content.zoom_segment(reopened, "S02", 12) + sheets_content.zoom_range(reopened, 12.4, 12.7, 12)
    if [v["cells"] for v in zoomed] != [12, 9] or not all((run.out / v["file"]).is_file() for v in zoomed):
        failures.append(f"zoom views {[(v['file'], v['cells']) for v in zoomed]}, want 12 and 9 cells")
    return failures


@kat("C1", "content", quick=True)
def c1_synth_talk(ctx):
    run, content = content_run(ctx, synth_talk(ctx), "C1", "--ocr", "off")
    failures = synth_timeline_failures(content, SYNTH_FRAME_S) + synth_keyframe_failures(content) + view_failures(run, content)
    transition = next((e for e in content["events"] if e["kind"] == "transition"), {})
    return verdict(failures, f"cuts {[s['start_s'] for s in content['shots'][1:]]}, transition "
                             f"{transition.get('start_s')}-{transition.get('end_s')}, 3 transients, keyframes "
                             f"{[k['t'] for k in content['keyframes']]}, K004 same_as K001, sheets and zooms ok")


def talk_keyframe_coverage(keyframes):
    """(slides with a keyframe within +1.0 s of their start, keyframes matching no slide start)."""
    starts = [number * fx.TALK_SLIDE_S for number in range(fx.TALK_SLIDES)]
    covered = [s for s in starts if any(s <= k["t"] <= s + TALK_KEY_WINDOW_S for k in keyframes)]
    extra = [k for k in keyframes if not any(s <= k["t"] <= s + TALK_KEY_WINDOW_S for s in starts)]
    return covered, extra


@kat("C2", "content")
def c2_talk10m(ctx):
    """Default keyframe budget: the soft budget for 10 min is 32, but slides held >= 2 s are kept up to the cap."""
    video = ctx.fixture("content_talk10m.mp4", fx.build_talk10m)
    run, content = content_run(ctx, video, "C2")
    keyframes = content["keyframes"]
    covered, extra = talk_keyframe_coverage(keyframes)
    texts = {e["id"]: e["text"] for e in content["text"]["events"]}
    code_hits = 0
    for number, code in enumerate(fx.talk_codes()):
        start = number * fx.TALK_SLIDE_S
        slide_keys = [k for k in keyframes if start <= k["t"] <= start + TALK_KEY_WINDOW_S]
        code_hits += any(str(code) in texts.get(i, "") for k in slide_keys for i in k["text_ids"])
    failures = []
    if len(covered) != fx.TALK_SLIDES:
        failures.append(f"slide starts with a keyframe {len(covered)}/{fx.TALK_SLIDES}")
    if len(extra) > TALK_MAX_EXTRA:
        failures.append(f"{len(extra)} extra keyframes > {TALK_MAX_EXTRA}: {[k['t'] for k in extra]}")
    if code_hits != fx.TALK_SLIDES:
        failures.append(f"slide keyframes whose OCR holds the code {code_hits}/{fx.TALK_SLIDES}")
    if run.timing["survey"] > TALK_SURVEY_MAX_S:
        failures.append(f"survey {run.timing['survey']:.1f} s > {TALK_SURVEY_MAX_S:.0f} s")
    params = run.params["content"]
    return verdict(failures, f"{len(covered)}/40 slides, {len(extra)} extra, codes {code_hits}/40, survey "
                             f"{run.timing['survey']:.1f} s, ocr {run.timing['ocr']:.1f} s ({content['ocr']['samples']} samples); "
                             f"default budget {params['max_keyframes']} (cap {params['keyframe_cap']} for held slides)")


@kat("C10", "content")
def c10_template_deck(ctx):
    """Slides that share one template and differ only in a title word and a code: every slide keyframed and its
    code read, each held for its full duration, and only the true repeat marked same_as."""
    video = ctx.fixture("content_template_deck.mp4", fx.build_template_deck)
    run, content = content_run(ctx, video, "C10")
    keyframes = content["keyframes"]
    texts = {e["id"]: e["text"] for e in content["text"]["events"]}
    failures, first_keys = [], []
    for number in range(fx.DECK_SLIDES):
        start = number * fx.DECK_SLIDE_S
        slide_keys = [k for k in keyframes if start <= k["t"] <= start + TALK_KEY_WINDOW_S]
        if not slide_keys:
            failures.append(f"slide {number + 1} at {start:g} s has no keyframe")
            continue
        first_keys.append(slide_keys[0])
        if not any(fx.deck_code(number) in texts.get(i, "") for k in slide_keys for i in k["text_ids"]):
            failures.append(f"slide {number + 1}: {fx.deck_code(number)} not read at its keyframe")
        if abs(slide_keys[0]["hold_s"] - fx.DECK_SLIDE_S) > DECK_HOLD_TOLERANCE_S:
            failures.append(f"slide {number + 1}: hold {slide_keys[0]['hold_s']} s, want {fx.DECK_SLIDE_S:g}")
    by_slide = {number: k["id"] for number, k in enumerate(first_keys)} if len(first_keys) == fx.DECK_SLIDES else {}
    want = {by_slide[repeat]: by_slide[original] for repeat, original in fx.DECK_REPEAT.items()} if by_slide else None
    repeats = {k["id"]: k["same_as"] for k in keyframes if k["same_as"]}
    if want is not None and repeats != want:
        failures.append(f"same_as {repeats}, want {want}")
    details = [k["t"] for k in keyframes if k["reason"] == "detail"]
    if not details:
        failures.append("no detail keyframe: the fixture no longer hides a slide swap from the 160 px survey")
    return verdict(failures, f"{len(first_keys)}/{fx.DECK_SLIDES} slides keyframed with their code, holds "
                             f"{sorted({k['hold_s'] for k in first_keys})} s, same_as {repeats}, detail keyframes at {details}")


def ocr_texts(content):
    return {e["text"] for e in content["text"]["events"]} | {p["text"] for p in content["text"]["persistent"]}


@kat("C3", "content", quick=True)
def c3_korean_ocr(ctx):
    """Every source line comes out of the pipeline (export, Vision, text events) character for character."""
    _, synth = content_run(ctx, synth_talk(ctx), "C3-synth")
    _, slide = content_run(ctx, ctx.fixture("content_ko_slide.mp4", fx.build_ko_slide), "C3-slide")
    expected = [line for _, lines in fx.SYNTH_SLIDES.values() for line in lines]
    failures = [f"synth_talk missing {line!r}" for line in expected if line not in ocr_texts(synth)]
    failures += [f"ko_slide missing {text!r}" for _, text in fx.KO_SLIDE_LINES if text not in ocr_texts(slide)]
    return verdict(failures, f"{len(expected)}/{len(expected)} synth_talk lines and {len(fx.KO_SLIDE_LINES)}/"
                             f"{len(fx.KO_SLIDE_LINES)} slide lines exact, incl. '2026년 9월 27일', '12,900원', '1,284명'")


def subtitle_exports(ctx, video):
    """{(points, width): image path}: the middle frame of each one-second subtitle, exported at each width."""
    _, packet_times = probe(video)
    jobs = {}
    for second, points in enumerate(fx.SUB_POINTS):
        t = float(packet_times[min(range(len(packet_times)), key=lambda i: abs(packet_times[i] - second - 0.5))])
        for width in SUB_WIDTHS:
            jobs[(points, width)] = decode.ExportJob(t, ctx.out_dir("C4") / f"sub_{points}_{width}.png", width=width)
    decode.export_frames(video, list(jobs.values()))
    return {key: job.out for key, job in jobs.items()}


@kat("C4", "content")
def c4_subtitle_legibility(ctx):
    """Exact text whenever the glyph height in the OCR image is >= 16 px (smaller sizes are reported, not required)."""
    video = ctx.fixture("content_subtitles.mp4", fx.build_subtitles)
    glyphs = fx.subtitle_glyphs()
    paths = subtitle_exports(ctx, video)
    results = dict(zip(paths, ocr.recognize_files(list(paths.values()), OCR_LANGS)))
    failures, cells = [], []
    for (points, width), result in results.items():
        glyph = glyphs[points] * width / fx.SUB_SIZE[0]
        read = " ".join(line["text"] for line in result.get("lines", []))
        is_exact = read == fx.SUB_TEXT
        cells.append(f"{points}pt@{width} {glyph:.0f}px {'ok' if is_exact else 'x'}")
        if glyph >= SUB_MIN_GLYPH_PX and not is_exact:
            failures.append(f"{points}pt at {width} px ({glyph:.1f} px glyphs) read {read!r}")
    required = sum(glyphs[p] * w / fx.SUB_SIZE[0] >= SUB_MIN_GLYPH_PX for p, w in results)
    return verdict(failures, f"{required - len(failures)}/{required} required exact; " + ", ".join(cells))
