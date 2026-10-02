#!/usr/bin/env python3
"""After a run: re-render its final recreation.html, score it, and make the web files.

  python3 finish_case.py NAME        (NAME-base for the run without video-lens: scored the same way, no web files)

The final page is rendered again here (declared.mjs, 1920x1080, 60 fps) so the score belongs to the page that is
published, not to whatever render the run made before its last edit. The render length and the floor (another
reel's original, for the look score of two different reels) come from the case's row in sources.tsv. Writes
runs/NAME/summary.json and side_by_side.mp4 (original left, rebuild right). For a video-lens run it also writes the web
files under web/: the comparison video (the original muted at half size beside the rebuild, labelled), its poster,
and the page.
"""
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from score import combine_results   # noqa: E402  (bench/score.py: a log can hold several result events)

SKILL = Path(os.environ.get("VIDEO_LENS_SKILL_LINK", Path.home() / ".claude/skills/video-lens"))
DECLARED = SKILL / "scripts" / "declared.mjs"


FONT = "/System/Library/Fonts/HelveticaNeue.ttc"


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True, capture_output=True)


def label_png(text, path):
    """A small white-on-dark tag; the Homebrew ffmpeg here has no drawtext, so it is overlaid as an image."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(FONT, 20)
    left, top, right, bottom = font.getbbox(text)
    image = Image.new("RGBA", (right - left + 20, bottom - top + 14), (0, 0, 0, 140))
    ImageDraw.Draw(image).text((10 - left, 7 - top), text, font=font, fill=(255, 255, 255, 255))
    image.save(path)


def write_web(name, original, render, page):
    """The published comparison: original | rebuild at 960x540 each, no sound, and its poster near the end."""
    web = ROOT / "web"
    web.mkdir(exist_ok=True)
    tags = [web / "tag-original.png", web / "tag-rebuild.png"]
    label_png("ORIGINAL", tags[0])
    label_png("VIDEO-LENS REBUILD", tags[1])
    pair = ("[0:v]scale=960:540,fps=60[o];[o][2:v]overlay=16:16[a];"
            "[1:v]scale=960:540[r];[r][3:v]overlay=16:16[b];[a][b]hstack=shortest=1")
    run("ffmpeg", "-v", "error", "-y", "-i", original, "-i", render, "-i", tags[0], "-i", tags[1],
        "-filter_complex", pair, "-c:v", "libx264",
        "-crf", "26", "-preset", "slow", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", web / f"{name}.mp4")
    run("ffmpeg", "-v", "error", "-y", "-sseof", "-0.6", "-i", web / f"{name}.mp4", "-frames:v", "1", "-q:v", "4",
        web / f"{name}.jpg")
    shutil.copy(page, web / f"{name}.html")


def run_stats(log):
    events = (json.loads(line) for line in log.open() if line.strip())
    result = combine_results([event for event in events if event.get("type") == "result"]) or {}
    return {"cost_usd": round(result.get("total_cost_usd", 0), 2), "turns": result.get("num_turns"),
            "is_error": result.get("is_error")}


def score(original, recreation, out_json):
    run(sys.executable, ROOT / "compare.py", original, recreation, "--json", out_json)
    return json.loads(out_json.read_text())


def main(name):
    reel = name.removesuffix("-base")
    source = next(row for row in csv.DictReader((ROOT / "sources.tsv").open(), delimiter="\t") if row["name"] == reel)
    floor_name, seconds = source["floor"], source["seconds"]
    case = ROOT / "runs" / name
    page = case / "work" / "recreation.html"
    if not page.exists():
        raise SystemExit(f"{name}: no recreation.html")
    final = case / "final"
    shutil.rmtree(final, ignore_errors=True)
    final.mkdir()
    shutil.copy(page, final / "recreation.html")
    run("node", DECLARED, final / "recreation.html", "--out", final / "decl", "--viewport", "1920x1080",
        "--render", "60", seconds)
    render = final / "decl" / "render.mp4"
    original = ROOT / "src" / f"{reel}.mp4"
    scores = score(original, render, final / "score.json")
    floor_json = ROOT / "floor" / f"{reel}-vs-{floor_name}" / "score.json"   # shared by a reel's two runs
    if not floor_json.exists():
        floor_json.parent.mkdir(parents=True, exist_ok=True)
        score(original, ROOT / "src" / f"{floor_name}.mp4", floor_json)
    floor = json.loads(floor_json.read_text())
    wall = json.loads((case / "wall.json").read_text())["wall_s"]
    summary = {
        "name": name, "look": scores["look_mean"], "look_floor": floor["look_mean"],
        "color": scores["color_mean"], "cuts_matched": scores["cuts_matched"], "cuts_original": scores["cuts_original"],
        "cuts_recreation": scores["cuts_recreation"], "cut_offset_ms_median": scores["cut_offset_ms_median"],
        "words_found": scores["words_found"], "words_original": scores["words_original"],
        "words_missing": scores["words_missing"], "minutes": round(wall / 60), "wall_s": wall,
        "page_bytes": page.stat().st_size, **run_stats(case / "log.jsonl"),
    }
    (case / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n")
    run("ffmpeg", "-v", "error", "-y", "-i", original, "-i", render, "-filter_complex",
        "[0:v]scale=960:540[a];[1:v]scale=960:540[b];[a][b]hstack", "-c:v", "libx264", "-crf", "23", "-an",
        case / "side_by_side.mp4")
    print(json.dumps({k: v for k, v in summary.items() if k != "words_missing"}))
    if name != reel:
        return   # the run without video-lens is published as numbers only
    write_web(name, original, render, page)


if __name__ == "__main__":
    main(sys.argv[1])
