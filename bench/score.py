#!/usr/bin/env python3
"""Score benchmark runs in $VLB_RUNS (default ~/vlb-runs) against evalset/truth.json and hardset/truth_hard.json
and summarise cost per arm.

Usage: python3 score.py [--csv out.csv] [--detail]
Each run folder holds run.jsonl (claude -p stream-json). The answer is the last ```json block of the final result.
"""
import argparse
import csv
import json
import os
import re
import statistics
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
RUNS = Path(os.environ.get("VLB_RUNS") or Path.home() / "vlb-runs")
TRUTH_FILES = [BENCH / "evalset" / "truth.json", BENCH / "hardset" / "truth_hard.json"]
VIDEOS = {v["file"].split("_")[0]: v for f in TRUTH_FILES if f.exists() for v in json.loads(f.read_text())["videos"]}

NAMED_EASINGS = {
    "linear": (0, 0, 1, 1), "ease": (0.25, 0.1, 0.25, 1), "ease-in": (0.42, 0, 1, 1),
    "ease-out": (0, 0, 0.58, 1), "ease-in-out": (0.42, 0, 0.58, 1),
}
FRAME_S = 1 / 60
EPS = 0.0015
PROPERTY_KEYWORDS = {
    "translatex": ("translatex", "translate(", "x"), "translatey": ("translatey", "translate(0", "y"),
    "scale": ("scale",), "opacity": ("opacity",), "rotate": ("rotate",),
    "background-color": ("background", "color", "색"),
}


def frame_s(video):
    """Source frame interval: VFR files keep the source rate in r_frame_rate."""
    num, den = video["container"]["video"]["r_frame_rate"].split("/")
    return float(den) / float(num)


def required_props(event):
    names = [(p.get("function") or p["property"]).lower() for p in event["properties"]]
    return [PROPERTY_KEYWORDS.get(n, (n,)) for n in names]


def stagger_members(video):
    return {m for g in (video.get("groups") or []) if g.get("type") == "stagger" for m in g["members"]}
# Approximate transition starts (s) read by hand from the author's private carousel recording, which is not shipped.
# With your own recording, replace them (and the count and period checks in score_orbit) with your clip's key.
ORBIT_STARTS = [0.12, 1.75, 3.33, 4.87, 6.41, 7.96, 9.57, 11.03, 12.68, 14.24, 15.79]


def bezier_y_at_x(p, x):
    x1, y1, x2, y2 = p

    def coord(t, a, b):
        return 3 * a * t * (1 - t) ** 2 + 3 * b * t ** 2 * (1 - t) + t ** 3

    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if coord(mid, x1, x2) < x:
            lo = mid
        else:
            hi = mid
    return coord((lo + hi) / 2, y1, y2)


def parse_easing(text):
    if not text or not isinstance(text, str):
        return None
    m = re.search(r"cubic-bezier\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)", text)
    if m:
        return tuple(float(g) for g in m.groups())
    return NAMED_EASINGS.get(text.strip().lower())


def is_easing_correct(reported, truth_easing):
    p = parse_easing(reported)
    if p is None:
        return False
    truth_p = (truth_easing["p1x"], truth_easing["p1y"], truth_easing["p2x"], truth_easing["p2y"])
    if all(abs(a - b) <= 0.1 for a, b in zip(p, truth_p)):
        return True
    worst = max(abs(bezier_y_at_x(p, float(x)) - y) for x, y in truth_easing["progress_at_x"].items())
    return worst <= 0.05


def extract_answer(result_text):
    blocks = re.findall(r"```json\s*(.*?)```", result_text or "", re.S)
    for block in reversed(blocks):
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            continue
    return None


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def props_text(item):
    props = item.get("properties") or []
    if isinstance(props, str):
        props = [props]
    return " ".join(str(p) for p in props).lower().replace(" ", "")


DISTRACTOR_WORDS = {"v1": ("caret", "커서", "cursor", "blink", "깜빡"), "v3": ("spinner", "스피너", "로딩", "rotate", "회전", "loading"),
                    "h1": ("shimmer", "skeleton", "스켈레톤", "로딩", "반짝", "loading")}


def merge_split_items(items):
    """One element animating several properties with the same timing may be reported as one item per property."""
    merged = []
    for item in items:
        start, dur = as_float(item.get("start_s")), as_float(item.get("duration_ms"))
        twin = next((m for m in merged if start is not None and as_float(m.get("start_s")) is not None
                     and abs(as_float(m.get("start_s")) - start) <= FRAME_S + EPS
                     and as_float(m.get("duration_ms")) == dur), None)
        if twin is None:
            merged.append(dict(item))
            continue
        props = twin.get("properties") or []
        extra = item.get("properties") or []
        twin["properties"] = (props if isinstance(props, list) else [props]) + (extra if isinstance(extra, list) else [extra])
    return merged


def score_motion(task, answer):
    video = VIDEOS[task]
    items = merge_split_items([m for m in (answer.get("motion") or []) if isinstance(m, dict)])
    checks, notes, used = [], [], set()
    frame = frame_s(video)
    staggered = stagger_members(video)
    for event in video["events"]:
        start = event["start_s"]
        candidates = [(abs((as_float(m.get("start_s")) or 99) - start), i) for i, m in enumerate(items) if i not in used]
        candidates = [c for c in candidates if c[0] <= 0.1]
        if not candidates:
            checks += [False] * (5 if event["id"] in staggered else 4)
            notes.append(f"{event['id']}: not found")
            continue
        _, idx = min(candidates)
        used.add(idx)
        item = items[idx]
        rep_start = as_float(item.get("start_s"))
        rep_dur = as_float(item.get("duration_ms"))
        css = event["duration_ms"] / 1000
        observed = event["observed_in_file"]["last_change_pts_s"] - start
        start_ok = rep_start is not None and abs(rep_start - start) <= frame + EPS
        dur_ok = rep_dur is not None and (
            abs(rep_dur / 1000 - css) <= frame + EPS or observed - frame - EPS <= rep_dur / 1000 <= css + EPS)
        ease_ok = is_easing_correct(item.get("easing"), event["easing"])
        ptxt = props_text(item)
        props_ok = all(any(k in ptxt for k in keys) for keys in required_props(event))
        checks += [start_ok, dur_ok, ease_ok, props_ok]
        detail = f"{event['id']}: start {rep_start} {'ok' if start_ok else 'X'}, dur {rep_dur} {'ok' if dur_ok else 'X'}, " \
                 f"ease {item.get('easing')} {'ok' if ease_ok else 'X'}, props {'ok' if props_ok else 'X'}"
        if event["id"] in staggered:
            rep_delay = as_float(item.get("delay_ms"))
            delay_ok = rep_delay is not None and abs(rep_delay - event["delay_ms"]) <= frame * 1000 + 1.5
            checks.append(delay_ok)
            detail += f", delay {rep_delay} {'ok' if delay_ok else 'X'}"
        notes.append(detail)
    extras = [m for i, m in enumerate(items) if i not in used]
    false_pos = len(extras)
    words = DISTRACTOR_WORDS.get(task, ())
    if any(any(w in json.dumps(m, ensure_ascii=False).lower() for w in words) for m in extras):
        notes.append("distractor reported as intended motion")
    if extras:
        notes.append(f"{len(extras)} extra motion item(s)")
    if task == "v3":
        audio = [as_float(a.get("t_s")) for a in (answer.get("audio") or []) if isinstance(a, dict)]
        audio_ok = any(t is not None and abs(t - 1.23) <= 0.010 for t in audio)
        sync = as_float(answer.get("sound_minus_motion_ms"))
        sync_ok = sync is not None and abs(sync - 30) <= 10
        checks += [audio_ok, sync_ok]
        notes.append(f"audio {audio} {'ok' if audio_ok else 'X'}, sync {sync} {'ok' if sync_ok else 'X'}")
    return checks, false_pos, notes


def normalise(text):
    return re.sub(r"\s+", " ", str(text)).strip().lower().replace("’", "'").replace("“", '"').replace("”", '"')


def cer(ref, hyp):
    ref, hyp = re.sub(r"[\s.,!?]", "", ref), re.sub(r"[\s.,!?]", "", hyp)
    prev = list(range(len(hyp) + 1))
    for i, rc in enumerate(ref, 1):
        cur = [i]
        for j, hc in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / max(1, len(ref))


def score_lecture(task, answer):
    video = VIDEOS[task]
    cut_tol = frame_s(video) + EPS
    checks, notes = [], []
    cuts = sorted(t for t in (as_float(c) for c in (answer.get("cuts_s") or [])) if t is not None and t > 0.2)
    for truth_cut in video["cuts_s"]:
        ok = any(abs(c - truth_cut) <= cut_tol for c in cuts)
        checks.append(ok)
    false_pos = sum(1 for c in cuts if all(abs(c - t) > cut_tol for t in video["cuts_s"]))
    notes.append(f"cuts {cuts}")
    slides = [s for s in (answer.get("slides") or []) if isinstance(s, dict)]
    all_text = " \n ".join(normalise(" ".join(s.get("text") or []) if isinstance(s.get("text"), list) else s.get("text", "")) for s in slides)
    for slide in video["text"]:
        for key in ("title_ko", "line_en", "footer"):
            truth_line = normalise(slide[key])
            exact = truth_line in all_text or re.sub(r"\s", "", truth_line) in re.sub(r"\s", "", all_text)
            if not exact and key == "title_ko":
                best = min((cer(truth_line, normalise(line)) for s in slides for line in (s.get("text") or [])), default=1)
                checks.append(0.5 if best <= 0.05 else False)
            else:
                checks.append(exact)
    speech = [s for s in (answer.get("speech") or []) if isinstance(s, dict)]
    for truth in video["narration"]:
        match = min(speech, key=lambda s: abs((as_float(s.get("start_s")) or 99) - truth["start_s"]), default=None)
        if match is None:
            checks += [False, False, False]
            continue
        rate = cer(truth["text"], str(match.get("text", "")))
        checks.append(True if rate == 0 else (0.5 if rate <= 0.05 else False))
        checks.append(abs((as_float(match.get("start_s")) or 99) - truth["start_s"]) <= 0.15)
        checks.append(abs((as_float(match.get("end_s")) or 99) - truth["end_s"]) <= 0.15)
        notes.append(f"say {truth['slide']}: CER {rate:.3f}, {match.get('start_s')}-{match.get('end_s')}")
    return checks, false_pos, notes


def score_orbit(answer):
    items = [m for m in (answer.get("motion") or []) if isinstance(m, dict)]
    starts = sorted(t for t in (as_float(m.get("start_s")) for m in items) if t is not None)
    count_ok = abs(len(starts) - 11) <= 1
    diffs = [b - a for a, b in zip(starts, starts[1:])]
    period_ok = bool(diffs) and 1.45 <= statistics.median(diffs) <= 1.65
    offset = statistics.median([min(starts, key=lambda s: abs(s - t)) - t for t in ORBIT_STARTS]) if starts else 0
    matched = sum(1 for t in ORBIT_STARTS if any(abs(s - offset - t) <= 0.05 for s in starts)) / len(ORBIT_STARTS)
    durations = [as_float(m.get("duration_ms")) for m in items if as_float(m.get("duration_ms"))]
    easings = [parse_easing(m.get("easing")) for m in items if parse_easing(m.get("easing"))]
    s_curve_ok = bool(easings) and all(bezier_y_at_x(p, 0.25) < 0.25 and bezier_y_at_x(p, 0.75) > 0.75 for p in easings)
    text = json.dumps(answer, ensure_ascii=False).lower()
    horizontal_ok = any(w in text for w in ("horizontal", "가로", "좌", "우", "left", "right", "옆", "translatex"))
    checks = [count_ok, period_ok, matched >= 0.8, s_curve_ok, horizontal_ok]
    notes = [f"n={len(starts)} period={statistics.median(diffs) if diffs else None} offset={offset:+.3f} matched={matched:.2f} "
             f"dur_med={statistics.median(durations) if durations else None} easings={easings[:2]}"]
    return checks, 0, notes


def score_gist(answer):
    # Keyword rubric for the author's private carousel recording; write your own for a different clip.
    text = json.dumps(answer, ensure_ascii=False).lower()
    rubric = [
        any(w in text for w in ("carousel", "캐러셀", "strip", "띠", "스트립", "슬라이드", "가로", "horizontal", "흘러", "넘어", "회전")),
        any(w in text for w in ("center", "가운데", "중앙", "frame", "틀", "창", "watch", "워치", "aperture")),
        any(w in text for w in ("the same strip", "aperture", "worn")),
        any(w in text for w in ("card", "카드", "illustration", "일러스트", "그림", "이미지")),
    ]
    return rubric, 0, []


def is_grok_log(log_path):
    with open(log_path) as fh:
        return '"available_commands"' in fh.readline()


def grok_metrics(log_path):
    """Grok Build CLI streaming-json: answer text arrives as `text` deltas, totals in the final `end` event."""
    text, end, tool_calls, image_reads, leaked, skill_used = [], None, 0, 0, False, False
    for line in log_path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "text":
            text.append(event.get("data") or "")
        elif kind == "end":
            end = event
        elif kind == "tool_call":
            tool_calls += 1
            payload = json.dumps(event.get("rawInput", {}), ensure_ascii=False)
            if event.get("title") == "read_file" and re.search(r"\.(png|jpe?g|webp)", payload, re.I):
                image_reads += 1
            if "video-lens-bench" in payload or "truth.json" in payload:
                leaked = True
            if "vl.py" in payload:
                skill_used = True
    if end is None:
        return None, tool_calls, image_reads, leaked, skill_used
    wall = log_path.parent / "wall.json"
    duration_ms = json.loads(wall.read_text())["wall_s"] * 1000 if wall.exists() else None
    result = {"type": "result", "subtype": "success" if end.get("stopReason") == "end_turn" else end.get("stopReason"),
              "result": "".join(text), "total_cost_usd": end.get("total_cost_usd"), "num_turns": end.get("num_turns"),
              "duration_ms": duration_ms, "usage": end.get("usage", {})}
    return result, tool_calls, image_reads, leaked, skill_used


# A run that touches the bench folder or an answer key is flagged as a holdout leak. "video-lens-bench" is the
# folder name the published runs used and the one the README suggests for your copy.
LEAK_MARKS = ("video-lens-bench", str(BENCH), "truth.json")


def run_metrics(log_path):
    if is_grok_log(log_path):
        return grok_metrics(log_path)
    results, tool_calls, image_reads, leaked, skill_used = [], 0, 0, False, False
    for line in log_path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            results.append(event)
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "tool_use":
                continue
            tool_calls += 1
            payload = json.dumps(block.get("input", {}), ensure_ascii=False)
            if block.get("name") == "Read" and re.search(r"\.(png|jpe?g|webp)", payload, re.I):
                image_reads += 1
            if any(mark in payload for mark in LEAK_MARKS) or f"{RUNS.name}/" in payload and "/work" not in payload:
                leaked = True
            if "video-lens" in payload or block.get("name") == "Skill":
                skill_used = True
    return combine_results(results), tool_calls, image_reads, leaked, skill_used


def combine_results(results):
    """A run can emit several result events (e.g. a background task finishing after the answer).
    total_cost_usd is cumulative; turns, duration and usage are per result; the answer is the last one with JSON."""
    if not results:
        return None
    with_answer = [r for r in results if extract_answer(r.get("result", ""))]
    combined = dict(with_answer[-1] if with_answer else results[-1])
    combined["total_cost_usd"] = max(r.get("total_cost_usd") or 0 for r in results)
    combined["num_turns"] = sum(r.get("num_turns") or 0 for r in results)
    combined["duration_ms"] = sum(r.get("duration_ms") or 0 for r in results)
    usage = {}
    for r in results:
        for key, value in (r.get("usage") or {}).items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
    combined["usage"] = usage
    return combined


def scorer_for(task):
    if task in ("orbit", "gist"):
        return None
    return score_lecture if VIDEOS[task].get("narration") else score_motion


def score_run(run_dir):
    task, arm, rep = run_dir.name.rsplit("-", 2)
    log_path = run_dir / "run.jsonl"
    if not log_path.exists():
        return None
    result, tool_calls, image_reads, leaked, skill_used = run_metrics(log_path)
    if result is None:
        return {"task": task, "arm": arm, "rep": rep, "status": "no result"}
    answer = extract_answer(result.get("result", ""))
    if answer is None:
        checks, false_pos, notes = [False], 0, ["no parsable JSON answer"]
    elif scorer_for(task):
        checks, false_pos, notes = scorer_for(task)(task, answer)
    elif task == "orbit":
        checks, false_pos, notes = score_orbit(answer)
    else:
        checks, false_pos, notes = score_gist(answer)
    points = sum(float(c) for c in checks)
    score = max(0.0, points - false_pos) / len(checks)
    usage = result.get("usage", {})
    wall = json.loads((run_dir / "wall.json").read_text())["wall_s"] if (run_dir / "wall.json").exists() else None
    return {
        "task": task, "arm": arm, "rep": rep, "status": result.get("subtype"),
        "score": round(score, 3), "checks": f"{points:g}/{len(checks)}", "false_pos": false_pos,
        "cost_usd": round(result.get("total_cost_usd") or 0, 4), "turns": result.get("num_turns"),
        "duration_s": round((result.get("duration_ms") or 0) / 1000, 1), "wall_s": wall,
        "input_tokens": usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
        "output_tokens": usage.get("output_tokens"), "tool_calls": tool_calls, "image_reads": image_reads,
        "skill_used": skill_used, "leaked": leaked, "notes": " | ".join(notes),
    }


def summarise(rows):
    by_cell = {}
    for row in rows:
        if "score" in row:
            by_cell.setdefault((row["task"], row["arm"]), []).append(row)
    print(f"{'task':6} {'arm':6} {'n':>2} {'score med':>9} {'min':>5} {'cost med $':>10} {'turns':>5} {'time s':>7} {'imgs':>5} {'pts per $':>9}")
    for (task, arm), cell in sorted(by_cell.items()):
        med = statistics.median
        score_med = med(r["score"] for r in cell)
        cost_med = med(r["cost_usd"] for r in cell)
        print(f"{task:6} {arm:6} {len(cell):>2} {score_med:>9.3f} {min(r['score'] for r in cell):>5.2f} {cost_med:>10.3f} "
              f"{med(r['turns'] or 0 for r in cell):>5} {med(r['duration_s'] for r in cell):>7.0f} "
              f"{med(r['image_reads'] for r in cell):>5} {score_med / cost_med if cost_med else 0:>9.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--detail", action="store_true")
    args = ap.parse_args()
    rows = [r for r in (score_run(d) for d in sorted(RUNS.iterdir()) if d.is_dir()) if r]
    if args.detail:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
    summarise(rows)
    if any(r.get("leaked") for r in rows):
        print("WARNING: holdout leak in", [f"{r['task']}-{r['arm']}-{r['rep']}" for r in rows if r.get("leaked")], file=sys.stderr)
    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=sorted({k for r in rows for k in r}))
            writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    main()
