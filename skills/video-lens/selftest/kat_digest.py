"""Scene digest KATs: C11 a slide talk with speech gives one scene per slide, each with its own frame, on-screen text
and speech; captions round-trip into digest.md and digest.html, a missing or unknown caption is refused, captions left
from other scenes are set aside, and a lower --scenes merges across the weaker boundary. C12 a yt-dlp info JSON with
chapters makes the scenes follow the chapters, with YouTube deep links, each frame showing the slide on screen longest
in its chapter. C11 and C12 run vl.py as the user would; C13 checks the chrome and frame rules on made-up scenes."""
import json
import re
from types import SimpleNamespace

import cv2
import numpy as np

import fixtures_audio as fa
import fixtures_content as fx
from fixtures_core import build_copy
from kat_content import synth_talk
from katlib import FAIL, SKIP, kat, verdict
from vl import digest, views

FRAME_S = 1 / fx.SYNTH_FPS
SLIDE_STARTS = (0.0, fx.SYNTH_CUTS[0], fx.SYNTH_DISSOLVE[1], fx.SYNTH_CUTS[1])   # slide 3 settles after the dissolve
SLIDE_TITLES = tuple(fx.SYNTH_SLIDES[number][1][0] for number in (1, 2, 3, 1))    # slide 4 repeats slide 1
CAPTION_EXTRA = "\n둘째 줄 <b>&"                                                     # must come out escaped
# The middle chapter holds slide 3 for 3.75 s and slide 1's return for 0.5 s (whose whole hold is 4.5 s): its frame
# must show slide 3. Slides are told apart by their background colour.
CHAPTERS = ((0.0, 9.0, "Opening"), (9.0, 16.0, "가운데 부분"), (16.0, 20.0, "Closing"))
CHAPTER_SLIDES = (1, 3, 1)
VIDEO_ID = "vlDigestK01"
INFO_TITLE = "video-lens 챕터 테스트"
SHEET_LINE = re.compile(r"^- (?P<path>/\S+sheet_01\.jpg) · (?P<w>\d+)x(?P<h>\d+) · (?P<tok>[\d,]+) tok · S01-S0(?P<last>\d)$", re.M)


def build_digest_talk(synth_path):
    """The content synth_talk slides (Korean titles) with the audio fixture's speech track (same cuts)."""
    return lambda path: fa.mux_audio(synth_path, fa.talk_track(), path)


def build_info_json(path):
    info = {"id": VIDEO_ID, "extractor_key": "Youtube", "title": INFO_TITLE, "uploader": "video-lens selftest",
            "webpage_url": f"https://www.youtube.com/watch?v={VIDEO_ID}", "upload_date": "20260930",
            "chapters": [{"start_time": a, "end_time": b, "title": title} for a, b, title in CHAPTERS]}
    path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def run_vl(ctx, *args):
    """stdout of a vl.py command that must succeed; raises with its error line otherwise."""
    result = ctx.vl(*args)
    if result.returncode != 0:
        raise AssertionError(f"vl.py {args[0]} exit {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def read_digest(out):
    return json.loads((out / "digest" / "digest.json").read_text(encoding="utf-8"))


def boundary_failures(scenes, expected_starts, dissolve_index=None):
    """Scene starts at the expected times within one frame; the scene after a dissolve starts inside it or on the first
    frame after it."""
    failures = []
    if len(scenes) != len(expected_starts):
        return [f"{len(scenes)} scenes at {[s['start_s'] for s in scenes]}, want {len(expected_starts)}"]
    for index, (scene, start) in enumerate(zip(scenes, expected_starts)):
        low = fx.SYNTH_DISSOLVE[0] if index == dissolve_index else start - FRAME_S
        if not low - 1e-6 <= scene["start_s"] <= start + FRAME_S + 1e-6:
            failures.append(f"{scene['id']} starts {scene['start_s']}, want {start:g} (+-1 frame)")
    return failures


def image_failures(out, scenes, frame_size):
    """One frame per scene at the source size, and the numbered sheet, all within the image limits."""
    failures = []
    frames = sorted(p.name for p in (out / "digest" / "frames").iterdir())
    if frames != [f"{s['id']}.jpg" for s in scenes]:
        failures.append(f"frames {frames}, want one per scene")
    for path in [out / s["frame"] for s in scenes] + sorted((out / "digest").glob("sheet_*.jpg")):
        image = cv2.imread(str(path))
        height, width = image.shape[:2]
        if max(width, height) > views.MAX_SIDE_PX or views.visual_tokens(width, height) > views.MAX_VISUAL_TOKENS:
            failures.append(f"{path.name} {width}x{height} breaks the image limit")
        if path.parent.name == "frames" and (width, height) != frame_size:
            failures.append(f"{path.name} is {width}x{height}, want the source {frame_size[0]}x{frame_size[1]}")
    return failures


def text_failures(scenes):
    """Each scene's OCR holds its own slide title as the headline and no other slide's title."""
    failures = []
    for scene, title in zip(scenes, SLIDE_TITLES):
        others = [other for other in set(SLIDE_TITLES) - {title} if other in scene["ocr"]]
        if title not in scene["ocr"] or scene["headline"] != title or others:
            failures.append(f"{scene['id']} ocr {scene['ocr']} headline {scene['headline']!r}, want {title!r} only")
    return failures


def speech_failures(scenes, spoken):
    """Every transcript sentence sits in the scene of the slide it was said over (truth: the slide spans), and the
    silent slide 3 gets none."""
    failures = []
    for start, end, text in spoken:
        slide = max(i for i, slide_start in enumerate(SLIDE_STARTS) if slide_start <= (start + end) / 2)
        holders = [scene["id"] for scene in scenes if text in scene["speech"]]
        if holders != [scenes[slide]["id"]]:
            failures.append(f"sentence at {start:.2f} s {text!r} in {holders}, want {scenes[slide]['id']}")
    if scenes[2]["speech"]:
        failures.append(f"silent slide 3 got speech {scenes[2]['speech']}")
    return failures


def caption_round_trip_failures(ctx, out, scene_ids):
    """Captions render into digest.md (relative frames) and a self-contained, escaped digest.html."""
    captions = {scene_id: f"{scene_id} 장면 설명{CAPTION_EXTRA}" for scene_id in scene_ids}
    path = out / "digest" / "captions.json"
    path.write_text(json.dumps(captions, ensure_ascii=False), encoding="utf-8")
    run_vl(ctx, "digest", out, "--captions", path)
    md = (out / "digest" / "digest.md").read_text(encoding="utf-8")
    page = (out / "digest" / "digest.html").read_text(encoding="utf-8")
    failures = []
    for scene_id in scene_ids:
        if f"{scene_id} 장면 설명" not in md or f"(frames/{scene_id}.jpg)" not in md:
            failures.append(f"digest.md lacks the caption or frame link of {scene_id}")
        if f"{scene_id} 장면 설명\n둘째 줄 &lt;b&gt;&amp;" not in page:
            failures.append(f"digest.html lacks the escaped caption of {scene_id}")
    embedded = page.count('src="data:image/jpeg;base64,')
    if embedded != len(scene_ids) or 'src="http' in page or "<b>&" in page:
        failures.append(f"digest.html: {embedded} embedded frames for {len(scene_ids)} scenes, or an external or raw source")
    if "prefers-color-scheme: dark" not in page or "@media print" not in page:
        failures.append("digest.html has no dark or print style")
    if "&t=" in md:
        failures.append("a local file got YouTube deep links")
    return failures


def refusal_failures(ctx, out, scene_ids):
    """exit 2 with one line naming the missing (or unknown) scene."""
    failures = []
    complete = {scene_id: "설명" for scene_id in scene_ids}
    cases = (("missing", {k: v for k, v in complete.items() if k != "S03"}, "S03"), ("unknown", {**complete, "S09": "x"}, "S09"))
    for label, captions, named in cases:
        path = out / f"captions_{label}.json"
        path.write_text(json.dumps(captions, ensure_ascii=False), encoding="utf-8")
        result = ctx.vl("digest", out, "--captions", path)
        lines = result.stderr.strip().splitlines()
        if result.returncode != 2 or len(lines) != 1 or named not in lines[0]:
            failures.append(f"{label} caption: exit {result.returncode}, stderr {result.stderr.strip()!r}")
    return failures


def stale_caption_failures(ctx, out):
    """A rebuild with the same scenes keeps captions.json; one whose scenes moved (same ids, other times, as after
    analyze --start) sets it aside as captions.stale.json and says so, so the render refuses instead of shipping it."""
    captions = out / "digest" / "captions.json"
    stale = out / "digest" / "captions.stale.json"
    failures = []
    if "captions.stale.json" in run_vl(ctx, "digest", out) or not captions.exists():
        failures.append("a rebuild with the same scenes set captions.json aside")
    document = read_digest(out)
    document["scenes"][1]["start_s"] += 0.5
    (out / "digest" / "digest.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    listing = run_vl(ctx, "digest", out)
    if captions.exists() or not stale.exists() or "captions.stale.json" not in listing:
        failures.append(f"captions for moved scenes: captions.json kept {captions.exists()}, "
                        f"stale file {stale.exists()}")
    if ctx.vl("digest", out, "--captions", captions).returncode != 2:
        failures.append("render did not refuse after the captions were set aside")
    return failures


def merge_failures(ctx, out):
    """--scenes 2 keeps the cut at 6.0 (the change after the dissolve is the weaker boundary) and clears the rest."""
    run_vl(ctx, "digest", out, "--scenes", "2")
    scenes = read_digest(out)["scenes"]
    failures = boundary_failures(scenes, SLIDE_STARTS[:2])
    if sorted(p.name for p in (out / "digest" / "frames").iterdir()) != ["S01.jpg", "S02.jpg"]:
        failures.append("stale frames left after a rebuild with fewer scenes")
    if (out / "digest" / "digest.md").exists():
        failures.append("stale digest.md left after a rebuild")
    return failures


@kat("C11", "digest")
def c11_slide_talk_digest(ctx):
    if not fa.has_voice(fa.TALK_VOICE):
        return SKIP, f"voice {fa.TALK_VOICE} not installed"
    video = ctx.fixture("digest_talk.mp4", build_digest_talk(synth_talk(ctx)))
    out = ctx.out_dir("C11")
    run_vl(ctx, "analyze", video, "--out", out, "--mode", "content")
    spoken = [tuple(s) for s in (json.loads((out / "analysis.json").read_text())["speech"] or {}).get("sentences", [])]
    if not spoken:
        return SKIP, "no transcript on this Mac (Apple ko-KR and whisper both unavailable)"
    listing = run_vl(ctx, "digest", out)
    scenes = read_digest(out)["scenes"]
    sheet = SHEET_LINE.search(listing)
    failures = boundary_failures(scenes, SLIDE_STARTS, dissolve_index=2)
    if failures:
        return verdict(failures, "")
    failures += image_failures(out, scenes, fx.SYNTH_SIZE) + text_failures(scenes) + speech_failures(scenes, spoken)
    if not sheet or sheet["last"] != "4":
        failures.append(f"no sheet line with its token cost in the listing: {listing!r}")
    scene_ids = [scene["id"] for scene in scenes]
    failures += caption_round_trip_failures(ctx, out, scene_ids) + refusal_failures(ctx, out, scene_ids)
    failures += stale_caption_failures(ctx, out) + merge_failures(ctx, out)
    return verdict(failures, f"4 scenes at {[s['start_s'] for s in scenes]}, one {fx.SYNTH_SIZE[0]}x{fx.SYNTH_SIZE[1]} frame "
                             f"each, sheet {sheet['w'] if sheet else '?'}x{sheet['h'] if sheet else '?'} "
                             f"{sheet['tok'] if sheet else '?'} tok; titles and {len(spoken)} sentences in their own scenes; "
                             "captions in md and escaped html, missing S03 and unknown S09 refused, captions for moved scenes "
                             "set aside; --scenes 2 keeps the 6.0 cut")


@kat("C12", "digest")
def c12_chapter_digest(ctx):
    video = ctx.fixture("digest_chapters.mp4", build_copy(synth_talk(ctx)))
    ctx.fixture("digest_chapters.info.json", build_info_json)
    out = ctx.out_dir("C12")
    run_vl(ctx, "analyze", video, "--out", out, "--mode", "content", "--ocr", "off")
    run_vl(ctx, "digest", out)
    scenes = read_digest(out)["scenes"]
    got = [(s["start_s"], s["end_s"], s["chapter"]) for s in scenes]
    if got != list(CHAPTERS):
        return (FAIL, f"scenes {got}, want the chapters {list(CHAPTERS)}")
    shown = [slide_shown(out / s["frame"]) for s in scenes]
    failures = [f"{s['id']} frame at {s['frame_t']} s shows slide {got_slide}, want slide {want}"
                for s, got_slide, want in zip(scenes, shown, CHAPTER_SLIDES) if got_slide != want]
    captions = out / "digest" / "captions.json"
    captions.write_text(json.dumps({s["id"]: f"{s['chapter']} 설명" for s in scenes}, ensure_ascii=False), encoding="utf-8")
    run_vl(ctx, "digest", out, "--captions", captions)
    md = (out / "digest" / "digest.md").read_text(encoding="utf-8")
    page = (out / "digest" / "digest.html").read_text(encoding="utf-8")
    links = [f"https://www.youtube.com/watch?v={VIDEO_ID}&t={int(start)}s" for start, _, _ in CHAPTERS]
    failures += [f"digest.md lacks {link}" for link in links if f"]({link})" not in md]
    failures += [f"digest.html lacks {link}" for link in links if f'href="{link.replace("&", "&amp;")}"' not in page]
    if not md.startswith(f"# {INFO_TITLE}\n"):
        failures.append(f"digest.md title line {md.splitlines()[0]!r}, want the info JSON title")
    return verdict(failures, f"3 scenes = chapters {[c[2] for c in CHAPTERS]}, frames show slides {shown}, deep links "
                             "&t=0s, 9s, 16s in md and html, title from the info JSON")


def slide_shown(path):
    """The synthetic slide (1 to 3) whose background colour is nearest the frame's median colour."""
    median = np.median(cv2.imread(str(path)).reshape(-1, 3), axis=0)
    return min(fx.SYNTH_SLIDES, key=lambda n: np.abs(median - np.array(fx.SYNTH_SLIDES[n][0][::-1], float)).sum())


def event(event_id, text, first_s, end_s, box, px_h=30, conf=1.0):
    return {"id": event_id, "text": text, "conf": conf, "first_s": first_s, "last_s": end_s, "end_s": end_s,
            "box": box, "px_h": px_h}


def chrome_failures():
    """Six 10 s scenes: a watermark read as ZONE in four and as ENOZ in one, a menu bar line, a slide title shown in
    two scenes and another title in the same place. The watermark, its misread and the menu line are chrome; both
    titles and a sidebar page number are content, and the page number (digits only) is never the headline."""
    mark, title_box = [0.93, 0.88, 0.07, 0.03], [0.1, 0.1, 0.5, 0.08]
    events = [event(f"T{n}", "ZONE", start + 1, start + 8, mark) for n, start in enumerate((0, 10, 20, 40))]
    events += [event("T5", "ENOZ", 31, 38, [0.929, 0.881, 0.07, 0.034], conf=0.5),
               event("T6", "파일", 50, 60, [0.02, 0.005, 0.03, 0.02]),
               event("T7", "첫 번째 슬라이드", 0, 10, title_box, px_h=40, conf=0.5),
               event("T8", "첫 번째 슬라이드", 30, 40, title_box, px_h=40, conf=0.5),
               event("T9", "두 번째 슬라이드", 10, 20, title_box, px_h=40, conf=0.5),
               event("T10", "22", 10, 20, [0.02, 0.3, 0.01, 0.02], px_h=20)]
    spans = [digest.Span(start, start + 10, digest.CUT) for start in range(0, 60, 10)]
    content, chrome = digest.split_chrome(spans, {"events": events, "persistent": []})
    failures = []
    if sorted(content, key=lambda i: int(i[1:])) != ["T7", "T8", "T9", "T10"]:
        failures.append(f"content lines {sorted(content)}, want the two titles and the page number (T7 to T10)")
    lines = digest.scene_text(spans[1], content.values(), {"T9", "T10"})
    texts, title = [line["text"] for line in lines], digest.headline(lines, {"T9", "T10"})
    if texts != ["두 번째 슬라이드", "22"] or title != "두 번째 슬라이드":
        failures.append(f"scene 2 lines {texts}, headline {title!r}")
    if chrome["recurring"] != ["ZONE"]:
        failures.append(f"recurring chrome {chrome['recurring']}, want ['ZONE']")
    return failures


def frame_choice_failures(folder):
    """Scene [0, 10): a state that flashes by with the most text, one that mostly belongs to the scene before, and the
    one on screen 6.5 s, which wins. Scene [10, 20): a black end card on screen 8 s loses to a 2 s shot."""
    keyframes_dir = folder / "keyframes"
    keyframes_dir.mkdir(parents=True, exist_ok=True)
    grey, black = np.full((90, 160, 3), 128, np.uint8), np.zeros((90, 160, 3), np.uint8)
    cases = (("K1", -5.0, 0.5, ["T1"], True, grey), ("K2", 0.5, 7.0, ["T2"], True, grey),
             ("K3", 7.0, 7.3, ["T3"], False, grey), ("K4", 7.3, 10.0, [], False, grey),
             ("K5", 10.0, 18.0, [], True, black), ("K6", 18.0, 20.0, [], False, grey))
    states = []
    for keyframe_id, begin, end, text_ids, settled, image in cases:
        cv2.imwrite(str(keyframes_dir / f"{keyframe_id}.jpg"), image)
        keyframe = {"id": keyframe_id, "t": max(begin, 0.0), "settled": settled, "text_ids": text_ids,
                    "file": f"keyframes/{keyframe_id}.jpg"}
        states.append((keyframe, begin, end))
    events = {"T1": {"text": "x" * 80}, "T2": {"text": "slide"}, "T3": {"text": "x" * 200}}
    run = SimpleNamespace(out=folder)
    picks = [digest.representative_state(run, digest.Span(start, end, digest.CUT), states, events)[0]["id"]
             for start, end in ((0, 10), (10, 20))]
    return [] if picks == ["K2", "K6"] else [f"frames {picks}, want K2 (longest on screen) and K6 (not the black card)"]


@kat("C13", "digest", quick=True)
def c13_chrome_and_frame_rules(ctx):
    failures = chrome_failures() + frame_choice_failures(ctx.work / "out" / "C13")
    return verdict(failures, "watermark, its misread and a menu bar line left out, a slide shown twice kept, digits never "
                             "the headline; frames: longest on screen wins, a black end card loses")
