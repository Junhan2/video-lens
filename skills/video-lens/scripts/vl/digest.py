"""Scene digest, on request only (`vl.py digest OUT [--scenes N]`): the deterministic half of "key captures and a few
lines per scene". After a content analysis it picks the scenes, exports one full-resolution frame per scene, tiles
numbered contact sheets and gathers each scene's on-screen text and speech into OUT/digest/digest.json. Claude writes
the captions from the sheets; digest_notes.py renders them into digest.md and digest.html.

Scenes are the video's chapters when yt-dlp's info JSON lists them, else the survey's keyframe states (a shot start, or
the moment a screen state appeared). Over the limit, the shortest scene joins the neighbour across its weaker boundary
(a cut or chapter start outranks a state change), so every boundary stays where a state began, never inside one.
"""
import json
import math
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from . import TOOL_VERSION, decode, ocr, sheets_content, views
from .cache import info_json_path, json_default, write_atomic
from .context import clock, content_time
from .errors import EXIT_BAD_ARGS, VlError
from .report import LOW_OCR_CONF, sentences, text_label

SCHEMA = "video-lens-digest/1"
DIGEST_DIR = "digest"
FRAMES_DIR = f"{DIGEST_DIR}/frames"
DIGEST_FILE = f"{DIGEST_DIR}/digest.json"
CAPTIONS_FILE = f"{DIGEST_DIR}/captions.json"
STALE_CAPTIONS_FILE = f"{DIGEST_DIR}/captions.stale.json"
MD_FILE = f"{DIGEST_DIR}/digest.md"
HTML_FILE = f"{DIGEST_DIR}/digest.html"
SCENES_BASE = 6                 # default limit without chapters: 6 + 2 per 10 minutes, at most 30
SCENES_PER_10_MIN = 2
SCENES_MAX = 30
CUT, CHANGE = 2, 1              # boundary strength: a cut or chapter start outranks a state change
SHEET_COLS = 4
SHEET_ROWS = 4
TEXT_HOLD_S = 1.0               # text belongs to a scene it shows in this long, or for half its time on screen
EDGE_BAND = 0.05                # a line wholly in the top or bottom 5 % of the frame is chrome: menu bar, clock
CHROME_SCENE_SHARE = 0.25       # text in >= 25 % of the scenes (and in >= 3) is chrome: app menus, footers, watermarks
CHROME_SCENES_MIN = 3           # below 3, a slide shown twice would count as chrome
CHROME_SAME_PLACE_IOU = 0.7     # a line where chrome text sits is its misread ("ENOZ", "20NE" over "ZONE")
CHROME_BOX_DECIMALS = 3         # chrome boxes compared once per place, not once per OCR event
SAME_PLACE_ROWS = 512           # OCR boxes per comparison block: bounds memory to rows x chrome places
NEAR_BLACK_P95 = 24             # grey level 95 % of a keyframe stays under: a fade or an empty end card
OCR_LINES_MAX = 12
SPEECH_CHARS_MAX = 400
LIST_SAID_CHARS = 80
FRAME_TIME_DECIMALS = 4
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
YOUTUBE_ID_PATHS = ("shorts", "live", "embed")


@dataclass
class Span:
    start_s: float
    end_s: float
    strength: int                                   # of the boundary the span starts at
    chapters: list = field(default_factory=list)    # chapter titles merged into it

    @property
    def duration_s(self):
        return self.end_s - self.start_s


def build_digest(run, scene_limit=None):
    """`vl.py digest OUT [--scenes N]`: frames, sheets and digest.json; returns the compact scene list for stdout."""
    content = run.analysis["content"]
    if not content:
        raise VlError(EXIT_BAD_ARGS, "this OUT has no content analysis, so it has no scenes",
                      "Rerun analyze with --mode content or both, then vl.py digest OUT")
    source = source_info(run)
    chapters = chapter_spans(source.pop("chapters"), run.range)
    starts = state_starts(content, run.range)
    candidates = chapters or state_spans(starts, run.range)
    limit = scene_limit or len(chapters) or default_limit(run.range.duration_s)
    spans = merge_spans(candidates, limit)
    events, chrome = split_chrome(spans, content["text"])
    states = keyframe_states(content["keyframes"], starts, run.range)
    spoken = sentences(run.analysis)
    scenes = [scene_record(run, number, span, states, events, spoken, source["youtube_id"])
              for number, span in enumerate(spans, 1)]
    previous_scenes = read_scene_spans(run.out)
    clear_outputs(run.out)
    images = export_frames(run, scenes)
    document = {"schema": SCHEMA, "tool_version": TOOL_VERSION,
                "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "source": source, "range": run.analysis["range"], "method": "chapters" if chapters else "keyframe states",
                "candidates": len(candidates), "limit": limit, "frame_size": [images[0].shape[1], images[0].shape[0]],
                "chrome": chrome, "sheets": write_sheets(run, scenes, images), "scenes": scenes,
                "captions": CAPTIONS_FILE}
    text = json.dumps(document, ensure_ascii=False, indent=1, default=json_default) + "\n"
    write_atomic(run.out / DIGEST_FILE, text.encode())
    is_stale = retire_stale_captions(run.out, previous_scenes, scene_spans(scenes))
    return scene_listing(run, document, is_stale)


# ---------------------------------------------------------------- source: yt-dlp info JSON

def source_info(run):
    """Title, link, uploader and chapters from yt-dlp's info JSON beside the input (a URL download writes one), else
    the URL or the file name."""
    info = read_info_json(run.input_path)
    url = info.get("webpage_url") or info.get("original_url") or run.url
    return {"title": info.get("title") or url or run.input_path.name, "url": url, "file": run.input_path.name,
            "uploader": info.get("uploader") or info.get("channel"), "upload_date": iso_date(info.get("upload_date")),
            "youtube_id": youtube_id(info, url), "info_json": bool(info), "chapters": info.get("chapters") or []}


def read_info_json(video_path):
    try:
        info = json.loads(info_json_path(video_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def iso_date(text):
    """yt-dlp's `20260930` as `2026-09-30`."""
    text = str(text or "")
    if len(text) != 8 or not text.isdigit():
        return text or None
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


def youtube_id(info, url):
    """The YouTube video id from the info JSON, else from a watch, youtu.be, shorts, live or embed URL; None otherwise."""
    if info.get("extractor_key") == "Youtube" and info.get("id"):
        return info["id"]
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if not any(host == name or host.endswith("." + name) for name in YOUTUBE_HOSTS):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if host.endswith("youtu.be"):
        return parts[0] if parts else None
    watch_id = parse_qs(parsed.query).get("v", [None])[0]
    if watch_id:
        return watch_id
    return parts[1] if len(parts) >= 2 and parts[0] in YOUTUBE_ID_PATHS else None


def deep_link(video_id, t):
    return f"https://www.youtube.com/watch?v={video_id}&t={int(t)}s" if video_id else None


def whole_clock(t):
    """mm:ss floored to the second, as a player shows it and as the deep link's &t starts."""
    return clock(math.floor(t), 0)


def time_span(scene):
    return f"{whole_clock(scene['start_s'])}-{whole_clock(scene['end_s'])}"


def clipped(text, limit):
    return text if len(text) <= limit else text[:limit - 3] + "..."


# ---------------------------------------------------------------- scene spans

def chapter_spans(chapters, rng):
    """Chapters clipped to the analysed range, each starting at a strong boundary."""
    spans = []
    for chapter in chapters:
        start = max(float(chapter.get("start_time") or 0.0), rng.start_s)
        end = min(float(chapter.get("end_time") or rng.end_s), rng.end_s)
        if end > start:
            spans.append(Span(start, end, CUT, [str(chapter.get("title") or "").strip()]))
    return spans


def state_starts(content, rng):
    """(time, boundary strength) per keyframe where its screen state began: a shot's first keyframe at the cut, any
    other where its state appeared (after a dissolve, not inside it); the first at the range start."""
    shot_starts = {shot["id"]: shot["start_s"] for shot in content["shots"]}
    starts = [(rng.start_s, CUT)]
    for keyframe in content["keyframes"][1:]:
        is_cut = keyframe["reason"] in sheets_content.SHOT_REASONS
        begin = shot_starts[keyframe["shot"]] if is_cut else keyframe.get("shown_s", keyframe["t"])
        starts.append((begin, CUT if is_cut else CHANGE))
    return starts


def state_spans(starts, rng):
    """One span per keyframe state that begins after the one before; each runs until the next state begins."""
    kept = []
    for start, strength in starts:
        if not kept or start > kept[-1][0]:
            kept.append((start, strength))
    ends = [start for start, _ in kept[1:]] + [rng.end_s]
    return [Span(start, end, strength) for (start, strength), end in zip(kept, ends)]


def keyframe_states(keyframes, starts, rng):
    """(keyframe, begin, end): while each keyframe's state is the latest on screen, from where it began until the
    next state begins. Unlike hold_s (the still part), this also counts a moving shot's time."""
    begins = [start for start, _ in starts]
    ends = begins[1:] + [rng.end_s]
    return [(keyframe, begin, max(begin, end)) for keyframe, begin, end in zip(keyframes, begins, ends)]


def default_limit(duration_s):
    return min(SCENES_MAX, SCENES_BASE + int(SCENES_PER_10_MIN * duration_s / 600))


def merge_spans(spans, limit):
    """Joins the shortest span (the earliest on a tie) to the neighbour across its weaker boundary until at most
    `limit` remain; the joined span keeps the earlier start and every chapter title."""
    spans = list(spans)
    while len(spans) > max(1, limit):
        shortest = min(range(len(spans)), key=lambda i: (spans[i].duration_s, i))
        first = min(shortest, merge_side(spans, shortest))
        left, right = spans[first], spans[first + 1]
        spans[first:first + 2] = [Span(left.start_s, right.end_s, left.strength, left.chapters + right.chapters)]
    return spans


def merge_side(spans, index):
    """The neighbour span `index` joins: across the weaker of its two boundaries, else the shorter neighbour."""
    if index == 0:
        return 1
    if index == len(spans) - 1:
        return index - 1
    before, after = spans[index].strength, spans[index + 1].strength
    if before != after:
        return index - 1 if before < after else index + 1
    return index - 1 if spans[index - 1].duration_s <= spans[index + 1].duration_s else index + 1


# ---------------------------------------------------------------- scene records

def scene_record(run, number, span, states, events, spoken, video_id):
    """digest.json scene: span, representative frame, content text (chrome left out) and the speech whose midpoint
    lies inside."""
    scene_id = f"S{number:02d}"
    state = representative_state(run, span, states, events)
    keyframe = state[0] if state else None
    frame_ids = set(keyframe["text_ids"]) if keyframe else set()
    lines = scene_text(span, events.values(), frame_ids)
    said = [sentence for sentence in spoken if span.start_s <= (sentence[0] + sentence[1]) / 2 < span.end_s]
    kept = first_sentences(said)
    frame_t = run.snap_time(frame_time(span, state))
    return {"id": scene_id, "start_s": content_time(span.start_s), "end_s": content_time(span.end_s),
            "chapter": " · ".join(title for title in span.chapters if title) or None,
            "link": deep_link(video_id, span.start_s),
            "frame": f"{FRAMES_DIR}/{scene_id}.jpg", "frame_t": round(frame_t, FRAME_TIME_DECIMALS),
            "keyframe": keyframe["id"] if keyframe else None,
            "headline": headline(lines, frame_ids),
            "ocr": [line["text"] for line in lines[:OCR_LINES_MAX]],
            "ocr_more": max(0, len(lines) - OCR_LINES_MAX),
            "speech": kept, "speech_more": len(said) - len(kept),
            "transcript_lines": [said[0][3], said[-1][3]] if said else None,
            "sheet": None, "cell": None}


def representative_state(run, span, states, events):
    """The (keyframe, begin, end) state that is on screen longest inside the span, so a state that mostly belongs to
    a neighbouring scene, or flashes by, cannot win; among states shown at least half that long, the one with the most
    content text, then settled (sharp), then longer. A near-black frame without text (a fade, an empty end card) wins
    only when nothing else is shown. None without keyframes."""
    def content_chars(keyframe):
        return sum(len(events[i]["text"]) for i in keyframe["text_ids"] if i in events)

    def rank(item):
        (keyframe, _, _), dwell = item
        return content_chars(keyframe), keyframe["settled"], dwell, -keyframe["t"]

    dwells = [(state, min(state[2], span.end_s) - max(state[1], span.start_s)) for state in states]
    shown = [(state, dwell) for state, dwell in dwells if dwell > 0]
    lit = [(state, dwell) for state, dwell in shown
           if content_chars(state[0]) or not is_near_black(run.out / state[0]["file"])] or shown
    if not lit:
        return None
    longest = max(dwell for _, dwell in lit)
    return max([item for item in lit if item[1] >= longest / 2], key=rank)[0]


def is_near_black(path):
    grey = cv2.imread(str(path), cv2.IMREAD_REDUCED_GRAYSCALE_4)
    return grey is not None and float(np.percentile(grey, 95)) < NEAR_BLACK_P95


def frame_time(span, state):
    """The keyframe's own sample when it lies inside the span, else the middle of the state's time inside the span
    (a chapter that starts or ends within one long screen state); the span's middle without keyframes."""
    if state is None:
        return (span.start_s + span.end_s) / 2
    keyframe, begin, end = state
    low, high = max(begin, span.start_s), min(end, span.end_s)
    return keyframe["t"] if low <= keyframe["t"] < high else (low + high) / 2


def is_in_span(event, span):
    """On screen inside the span for TEXT_HOLD_S or half its time on screen: the OCR sample after a cut that still
    saw the old slide does not pull its lines in."""
    end_s = event.get("end_s", event["last_s"])
    overlap = min(end_s, span.end_s) - max(event["first_s"], span.start_s)
    return overlap > 0 and overlap >= min(TEXT_HOLD_S, (end_s - event["first_s"]) / 2)


def split_chrome(spans, text):
    """({id: event} of content text, chrome summary). Chrome is screen furniture, not scene content: lines wholly in
    the top or bottom EDGE_BAND (menu bar, clock), text in CHROME_SCENE_SHARE of the scenes or flagged persistent by
    the analysis (app menus, footers, a watermark read the same way often enough), and any line where such text sits
    (the watermark's misreads, which match no spelling)."""
    events = text["events"]
    scenes_of = defaultdict(set)
    for number, span in enumerate(spans):
        for event in events:
            if is_in_span(event, span):
                scenes_of[ocr.normalize(event["text"])].add(number)
    needed = max(CHROME_SCENES_MIN, math.ceil(CHROME_SCENE_SHARE * len(spans)))
    recurring = {key for key, numbers in scenes_of.items() if len(numbers) >= needed}
    recurring |= {ocr.normalize(item["text"]) for item in text["persistent"]}
    anchors = [event["box"] for event in events if ocr.normalize(event["text"]) in recurring]
    at_chrome_place = same_place([event["box"] for event in events], anchors)
    content, chrome_texts = {}, {}
    for event, is_placed in zip(events, at_chrome_place):
        key = ocr.normalize(event["text"])
        if key in recurring:
            chrome_texts.setdefault(key, (len(scenes_of[key]), event["text"]))
        elif not is_placed and not is_in_edge_band(event["box"]):
            content[event["id"]] = event
    common = sorted(chrome_texts.values(), key=lambda item: -item[0])
    return content, {"lines": len(events) - len(content), "recurring": [text for _, text in common[:OCR_LINES_MAX]]}


def is_in_edge_band(box):
    _, y, _, h = box
    return y + h <= EDGE_BAND or y >= 1 - EDGE_BAND


def same_place(boxes, anchors):
    """Per box (x, y, w, h), whether it overlaps an anchor box with IoU >= CHROME_SAME_PLACE_IOU. Boxes are compared
    SAME_PLACE_ROWS at a time: events and anchors both grow with the video, so one matrix would grow with its square."""
    if not boxes or not anchors:
        return np.zeros(len(boxes), bool)
    rows = np.array(boxes, float)[:, None, :]
    places = np.unique(np.round(np.array(anchors, float), CHROME_BOX_DECIMALS), axis=0)[None, :, :]
    return np.concatenate([overlaps_any(rows[i:i + SAME_PLACE_ROWS], places)
                           for i in range(0, len(rows), SAME_PLACE_ROWS)])


def overlaps_any(a, b):
    """(n, 1, 4) boxes against (1, m, 4) boxes: whether each of the n has IoU >= CHROME_SAME_PLACE_IOU with any."""
    width = np.minimum(a[..., 0] + a[..., 2], b[..., 0] + b[..., 2]) - np.maximum(a[..., 0], b[..., 0])
    height = np.minimum(a[..., 1] + a[..., 3], b[..., 1] + b[..., 3]) - np.maximum(a[..., 1], b[..., 1])
    inter = np.clip(width, 0, None) * np.clip(height, 0, None)
    union = a[..., 2] * a[..., 3] + b[..., 2] * b[..., 3] - inter
    return (inter >= CHROME_SAME_PLACE_IOU * union).any(axis=1)


def scene_text(span, events, frame_ids):
    """Content text in the span once per text, ordered for the OCR_LINES_MAX cap by glyph height (slide titles and
    body before a sidebar's small print, from every state in the scene), the representative frame's lines first
    among equals."""
    inside = [event for event in events if is_in_span(event, span)]
    inside.sort(key=lambda e: (-e["px_h"], e["id"] not in frame_ids, e["first_s"], e["box"][1], e["box"][0]))
    seen, lines = set(), []
    for event in inside:
        key = ocr.normalize(event["text"])
        if key not in seen:
            seen.add(key)
            lines.append(event)
    return lines


def headline(lines, frame_text_ids):
    """The tallest confident line on the scene's frame (usually its slide title), else in the whole scene: a merged
    scene spans several slides, and its headline must name the one its frame shows. Digits and symbols alone (a
    sidebar page number, a zoom level) never name a scene."""
    named = [line for line in lines if any(char.isalpha() for char in line["text"])]
    candidates = [line for line in named if line["id"] in frame_text_ids] or named
    if not candidates:
        return None
    return max(candidates, key=lambda e: (e["conf"] >= LOW_OCR_CONF, e["px_h"], -e["first_s"]))["text"]


def first_sentences(said):
    """Sentence texts in order until SPEECH_CHARS_MAX; the first one always."""
    kept, total = [], 0
    for _, _, text, _ in said:
        if kept and total + len(text) > SPEECH_CHARS_MAX:
            break
        kept.append(text)
        total += len(text)
    return kept


# ---------------------------------------------------------------- images and outputs

def clear_outputs(out):
    """A rebuild may pick other scenes, so the last build's frames, sheets and notes go; captions.json is judged by
    retire_stale_captions."""
    folder = out / DIGEST_DIR
    shutil.rmtree(out / FRAMES_DIR, ignore_errors=True)
    for path in [*folder.glob("sheet_*.jpg"), out / MD_FILE, out / HTML_FILE]:
        path.unlink(missing_ok=True)


def scene_spans(scenes):
    """What captions are written for: each scene's id and time span."""
    return [[scene["id"], scene["start_s"], scene["end_s"]] for scene in scenes]


def read_scene_spans(out):
    """The scene spans of the digest.json already in OUT, None when there is none to read."""
    try:
        return scene_spans(json.loads((out / DIGEST_FILE).read_text(encoding="utf-8"))["scenes"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def retire_stale_captions(out, previous_spans, spans):
    """captions.json written for other scenes (same ids, other times: another --scenes or analysed range) becomes
    captions.stale.json, so it cannot render under the wrong frames; returns whether it moved."""
    captions = out / CAPTIONS_FILE
    if not captions.exists() or previous_spans == spans:
        return False
    captions.replace(out / STALE_CAPTIONS_FILE)
    return True


def export_frames(run, scenes):
    """One full-resolution frame per scene (at most views.MAX_SIDE_PX per side), written and returned."""
    width, height = run.display_size
    export_w = min(width, math.floor(views.MAX_SIDE_PX * width / max(width, height)))
    images = decode.read_frames(run.input_path, [scene["frame_t"] for scene in scenes],
                                width=export_w if export_w < width else None, stream_index=run.probe["video"]["index"])
    images = [views.fit_to_limits(image) for image in images]
    for scene, image in zip(scenes, images):
        views.write_view(run, image, scene["frame"], level="L3", kind="frame", covers=scene["id"])
    return images


def write_sheets(run, scenes, images):
    """sheet_NN.jpg: SHEET_COLS x SHEET_ROWS scenes per sheet, each cell labelled `S01 00:00-01:23`; fills each
    scene's sheet and cell and returns the view records."""
    per_sheet = SHEET_COLS * SHEET_ROWS
    written = []
    for number, first in enumerate(range(0, len(scenes), per_sheet), 1):
        chunk, tiles = scenes[first:first + per_sheet], images[first:first + per_sheet]
        tile_w, tile_h = views.tile_size(tiles[0].shape, math.ceil(len(chunk) / SHEET_COLS), views.ZOOM_CELL_W)
        labels = [f"{scene['id']} {time_span(scene)}" for scene in chunk]
        sheet = views.tile_sheet(tiles, labels, min(SHEET_COLS, len(chunk)), tile_w, tile_h)
        rel = f"{DIGEST_DIR}/sheet_{number:02d}.jpg"
        written.append(views.write_view(run, sheet, rel, level="L2", kind="scenes", covers=f"{chunk[0]['id']}-{chunk[-1]['id']}",
                                        cells=len(chunk), cell_px=(tile_w, tile_h)))
        for cell, scene in enumerate(chunk):
            scene["sheet"], scene["cell"] = rel, cell
    return written


def scene_listing(run, document, is_stale):
    """stdout: the source, the sheets with their token cost, one line per scene and the caption step."""
    source, scenes, rng = document["source"], document["scenes"], document["range"]
    frame_w, frame_h = document["frame_size"]
    captions = run.out / CAPTIONS_FILE
    origin = "chapters (yt-dlp info JSON)" if document["method"] == "chapters" else f"{document['candidates']} keyframe states"
    lines = [f"# video-lens digest · {source['title']}",
             f"{source['url'] or source['file']} · {whole_clock(rng['end_s'] - rng['start_s'])} · {len(scenes)} scenes "
             f"from {origin}, limit {document['limit']}",
             "Sheets (Read these):"]
    lines += [f"- {views.view_line(run, view)} · {view['covers']}" for view in document["sheets"]]
    lines.append(f"Frames: {run.out / FRAMES_DIR}/Sxx.jpg · {frame_w}x{frame_h} · "
                 f"{views.visual_tokens(frame_w, frame_h):,} tok each (Read one only when its sheet cell is unclear)")
    lines.append(f"Scenes (id · time · chapter · on-screen headline · first sentence; all text in {run.out / DIGEST_FILE}):")
    lines += [scene_line(scene) for scene in scenes]
    if is_stale:
        lines.append(f"The old captions.json was written for other scenes: moved to {run.out / STALE_CAPTIONS_FILE}")
    lines.append(f'Next: write {captions} as {{"S01": "1-2 plain lines", ...}} for every scene in the user\'s language, '
                 f"then vl.py digest OUT --captions {captions}")
    return "\n".join(lines) + "\n"


def scene_line(scene):
    parts = [f"{scene['id']} {time_span(scene)}"]
    if scene["chapter"]:
        parts.append(f"ch {text_label(scene['chapter'])}")
    if scene["headline"]:
        parts.append(text_label(scene["headline"]))
    if scene["speech"]:
        parts.append(clipped(scene["speech"][0], LIST_SAID_CHARS))
    return "- " + " · ".join(parts)
