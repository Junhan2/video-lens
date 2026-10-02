#!/usr/bin/env python3
"""Score a recreation against its original with checks that do not depend on how the recreation was made.

  python3 compare.py ORIGINAL.mp4 RECREATION.mp4 [--json OUT.json]

look      SSIM of the two frames at the same moment (grayscale, 192x108), sampled 10 times a second; 1 = identical.
color     mean CIE76 colour difference of the same frames (Lab, 96x54); about 2 is barely visible, 50+ is a
          different picture.
cuts      hard cuts found by ffmpeg's scene score (> 0.3) in each clip, matched one to one within 0.1 s.
text      distinct words (letters only, 3+, in the macOS word list so OCR misreads drop out) read by video-lens OCR
          in the original, and the share found again in the recreation.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

SAMPLES_PER_S = 10
LOOK_SIZE = (192, 108)
COLOR_SIZE = (96, 54)
CUT_SCORE = 0.3
CUT_TOLERANCE_S = 0.1
VL = Path(os.environ.get("VIDEO_LENS_SKILL_LINK", Path.home() / ".claude/skills/video-lens")) / "scripts" / "vl.py"
WORD = re.compile(r"[A-Za-z]{3,}")
DICTIONARY = {line.strip().upper() for line in open("/usr/share/dict/words")}


def sampled_frames(path, times):
    """The frames at the given times, shrunk to COLOR_SIZE and LOOK_SIZE as they are read."""
    capture = cv2.VideoCapture(str(path))
    fps = capture.get(cv2.CAP_PROP_FPS)
    wanted = {round(t * fps): i for i, t in enumerate(times)}
    frames, index = [None] * len(times), 0
    while wanted and capture.grab():
        if index in wanted:
            frame = capture.retrieve()[1]
            frames[wanted.pop(index)] = (cv2.resize(frame, COLOR_SIZE, interpolation=cv2.INTER_AREA),
                                         cv2.resize(frame, LOOK_SIZE, interpolation=cv2.INTER_AREA))
        index += 1
    capture.release()
    return frames


def duration(path):
    capture = cv2.VideoCapture(str(path))
    seconds = capture.get(cv2.CAP_PROP_FRAME_COUNT) / capture.get(cv2.CAP_PROP_FPS)
    capture.release()
    return seconds


def ssim(a, b):
    a, b = a.astype(np.float64), b.astype(np.float64)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    blur = lambda x: cv2.GaussianBlur(x, (11, 11), 1.5)
    mu_a, mu_b = blur(a), blur(b)
    var_a, var_b = blur(a * a) - mu_a ** 2, blur(b * b) - mu_b ** 2
    cov = blur(a * b) - mu_a * mu_b
    return float((((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (var_a + var_b + c2))).mean())


def color_difference(a, b):
    lab = lambda x: cv2.cvtColor(x.astype(np.float32) / 255, cv2.COLOR_BGR2Lab)
    return float(np.linalg.norm(lab(a) - lab(b), axis=2).mean())


def frame_scores(original, recreation):
    seconds = min(duration(original), duration(recreation))
    times = [i / SAMPLES_PER_S for i in range(int(seconds * SAMPLES_PER_S))]
    pairs = zip(sampled_frames(original, times), sampled_frames(recreation, times))
    gray = lambda x: cv2.cvtColor(x, cv2.COLOR_BGR2GRAY)
    look, color = [], []
    for a, b in pairs:
        if a is None or b is None:
            break
        look.append(round(ssim(gray(a[1]), gray(b[1])), 4))
        color.append(round(color_difference(a[0], b[0]), 2))
    return look, color


def cuts(path):
    output = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-vf", f"select='gt(scene,{CUT_SCORE})',showinfo",
                             "-an", "-f", "null", "-"], capture_output=True, text=True).stderr
    return [float(t) for t in re.findall(r"pts_time:([0-9.]+)", output)]


def match_cuts(original, recreation):
    left, offsets = list(recreation), []
    for t in original:
        nearest = min(left, key=lambda r: abs(r - t), default=None)
        if nearest is not None and abs(nearest - t) <= CUT_TOLERANCE_S:
            offsets.append(round((nearest - t) * 1000))
            left.remove(nearest)
    return offsets


def ocr_words(video, out_dir):
    if not (out_dir / "analysis.json").exists():
        subprocess.run([sys.executable, str(VL), "analyze", str(video), "--out", str(out_dir), "--mode", "both"],
                       capture_output=True, check=True)
    text = subprocess.run([sys.executable, str(VL), "text", str(out_dir), "--kind", "ocr", "--max-lines", "100000"],
                          capture_output=True, text=True, check=True).stdout
    quoted = " ".join(re.findall(r'TEXT "([^"]*)"', text))
    return {word.upper() for word in WORD.findall(quoted)} & DICTIONARY


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("original", type=Path)
    parser.add_argument("recreation", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    look, color = frame_scores(args.original, args.recreation)
    original_cuts, recreation_cuts = cuts(args.original), cuts(args.recreation)
    offsets = match_cuts(original_cuts, recreation_cuts)
    work = (args.json or args.recreation).parent / "compare_vl"
    original_words = ocr_words(args.original, work / "original")
    recreation_words = ocr_words(args.recreation, work / "recreation")
    found = original_words & recreation_words
    result = {
        "look_mean": round(float(np.mean(look)), 3), "look_per_sample": look,
        "color_mean": round(float(np.mean(color)), 1), "color_per_sample": color,
        "cuts_original": len(original_cuts), "cuts_recreation": len(recreation_cuts), "cuts_matched": len(offsets),
        "cut_offset_ms_median": int(np.median(np.abs(offsets))) if offsets else None,
        "words_original": len(original_words), "words_found": len(found),
        "words_missing": sorted(original_words - recreation_words),
    }
    summary = {k: v for k, v in result.items() if not k.endswith("per_sample")}
    print(json.dumps(summary, ensure_ascii=False))
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")


if __name__ == "__main__":
    main()
