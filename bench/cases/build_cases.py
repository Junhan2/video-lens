#!/usr/bin/env python3
"""Writes docs/data/cases.json from runs/*/summary.json and sources.tsv, and copies each case's web files
(web/NAME.mp4, .jpg, .html from finish_case.py) to docs/assets/cases/ and docs/cases/.

  python3 build_cases.py [--check]   --check prints what would change and writes nothing
Likes and views are Revid's counts of September 28, 2026 (sources.tsv).
"""
import csv
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOCS = HERE.parent.parent / "docs"
MODEL = "Claude Opus 5.5"
SCORES = ("look", "cuts_matched", "cuts_original", "words_found", "words_original", "minutes", "cost_usd")


def case_entry(source):
    name = source["name"]
    summary = json.loads((HERE / "runs" / name / "summary.json").read_text())
    alone = json.loads((HERE / "runs" / f"{name}-base" / "summary.json").read_text())
    return {
        "id": name, "author": source["author"], "url": source["url"], "made_with": source["made_with"],
        "likes": source["likes"], "views": source["views"],
        "video": f"assets/cases/{name}.mp4", "poster": f"assets/cases/{name}.jpg", "page": f"./cases/{name}.html",
        "look_floor": summary["look_floor"], **{key: summary[key] for key in SCORES},
        "alone": {key: alone[key] for key in SCORES},   # the same model and prompt without video-lens
    }


def main(is_check):
    sources = list(csv.DictReader((HERE / "sources.tsv").open(), delimiter="\t"))
    # The prompt is the same for every reel except the render length; the page shows the first reel's.
    prompt = (HERE / "runs" / sources[0]["name"] / "prompt.txt").read_text().strip()
    data = {"model": MODEL, "prompt": prompt, "cases": [case_entry(source) for source in sources]}
    target = DOCS / "data" / "cases.json"
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    if is_check:
        print("cases.json", "up to date" if target.exists() and target.read_text() == text else "out of date")
        return
    target.write_text(text)
    (DOCS / "assets" / "cases").mkdir(exist_ok=True)
    (DOCS / "cases").mkdir(exist_ok=True)
    for source in sources:
        name = source["name"]
        for suffix in ("mp4", "jpg"):
            shutil.copy(HERE / "web" / f"{name}.{suffix}", DOCS / "assets" / "cases" / f"{name}.{suffix}")
        shutil.copy(HERE / "web" / f"{name}.html", DOCS / "cases" / f"{name}.html")
    print("wrote", target)


if __name__ == "__main__":
    main("--check" in sys.argv)
