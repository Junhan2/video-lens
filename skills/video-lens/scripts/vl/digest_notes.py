"""Scene digest notes (`vl.py digest OUT --captions FILE`): Claude's captions and OUT/digest/digest.json rendered into
digest.md (frames linked relatively) and a self-contained digest.html (frames embedded, light and dark, printable).
Captions that miss a scene or name an unknown one are refused, so no note ships with a silent gap.
"""
import base64
import html
import json
import re
from pathlib import Path

import cv2

from .cache import write_atomic
from .digest import DIGEST_DIR, DIGEST_FILE, HTML_FILE, MD_FILE, clipped, time_span, whole_clock
from .errors import EXIT_BAD_ARGS, EXIT_INPUT, VlError

SAID_CHARS = 160
HTML_IMAGE_W = 1280             # embedded frames: at full resolution a 30-scene page would weigh tens of MB
HTML_JPEG_QUALITY = 85
MD_SPECIAL = re.compile(r"([\\`*_\[\]<>])")
CAPTIONS_SHAPE = '{"S01": "...", ...}'
PAGE_STYLE = """
:root { color-scheme: light dark; --bg: #ffffff; --fg: #1d1d1f; --muted: #6e6e73; --line: #d2d2d7; --link: #0a5fd8; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161617; --fg: #f5f5f7; --muted: #a1a1a6; --line: #3a3a3c; --link: #6ea8ff; }
}
body { margin: 0 auto; max-width: 880px; padding: 32px 16px 64px; background: var(--bg); color: var(--fg);
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Noto Sans KR", "Segoe UI", sans-serif; }
h1 { font-size: 28px; line-height: 1.3; margin: 0 0 8px; }
h2 { font-size: 18px; margin: 0 0 12px; }
.meta, .time, .said { color: var(--muted); }
a { color: var(--link); }
section { border-top: 1px solid var(--line); padding: 24px 0; }
img { display: block; width: 100%; height: auto; border: 1px solid var(--line); border-radius: 6px; }
.caption { margin: 12px 0 0; white-space: pre-line; }
.said { margin: 8px 0 0; padding-left: 12px; border-left: 3px solid var(--line); font-size: 14px; }
@media print {
  :root { --bg: #ffffff; --fg: #000000; --muted: #444444; --line: #bbbbbb; --link: #000000; }
  body { max-width: none; padding: 0; }
  section { break-inside: avoid; }
  a { text-decoration: none; }
}
"""


def render_notes(out, captions_path):
    """Writes digest.md and digest.html from digest.json and the captions; returns the stdout lines."""
    out = Path(out).expanduser().resolve()
    document = load_digest(out)
    scene_ids = [scene["id"] for scene in document["scenes"]]
    captions = load_captions(Path(captions_path).expanduser(), scene_ids)
    write_atomic(out / MD_FILE, notes_markdown(document, captions).encode())
    page = notes_html(out, document, captions).encode()
    write_atomic(out / HTML_FILE, page)
    return (f"digest.md: {out / MD_FILE} (links frames/Sxx.jpg: copy the whole {out / DIGEST_DIR} folder)\n"
            f"digest.html: {out / HTML_FILE} · {len(page) / 1e6:.1f} MB, self-contained (frames embedded)\n"
            f"{len(scene_ids)} scenes captioned\n")


def load_digest(out):
    try:
        return json.loads((out / DIGEST_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise VlError(EXIT_INPUT, f"no {DIGEST_FILE} in {out}", "Run vl.py digest OUT first") from None


def load_captions(path, scene_ids):
    """{id: caption} for every scene; a refusal names every missing, empty or unknown id on one line."""
    try:
        captions = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise VlError(EXIT_BAD_ARGS, f"no captions file at {path}", f"Write {CAPTIONS_SHAPE} there first") from None
    except (OSError, ValueError) as error:
        raise VlError(EXIT_BAD_ARGS, f"captions file {path.name} is not readable JSON ({error})",
                      f"Write one JSON object {CAPTIONS_SHAPE}") from None
    if not isinstance(captions, dict):
        raise VlError(EXIT_BAD_ARGS, f"captions file {path.name} is not a JSON object", f"Write {CAPTIONS_SHAPE}")
    missing = [i for i in scene_ids if not isinstance(captions.get(i), str) or not captions[i].strip()]
    unknown = [json.dumps(key, ensure_ascii=False) for key in captions if key not in scene_ids]
    problems = ([f"no caption for {', '.join(missing)}"] if missing else []) + (
        [f"unknown scene id {', '.join(unknown)}"] if unknown else [])
    if problems:
        raise VlError(EXIT_BAD_ARGS, f"captions refused: {'; '.join(problems)}",
                      f"Write 1-2 lines for each of {scene_ids[0]} to {scene_ids[-1]} and no other id, then rerun")
    return {scene_id: captions[scene_id].strip() for scene_id in scene_ids}


# ---------------------------------------------------------------- shared parts

def said_excerpt(scene):
    """The scene's first sentences, cut to SAID_CHARS; None without speech."""
    said = " ".join(scene["speech"])
    return clipped(said, SAID_CHARS) if said else None


def source_facts(document):
    """(text, link or None) pairs for the line under the title: source, length, uploader, date, scene count."""
    source, rng = document["source"], document["range"]
    length = (whole_clock(rng["end_s"] - rng["start_s"]) if rng["is_full"]
              else f"{whole_clock(rng['start_s'])}-{whole_clock(rng['end_s'])}")
    facts = [(source["url"], source["url"]) if source["url"] else (source["file"], None), (length, None)]
    facts += [(value, None) for value in (source["uploader"], source["upload_date"]) if value]
    return facts + [(f"{len(document['scenes'])} scenes", None)]


# ---------------------------------------------------------------- digest.md

def md_text(text):
    return MD_SPECIAL.sub(r"\\\1", text)


def notes_markdown(document, captions):
    facts = [f"[{md_text(text)}]({link})" if link else md_text(text) for text, link in source_facts(document)]
    lines = [f"# {md_text(document['source']['title'])}", "", " · ".join(facts), ""]
    for scene in document["scenes"]:
        span = time_span(scene)
        heading = [scene["id"], f"[{span}]({scene['link']})" if scene["link"] else span]
        heading += [md_text(scene["chapter"])] if scene["chapter"] else []
        frame = Path(scene["frame"]).relative_to(DIGEST_DIR).as_posix()
        lines += [f"## {' · '.join(heading)}", "", f"![{scene['id']} {span}]({frame})", "",
                  md_text(captions[scene["id"]]).replace("\n", "  \n"), ""]
        said = said_excerpt(scene)
        lines += [f'> "{md_text(said)}"', ""] if said else []
    return "\n".join(lines)


# ---------------------------------------------------------------- digest.html

def notes_html(out, document, captions):
    title = html.escape(document["source"]["title"])
    facts = " · ".join(f'<a href="{html.escape(link)}">{html.escape(text)}</a>' if link else html.escape(text)
                       for text, link in source_facts(document))
    sections = "\n".join(scene_html(out, scene, captions[scene["id"]]) for scene in document["scenes"])
    return (f'<!doctype html>\n<html>\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>{title}</title>\n'
            f"<style>{PAGE_STYLE}</style>\n</head>\n<body>\n<header>\n<h1>{title}</h1>\n"
            f'<p class="meta">{facts}</p>\n</header>\n<main>\n{sections}\n</main>\n</body>\n</html>\n')


def scene_html(out, scene, caption):
    span = time_span(scene)
    shown_time = f'<a href="{html.escape(scene["link"])}">{span}</a>' if scene["link"] else span
    chapter = f" · {html.escape(scene['chapter'])}" if scene["chapter"] else ""
    image, width, height = embedded_frame(out / scene["frame"])
    said = said_excerpt(scene)
    quote = f'<blockquote class="said">"{html.escape(said)}"</blockquote>\n' if said else ""
    return (f'<section id="{scene["id"]}">\n<h2>{scene["id"]} · <span class="time">{shown_time}</span>{chapter}</h2>\n'
            f'<img src="{image}" width="{width}" height="{height}" alt="{html.escape(caption)}">\n'
            f'<p class="caption">{html.escape(caption)}</p>\n{quote}</section>')


def embedded_frame(path):
    """The frame as a JPEG data URI at most HTML_IMAGE_W wide: (uri, width, height)."""
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise VlError(EXIT_INPUT, f"frame {path.name} is missing from {path.parent}",
                      "Run vl.py digest OUT again, then --captions")
    if image.shape[1] > HTML_IMAGE_W:
        size = (HTML_IMAGE_W, max(1, round(HTML_IMAGE_W * image.shape[0] / image.shape[1])))
        image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
    _, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, HTML_JPEG_QUALITY])
    uri = "data:image/jpeg;base64," + base64.b64encode(data.tobytes()).decode("ascii")
    return uri, image.shape[1], image.shape[0]
