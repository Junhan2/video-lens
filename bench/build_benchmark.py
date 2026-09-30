#!/usr/bin/env python3
"""Fill docs/data/benchmark.json of the public repo from results.csv (written by score.py --csv).

Overall per condition: mean score, worst score, runs scoring 1, mean cost, mean time, mean turns.
Per task: median score, worst score, median cost, median time (3 runs each).
A model or a competitor skill is written only when every one of its runs is scored; otherwise it keeps its
current entry (for example "status": "pending"). Competitor skills ran on one model (skills.model), so their cells
go into that model's overall and per_task under the skill id, next to "baseline" and "skill".
--check prints what would change without writing.
"""
import argparse
import copy
import csv
import json
import statistics
from pathlib import Path

BENCH = Path(__file__).resolve().parent
DATA = BENCH.parent / "docs" / "data" / "benchmark.json"
MODEL_ARMS = {"opus-5.5": ("a0f", "bf"), "sonnet-5.5": ("a0s", "bs"), "grok-4.7": ("g0", "gs")}
SKILL_ARMS = {"watch": "w", "video-use": "vu"}   # competitor skills, all on Opus 5.5
PERFECT = 0.999


def load_rows(path):
    rows = {}
    for row in csv.DictReader(open(path)):
        if row.get("score"):
            rows.setdefault(row["arm"], []).append(row)
    return rows


def numbers(rows, key):
    return [float(row[key]) for row in rows]


def overall(rows):
    scores = numbers(rows, "score")
    return {
        "runs": len(rows),
        "mean_score": round(statistics.mean(scores), 3),
        "worst_score": round(min(scores), 3),
        "perfect_runs": sum(score >= PERFECT for score in scores),
        "cost_usd": round(statistics.mean(numbers(rows, "cost_usd")), 3),
        "time_s": round(statistics.mean(numbers(rows, "duration_s")), 1),
        "turns": round(statistics.mean(numbers(rows, "turns")), 1),
    }


def per_task(rows, task_ids):
    cells = {}
    for task in task_ids:
        cell = [row for row in rows if row["task"] == task]
        cells[task] = {
            "median_score": round(statistics.median(numbers(cell, "score")), 3),
            "worst_score": round(min(numbers(cell, "score")), 3),
            "median_cost_usd": round(statistics.median(numbers(cell, "cost_usd")), 3),
            "median_time_s": round(statistics.median(numbers(cell, "duration_s")), 1),
        }
    return cells


def is_complete(rows, task_ids, per_task_runs):
    return rows is not None and all(sum(row["task"] == task for row in rows) == per_task_runs for task in task_ids)


def build(data, rows):
    task_ids = [task["id"] for task in data["tasks"]]
    reps = data["runs_per_task"]
    changed = []
    for model_id, (base_arm, skill_arm) in MODEL_ARMS.items():
        base, skill = rows.get(base_arm), rows.get(skill_arm)
        if not (is_complete(base, task_ids, reps) and is_complete(skill, task_ids, reps)):
            continue
        model = data["models"][model_id]
        model.pop("status", None)
        model["overall"] = {"baseline": overall(base), "skill": overall(skill)}
        base_cells, skill_cells = per_task(base, task_ids), per_task(skill, task_ids)
        model["per_task"] = {task: {"baseline": base_cells[task], "skill": skill_cells[task]} for task in task_ids}
        changed.append(model_id)
    skills = data.get("skills")
    for skill_id, arm in SKILL_ARMS.items():
        if not skills or not is_complete(rows.get(arm), task_ids, reps):
            continue
        next(item for item in skills["arms"] if item["id"] == skill_id).pop("status", None)
        model = data["models"][skills["model"]]
        model["overall"][skill_id] = overall(rows[arm])
        for task, cell in per_task(rows[arm], task_ids).items():
            model["per_task"][task][skill_id] = cell
        changed.append(skill_id)
    return changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default=str(BENCH / "results.csv"))
    parser.add_argument("--check", action="store_true", help="report differences, write nothing")
    args = parser.parse_args()
    before = json.loads(DATA.read_text())
    after = copy.deepcopy(before)
    changed = build(after, load_rows(args.csv))
    print("complete:", ", ".join(changed) or "none")
    if before == after:
        print("benchmark.json unchanged")
        return
    if args.check:
        changed_models = [key for key in after["models"] if after["models"][key] != before["models"].get(key)]
        print("would change:", ", ".join(changed_models + (["skills"] if after.get("skills") != before.get("skills") else [])))
        return
    DATA.write_text(json.dumps(after, ensure_ascii=False, indent=1) + "\n")
    print("wrote", DATA)


if __name__ == "__main__":
    main()
