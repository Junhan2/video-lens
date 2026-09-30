#!/usr/bin/env python3
"""Rewrites everything generated from docs/data/benchmark.json: blocks in README*.md and docs/BENCHMARK*.md,
and the computed English sentences in docs/index.html.

  python3 tools/render_results.py           rewrite every block
  python3 tools/render_results.py --check   exit 1 if any block is out of date (writes nothing)

Blocks (only those present in a file are touched):
  <!-- results:start --> ... <!-- results:end -->     overall results and each model's setup
  <!-- per-task:start --> ... <!-- per-task:end -->   per-task medians per model
  <!-- tasks:start --> ... <!-- tasks:end -->         the task list
  <!-- skills:start --> ... <!-- skills:end -->       the model alone, with video-lens and with other video skills
  <!-- skills-per-task:start --> ... <!-- skills-per-task:end -->   the same comparison per task
  <!-- long-lecture:start -->...<!-- long-lecture:end -->   one sentence, start and end on the same line

docs/index.html: the elements site.js fills from the data (hero-summary, hero-models, why-spread, why-models,
bench-intro, when-long, method-setup, method-tasks-title, method-tasks) get the same English text, so the page
reads correctly before JavaScript runs. site.js replaces it when a language is chosen.

Labels come from docs/i18n/<lang>.json with English for missing keys: README.md and docs/BENCHMARK.md use en,
README.<lang>.md and docs/BENCHMARK.<lang>.md use <lang> (README.zh-CN.md -> zh-CN.json).
Models with "status": "pending" are skipped. Printed numbers keep the JSON's precision (scores and dollars
3 decimals, seconds and turns 1) and each one is checked to parse back to the exact JSON value. Percentages round
half up, like Intl.NumberFormat on the page.
Standard library only.
"""
import datetime
import json
import re
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from i18n import rich_html

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data" / "benchmark.json"
I18N = ROOT / "docs" / "i18n"
PAGE = ROOT / "docs" / "index.html"
SETUP_FIELDS = ("harness", "effort", "cost", "time")
SERIES = ("baseline", "skill")
GENERATED = "<!-- Generated from docs/data/benchmark.json by tools/render_results.py. Do not edit by hand. -->"


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def merged(base, overlay):
    result = dict(base)
    for key, value in overlay.items():
        result[key] = merged(base.get(key, {}), value) if isinstance(value, dict) else value
    return result


class Labels:
    """Strings of one language over English, plus number formatting that round-trips to the JSON."""

    def __init__(self, code):
        strings = load_json(I18N / "en.json")
        path = I18N / f"{code}.json"
        if code != "en" and path.exists():
            strings = merged(strings, load_json(path))
        self.strings = strings
        self.decimal = self.t("format.decimal")

    def t(self, key, **params):
        node = self.strings
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, str):
            raise KeyError(f"missing i18n key {key}")
        return re.sub(r"\{(\w+)\}", lambda m: str(params.get(m.group(1), m.group(0))), node)

    def number(self, value, digits):
        text = f"{value:.{digits}f}"
        if float(text) != float(value):
            raise ValueError(f"{value} does not fit {digits} decimals; the table would not equal the JSON")
        return text.replace(".", self.decimal)

    def score(self, value):
        return self.number(value, 3)

    def usd(self, value):
        return self.t("format.usd", n=self.number(value, 3))

    def seconds(self, value):
        return self.t("format.seconds", n=self.number(value, 1))

    def change(self, before, after):
        ratio = (after - before) / before
        if abs(ratio) < 0.005:
            return self.t("change.same")
        pct = self.t("format.percent", n=(Decimal(abs(ratio)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return self.t("change.lower" if ratio < 0 else "change.higher", pct=pct)

    def task(self, task, length):
        try:
            return self.t(f"tasks.{task['id']}.{length}")
        except KeyError:
            return task["label"]


def measured_models(data):
    return [model for model in data["models"].values()
            if model.get("status") != "pending" and "overall" in model and "per_task" in model]


def setup_line(data, model, labels):
    """How one model was run: the benchmark-wide settings overlaid with the model's own."""
    settings = {**data.get("settings", {}), **model.get("settings", {})}
    missing = [field for field in SETUP_FIELDS if not settings.get(field)]
    if missing:
        raise ValueError(f"{model['label']}: settings needs {', '.join(missing)} in benchmark.json")
    harness = settings["harness"]
    return labels.t("setup.line", model=model["label"], harness=harness, effort=settings["effort"],
                    cost=labels.t(f"setup.cost_{settings['cost']}", harness=harness),
                    time=labels.t(f"setup.time_{settings['time']}", harness=harness))


def table(header, rows, numeric_from):
    align = ["---" if i < numeric_from else "---:" for i in range(len(header))]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(align) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def overall_cells(cell, labels):
    """Mean score, worst run, runs scoring 1, cost, time and turns of one condition over all its runs."""
    return [labels.score(cell["mean_score"]), labels.score(cell["worst_score"]),
            labels.t("format.of", a=cell["perfect_runs"], b=cell["runs"]), labels.usd(cell["cost_usd"]),
            labels.seconds(cell["time_s"]), labels.number(cell["turns"], 1)]


def task_cells(cell, labels):
    """Median score, worst run, median cost and median time of one condition on one task."""
    return [labels.score(cell["median_score"]), labels.score(cell["worst_score"]),
            labels.usd(cell["median_cost_usd"]), labels.seconds(cell["median_time_s"])]


def results_block(data, labels):
    models = measured_models(data)
    header = [labels.t(f"table.{key}") for key in
              ("model", "condition", "mean_score", "worst_score", "perfect_runs", "cost", "time", "turns")]
    rows = [[model["label"], labels.t(f"series.{series}"), *overall_cells(model["overall"][series], labels)]
            for model in models for series in SERIES]
    changes = [labels.t("results.change_line", model=model["label"],
                        cost=labels.change(model["overall"]["baseline"]["cost_usd"], model["overall"]["skill"]["cost_usd"]),
                        time=labels.change(model["overall"]["baseline"]["time_s"], model["overall"]["skill"]["time_s"]))
               for model in models]
    note = labels.t("results.note", date=data["measured_on"], tasks=data["tasks_count"],
                    reps=data["runs_per_task"], runs=data["runs_per_condition"])
    setups = [setup_line(data, model, labels) for model in models]
    return "\n\n".join([table(header, rows, 2), bullets(changes), note, bullets(setups)])


def bullets(lines):
    return "\n".join(f"- {line}" for line in lines)


def per_task_block(data, labels):
    header = [labels.t(f"table.{key}") for key in
              ("task", "condition", "median_score", "worst_score", "median_cost", "median_time")]
    parts = []
    for model in measured_models(data):
        rows = []
        for task in data["tasks"]:
            if task["id"] not in model["per_task"]:
                continue
            for series in SERIES:
                rows.append([labels.task(task, "long") if series == "baseline" else "", labels.t(f"series.{series}"),
                             *task_cells(model["per_task"][task["id"]][series], labels)])
        parts.append(f"### {model['label']}\n\n{table(header, rows, 2)}")
    parts.append(labels.t("results.per_task_note", reps=data["runs_per_task"]))
    return "\n\n".join(parts)


def skill_conditions(data):
    """(model, arms, keys): the model the other video skills ran on, its measured competitor arms, and the condition
    keys in the page's row order (alone, video-lens, then each competitor). keys is empty until a competitor has data."""
    skills = data.get("skills")
    model = skills and data["models"].get(skills["model"])
    if not model or model not in measured_models(data):
        return model, [], []
    arms = [arm for arm in skills["arms"] if arm.get("status") != "pending" and arm["id"] in model["overall"]]
    return model, arms, [*SERIES, *(arm["id"] for arm in arms)] if arms else []


def skills_text(data, labels):
    """The intro sentence and the notes (one per competitor, then the cost note), shared by README and page."""
    model, arms, _ = skill_conditions(data)
    intro = labels.t("skills.intro", model=model["label"], tasks=data["tasks_count"], runs=data["runs_per_condition"])
    return intro, [labels.t(f"skills.note.{arm['id']}", version=arm["version"]) for arm in arms] + [labels.t("skills.cost_note")]


def skills_block(data, labels):
    model, _, keys = skill_conditions(data)
    if not keys:
        return labels.t("skills.pending")
    header = [labels.t(f"table.{key}") for key in
              ("condition", "mean_score", "worst_score", "perfect_runs", "cost", "time", "turns")]
    rows = [[labels.t(f"series.{key}"), *overall_cells(model["overall"][key], labels)] for key in keys]
    intro, notes = skills_text(data, labels)
    return "\n\n".join([intro, table(header, rows, 1), bullets(notes)])


def skills_per_task_block(data, labels):
    model, _, keys = skill_conditions(data)
    if not keys:
        return labels.t("skills.pending")
    header = [labels.t(f"table.{key}") for key in
              ("task", "condition", "median_score", "worst_score", "median_cost", "median_time")]
    rows = [[labels.task(task, "long") if index == 0 else "", labels.t(f"series.{key}"),
             *task_cells(model["per_task"][task["id"]][key], labels)]
            for task in data["tasks"] for index, key in enumerate(keys)]
    title = labels.t("skills.per_task_title", model=model["label"])
    return "\n\n".join([f"### {title}", table(header, rows, 2), labels.t("results.per_task_note", reps=data["runs_per_task"])])


def tasks_block(data, labels):
    rows = [[f"`{task['id']}`", labels.task(task, "long")] for task in data["tasks"]]
    return table([labels.t("table.id"), labels.t("table.task")], rows, 2)


def long_lecture(data, labels):
    """The 10-minute lecture (h3) for the first measured model, as the page's when-long sentence."""
    model = measured_models(data)[0]
    cell = model["per_task"]["h3"]
    return labels.t("when.use_long", model=model["label"], reps=data["runs_per_task"],
                    cost=labels.change(cell["baseline"]["median_cost_usd"], cell["skill"]["median_cost_usd"]),
                    time=labels.change(cell["baseline"]["median_time_s"], cell["skill"]["median_time_s"]))


BLOCKS = {"results": results_block, "per-task": per_task_block, "tasks": tasks_block, "skills": skills_block,
          "skills-per-task": skills_per_task_block}
INLINE_BLOCKS = {"long-lecture": long_lecture}


def language_of(path):
    parts = path.name.split(".")
    return parts[1] if len(parts) == 3 else "en"


def rendered(text, data, labels, path):
    for name, build in BLOCKS.items():
        start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
        if start not in text and end not in text:
            continue
        if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
            raise ValueError(f"{path.name}: expected one {start} before one {end}")
        before, rest = text.split(start, 1)
        _, after = rest.split(end, 1)
        text = f"{before}{start}\n{GENERATED}\n\n{build(data, labels)}\n\n{end}{after}"
    for name, build in INLINE_BLOCKS.items():
        start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
        if start not in text and end not in text:
            continue
        pattern = re.compile(f"({re.escape(start)})[^\n]*?({re.escape(end)})")
        if text.count(start) != 1 or text.count(end) != 1 or not pattern.search(text):
            raise ValueError(f"{path.name}: expected one {start} and one {end} on the same line")
        text = pattern.sub(lambda match: f"{match.group(1)}{build(data, labels)}{match.group(2)}", text)
    return text


def long_date(iso_date):
    """What Intl.DateTimeFormat('en', { dateStyle: 'long' }) prints, e.g. September 30, 2026."""
    date = datetime.date.fromisoformat(iso_date)
    months = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December")
    return f"{months[date.month - 1]} {date.day}, {date.year}"


def page_content(data, labels):
    """Per element id: a rich string, or a list of HTML lines (list items, table rows). Same text as site.js."""
    models = measured_models(data)
    counts = {"tasks": data["tasks_count"], "reps": data["runs_per_task"], "runs": data["runs_per_condition"]}

    def overall(model, field):
        return labels.change(model["overall"]["baseline"][field], model["overall"]["skill"][field])

    def scores(model, field):
        return {f"skill_{field}": labels.score(model["overall"]["skill"][f"{field}_score"]),
                f"base_{field}": labels.score(model["overall"]["baseline"][f"{field}_score"])}

    spread_model, spread_task = min(((model, task) for model in models for task in data["tasks"]
                                     if task["id"] in model["per_task"]),
                                    key=lambda pair: pair[0]["per_task"][pair[1]["id"]]["baseline"]["worst_score"])
    spread = spread_model["per_task"][spread_task["id"]]["baseline"]
    skills_intro, skill_notes = skills_text(data, labels) if skill_conditions(data)[2] else ("", [])
    rows = [f'<tr><th scope="row">{task["id"]}</th><td>{rich_html(labels.task(task, "long"))}</td></tr>'
            for task in data["tasks"]]
    return {
        "hero-summary": labels.t("hero.summary", **counts),
        "hero-models": items(labels.t("hero.summary_model", model=model["label"], cost=overall(model, "cost_usd"),
                                      time=overall(model, "time_s"), **scores(model, "mean")) for model in models),
        "why-spread": labels.t("why.alone_spread", task=labels.task(spread_task, "short"), model=spread_model["label"],
                               reps=counts["reps"], median=labels.score(spread["median_score"]),
                               worst=labels.score(spread["worst_score"])),
        "why-models": items(labels.t("why.alone_model", model=model["label"], cost=overall(model, "cost_usd"),
                                     time=overall(model, "time_s"), **scores(model, "mean"), **scores(model, "worst"))
                            for model in models),
        "bench-intro": labels.t("bench.intro", **counts, date=long_date(data["measured_on"])),
        "when-long": long_lecture(data, labels),
        "method-setup": items(setup_line(data, model, labels) for model in models),
        "method-tasks-title": labels.t("method.tasks_title", **counts),
        "method-tasks": rows,
        "skills-intro": skills_intro,
        "skills-notes": items(skill_notes),
    }


def items(texts):
    return [f"<li>{rich_html(text)}</li>" for text in texts]


def rendered_page(page, data, labels):
    for element_id, content in page_content(data, labels).items():
        pattern = re.compile(rf'(<([a-zA-Z][\w-]*)\b[^>]*\sid="{re.escape(element_id)}"[^>]*>)(.*?)(</\2\s*>)', re.S)
        matches = list(pattern.finditer(page))
        if len(matches) != 1:
            raise ValueError(f"index.html: expected one element with id {element_id}, found {len(matches)}")
        line_start = page.rfind("\n", 0, matches[0].start()) + 1
        indent = re.match(r"[ \t]*", page[line_start:]).group(0)
        if isinstance(content, str):
            inner = rich_html(content)
        else:
            inner = "".join(f"\n{indent}  {line}" for line in content) + f"\n{indent}"
        page = pattern.sub(lambda match: f"{match.group(1)}{inner}{match.group(4)}", page, count=1)
    return page


def main(argv):
    is_check = "--check" in argv
    data = load_json(DATA)
    files = sorted(ROOT.glob("README*.md")) + sorted((ROOT / "docs").glob("BENCHMARK*.md"))
    stale = []
    for path in files + [PAGE]:
        text = path.read_text(encoding="utf-8")
        if path == PAGE:
            updated = rendered_page(text, data, Labels("en"))
        else:
            updated = rendered(text, data, Labels(language_of(path)), path)
        if updated == text:
            print(f"{path.relative_to(ROOT)}: up to date")
            continue
        stale.append(path)
        if not is_check:
            path.write_text(updated, encoding="utf-8")
        print(f"{path.relative_to(ROOT)}: {'out of date' if is_check else 'updated'} ({language_of(path)})")
    return 1 if is_check and stale else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
