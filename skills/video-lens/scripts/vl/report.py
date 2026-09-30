"""report.md (stdout, at most --budget-chars), timeline.md, and the `text` and `rows` view commands (spec 7.11).

Every number comes from run.analysis; nothing is measured here. A table that does not fit the budget ends in a
pointer to the command that prints the rest, so the report never drops rows silently.
"""
import bisect
import math
import re
from dataclasses import dataclass

from . import sheets_motion, views
from .cache import write_atomic
from .context import clock
from .errors import EXIT_BAD_ARGS, VlError

TIMELINE_FILE = "timeline.md"
BLOCK_BREAK_CHANGE = 0.30       # a keyframe changing this much starts a timeline block (spec 7.11)
SPEECH_GAP_S = 2.0              # so does speech after this much silence
BLOCK_MIN_S = 20.0              # blocks are merged until they span this long
LONG_BLOCK_MIN_S = 60.0         # ... or this long for videos over 10 min
LONG_VIDEO_S = 600.0
SPEECH_PREVIEW_CHARS = 60
LOW_OCR_CONF = 0.6
SUGGESTIONS_MAX = 4
MICRO_TIMES_SHOWN = 6
MINOR_ROW_KINDS = ("EVENT", "BLIP", "SOUND")
MINOR_JOIN_S = 1.0              # minor rows closer than this share one timeline line
SCENE_CHANGE_KINDS = ("cut", "transition")
TIMELINE_EVENT_KINDS = ("transition", "transient", "update")
INSTANT_FRAMES = 2              # an event of this many changed frames that nothing fitted is a swap
TIMELINE_FIRST_SHARE = 0.5      # mode both: speech over this share of the range puts timeline.md first in Next
CSS_INLINE_CHARS = 1000         # css snippets up to this size are printed in the report
NEAR_TIE_NOTE = "curve cannot be told apart"     # the easing column already shows `~ name`
TIES_SHOWN = 4
INSTANT_CELL = "instant (<= 2 frames)"
FRAME_VIEW_LINE = "- L3 vl.py frame OUT --t {t} {roi}· <=4,761 tok (native crop)"
ZOOM_COLUMNS = 4
ELEMENT_HEADER = ["| el | types | start | delay | duration (range) | easing (rmse) | travel css px | conf |",
                  "|---|---|---|---|---|---|---|---|"]
NON_WORD = re.compile(r"[\W_]+")
CUT_NOTE = "(report cut at --budget-chars: read timeline.md or use vl.py rows)\n"


# ---------------------------------------------------------------- budgeted layout

@dataclass
class Table:
    """Lines around rows that may be cut from the end; `pointer(first_hidden, hidden)` names where the rest is."""
    head: list
    rows: list
    pointer: object
    shown: int = None

    def lines(self):
        shown = self.shown_count()
        hidden = len(self.rows) - shown
        return self.head + self.rows[:shown] + ([self.pointer(shown, hidden)] if hidden else [])

    def shown_count(self):
        return len(self.rows) if self.shown is None else self.shown


def render_parts(parts):
    lines = []
    for part in parts:
        lines += part.lines() if isinstance(part, Table) else part
    return "\n".join(lines) + "\n"


def fit_budget(parts, budget):
    """Cuts rows from the table showing the most rows until the text fits. As a last resort whole trailing parts
    go (so an earlier table keeps its pointer), then a hard cut keeps the cap."""
    text = render_parts(parts)
    while len(text) > budget:
        tables = [p for p in parts if isinstance(p, Table) and p.shown_count() > 1]
        if not tables:
            break
        table = max(tables, key=Table.shown_count)
        shown = table.shown_count()
        mean_row = max(1, sum(map(len, table.rows[:shown])) // shown + 1)
        table.shown = max(1, shown - max(1, math.ceil((len(text) - budget) / mean_row)))
        text = render_parts(parts)
    kept = list(parts)
    while len(text) > budget and len(kept) > 1:
        kept.pop()
        text = render_parts(kept)
        if len(text) + len(CUT_NOTE) <= budget:
            return text + CUT_NOTE
    if len(text) > budget:
        text = text[:budget - len(CUT_NOTE) - 1].rsplit("\n", 1)[0] + "\n" + CUT_NOTE
    return text


def rows_pointer(kind):
    return lambda first_hidden, hidden: f"+{hidden} rows: vl.py rows OUT --kind {kind} --from {first_hidden + 1}"


# ---------------------------------------------------------------- shared formatting

def input_name(analysis):
    return analysis["input"]["path"].rsplit("/", 1)[-1]


def media_line(analysis):
    """`1280x800 · 3.000 s · 180 fr · 60 fps CFR · audio aac 48 kHz`."""
    video = analysis["video"]
    size = f"{video['display_width']}x{video['display_height']}" + (f" (rotated {video['rotation']})" if video["rotation"] else "")
    rate = f"{video['avg_fps']:g} fps " + (f"VFR (frame dt {video['min_dt_ms']:g} to {video['max_dt_ms']:g} ms)" if video["is_vfr"] else "CFR")
    stream = analysis["audio_stream"]
    audio = f"audio {stream['codec']} {stream['sample_rate'] / 1000:g} kHz" if stream else "no audio"
    return f"{size} · {video['duration_s']:.3f} s · {video['frames']} fr · {rate} · {audio}"


def mode_text(analysis):
    reason = analysis["mode_reason"]
    return f"mode {analysis['mode']} ({reason if reason.startswith('--') else 'auto: ' + reason})"


def run_line(analysis):
    mode = mode_text(analysis)
    cache = analysis["cache"]
    state = "hit" if not cache["misses"] else "miss" if not cache["hits"] else "partial hit"
    return f"{mode} · cache {state} · {analysis['timing_s']['total']:.1f} s"


def level(value):
    """A loudness value; audio.py stores -inf and nan (silence) as None."""
    return "-inf" if value is None else f"{value:g}"


def audio_lines(analysis):
    audio, speech_section, sync = analysis["audio"], analysis["speech"], analysis["sync"]
    lines = []
    if audio:
        loud = audio["loudness"]
        kinds = [s["kind"] for s in audio["segments"]]
        counts = ", ".join(f"{kind} {kinds.count(kind)}" for kind in ("speech", "sound", "blip") if kind in kinds)
        lines.append(f"Audio: {level(loud['integrated_lufs'])} LUFS, LRA {level(loud['lra_lu'])} LU, true peak "
                     f"{level(loud['true_peak_dbfs'])} dBFS · activity {audio['activity_ratio'] * 100:.0f} %"
                     + (f" ({counts})" if counts else "") + f" · {len(audio['onsets'])} onsets")
    if speech_section:
        lines.append("Speech: " + speech_summary(speech_section))
    if sync:
        lines.append("Sync: " + sync_summary(sync))
    return lines


def speech_summary(section):
    if not section["files"]:
        return "no transcript (see warnings)"
    minutes = sum(s["end_s"] - s["start_s"] for s in section["segments"]) / 60
    source = " ".join(filter(None, (section["source"], section["locale"])))
    clamp = " · word starts clamped to voiced onsets" if section["word_start_clamped"] else ""
    return (f"{source} · {section['chars']:,} chars over {minutes:.1f} min"
            f" · {section['files']['txt']}, {section['files']['srt']}{clamp}")


def sync_summary(sync):
    if sync["status"] != "ok":
        return f"{sync['status']} ({sync['pairs']} paired onsets; needs 3)"
    low, high = sync["offset_ms_iqr"]
    return (f"{sync['offset_ms_median']:+.1f} ms ({sync['convention']}) · IQR {low:+.1f} to {high:+.1f} · "
            f"{sync['pairs']} pairs · xcorr {sync['xcorr_lag_ms']:+g} ms · frame {sync['frame_interval_ms']} ms")


def compact_number(value):
    """Compact number without a negative zero: -0.04 -> '0', 59.83 -> '59.8'."""
    return f"{round(value, 1) + 0.0:g}"


def text_label(text, limit=40):
    return '"' + (text if len(text) <= limit else text[:limit - 3] + "...") + '"'


def id_ranges(ids):
    """`M01, M05-M08`: consecutive ids joined into ranges."""
    numbers = sorted(int(i[1:]) for i in ids)
    prefix = next(iter(ids))[0] if ids else ""
    runs, width = [], len(next(iter(ids))) - 1 if ids else 2
    for number in numbers:
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    name = lambda n: f"{prefix}{n:0{width}d}"
    return ", ".join(name(a) if a == b else f"{name(a)}-{name(b)}" for a, b in runs)


def persistent_line(content):
    persistent = content["text"]["persistent"]
    if not persistent:
        return None
    return "Persistent text: " + " · ".join(f"{text_label(p['text'])} ({p['share'] * 100:.0f} %)" for p in persistent)


# ---------------------------------------------------------------- motion rows

def element_delays(event):
    return {element_id: group["delay_ms"] for group in event["timing_groups"] for element_id in group["elements"]}


def is_start_unknown(element):
    return not any(channel["amplitude_fixed"] for channel in element["channels"].values())


def curve_label(name, bezier):
    return name if name != "custom" else bezier_text(bezier)


def easing_cell(element):
    """Curve name (or bezier), near tie, overshoot and RMSE; `?` when the start state was never seen, `instant`
    when the change took <= 2 frames."""
    easing = element["easing"]
    if element["timing"]["instant"]:
        return INSTANT_CELL
    if easing["source"] == "measured-linear":
        label = "linear() (measured)"
    else:
        label = curve_label(easing["name"], easing["cubic_bezier"])
    ties = [tie["name"] for tie in easing["near_ties"]]
    label += f" ~ {', '.join(ties[:TIES_SHOWN])}{f' +{len(ties) - TIES_SHOWN}' if len(ties) > TIES_SHOWN else ''}" if ties else ""
    label += " overshoot" if easing["overshoot"] else ""
    text = f"{label} ({easing['fit_rmse']:.4f})"
    return f"? ({text})" if is_start_unknown(element) else text


def travel_cell(element):
    geometry = element["geometry"]
    dx, dy = geometry["translate_css_px"]
    at_least = " (at least)" if (element.get("clip") or {}).get("reading") == "at-edge" else ""
    parts = [f"{compact_number(dx)},{compact_number(dy)}{at_least}"] if abs(dx) >= 0.05 or abs(dy) >= 0.05 else []
    if abs(geometry["scale_from"] - 1) > 0.005 or abs(geometry["scale_to"] - 1) > 0.005:
        parts.append(f"scale {geometry['scale_from']:.2f}>{geometry['scale_to']:.2f}")
    if abs(geometry["rotate_deg"]) >= 0.1:
        parts.append(f"rot {geometry['rotate_deg']:.1f} deg")
    return " ".join(parts) or "-"


def duration_span(timing):
    """'low-high' of the duration range, or 'no range' for a change-energy proxy."""
    span = timing["duration_ms_range"]
    return f"{span[0]:.0f}-{span[1]:.0f}" if span else "no range"


def element_row(event, element, extra=()):
    timing = element["timing"]
    delay = element_delays(event).get(element["id"], 0.0)
    cells = [element["id"], "+".join(element["types"]) or "-", f"{timing['start_s']:.4f}", f"{delay:.0f}",
             f"{timing['duration_ms']:.0f} ({duration_span(timing)})", easing_cell(element), travel_cell(element),
             element["confidence"], *extra]
    return "| " + " | ".join(cells) + " |"


CHANNEL_TYPES = {"opacity": "fade"}


def split_rows(event, element, extra=()):
    """A split element is listed per channel with the channel's own timing and curve; the first row says why."""
    split, rows = element["split"], []
    owns = [c["own_fit"]["start_s"] for c in element["channels"].values() if "own_fit" in c]
    first_start = min(owns + ([event["timing_groups"][0]["start_s"]] if event["timing_groups"] else []))
    for number, (name, channel) in enumerate((n, c) for n, c in element["channels"].items() if "own_fit" in c):
        own = channel["own_fit"]
        why = (f" (split: one timing misses {split['channel']}, rmse {split['shared_rmse']:.3f} vs {split['own_rmse']:.3f})"
               if number == 0 else " (split)")
        label = f"{curve_label(own['name'], own['cubic_bezier'])} ({own['rmse']:.4f})"
        cells = [f"{element['id']} {name}", CHANNEL_TYPES.get(name, name) + why, f"{own['start_s']:.4f}",
                 f"{(own['start_s'] - first_start) * 1000:.0f}", f"{own['duration_ms']:.0f}",
                 label if channel["amplitude_fixed"] else f"? ({label})",
                 travel_cell(element) if name in ("translate", "scroll") else "-", element["confidence"],
                 *(extra if number == 0 else [""] * len(extra))]
        rows.append("| " + " | ".join(cells) + " |")
    return rows


def element_rows(event, element, extra=()):
    """One row per element under its one shared timing; a split element gets one row per channel."""
    return split_rows(event, element, extra) if element.get("split") else [element_row(event, element, extra)]


def motion_elements(motion):
    return [(event, element) for event in motion["events"] for element in event["elements"]]


def content_event_at(event, content):
    """The content visual event (any kind) this motion event overlaps, if any."""
    if not content:
        return None
    return next((v for v in content["events"]
                 if v["start_s"] <= event["last_change_s"] and event["first_change_s"] < v["end_s"]), None)


def is_instant_event(event):
    """A swap or cut: every fitted element changed within 2 frame intervals, or nothing was fitted on <= 2 frames."""
    if event["elements"]:
        return all(element["timing"]["instant"] for element in event["elements"])
    return event["features"]["changed_frames"] <= INSTANT_FRAMES


def is_scene_change(event, content):
    """Mode both: the motion event is a content cut or dissolve, or an instant change the content survey also saw
    (a slide swap). Its element fits are not UI motion."""
    visual = content_event_at(event, content)
    return bool(visual) and (visual["kind"] in SCENE_CHANGE_KINDS or is_instant_event(event))


def scene_change_ids(analysis):
    motion, content = analysis["motion"], analysis["content"]
    if not motion or not content:
        return set()
    return {event["id"] for event in motion["events"] if is_scene_change(event, content)}


def event_lines(event, content=None):
    """What the element table cannot say: overlap with a content event, unfitted events, stagger, scroll, stalls
    and notes."""
    name = event["id"]
    lines = []
    visual = content_event_at(event, content)
    if visual:
        what = ": a scene change, not UI motion; ignore its element fits" if is_scene_change(event, content) else ""
        lines.append(f"- {name} coincides with content event {visual['id']} ({visual['kind']} "
                     f"{visual['start_s']:.3f}-{visual['end_s']:.3f} s){what}")
    if not event["analysed"] or not event["elements"]:
        why = "; ".join(event["notes"]) or "not analysed"
        return lines + [f"- {name} {event['class']} {event['first_change_s']:.4f}-{event['last_change_s']:.4f} s: {why}"]
    lines += [f"- {name}: {note}" for note in event["notes"]]
    stagger = event["stagger"]
    if stagger:
        steps = ", ".join(f"{s:g}" for s in stagger["step_ms"])
        lines.append(f"- Stagger {name}: t50 step {stagger['step_ms_median']:g} ms ({steps}), {stagger['order']}")
    scroll = event["scroll"]
    if scroll:
        dx, dy = scroll["travel_px"]
        lines.append(f"- Scroll {name}: travel {compact_number(dx)},{compact_number(dy)} px, peak "
                     f"{compact_number(scroll['peak_speed_px_s'])} px/s, unreliable steps {scroll['unreliable_frac'] * 100:.0f} %, "
                     f"min response {scroll['response_min']:.2f}")
    for element in event["elements"]:
        stalls = element["timing"]["stalled_frames_s"]
        if stalls:
            lines.append(f"- {element['id']} stalled at " + ", ".join(f"{t:.4f}" for t in stalls) + " s")
        lines += [f"- {element['id']}: {note}" for note in element["notes"]
                  if not note.startswith(NEAR_TIE_NOTE) and "stalled frame" not in note]
    return lines


def motion_header(analysis):
    summary = analysis["motion"]["summary"]
    frame_ms = analysis["video"]["median_dt_ms"]
    micro = summary["micro_times_s"]
    micro_at = (" at " + ", ".join(f"{t:g}" for t in micro[:MICRO_TIMES_SHOWN]) + (" ..." if len(micro) > MICRO_TIMES_SHOWN else "")
                if micro else "")
    loop = summary["loop"]
    loop_text = f" · loop {loop['period_s']:g} s (cv {loop['cv']:g})" if loop else ""
    return (f"## Motion: {summary['motion_events']} motion events · {summary['subtle_events']} subtle · not fitted: "
            f"{summary['noise_events']} noise, {summary['micro_events']} micro{micro_at} · times s, ms · "
            f"±{frame_ms:.1f} ms/frame · dpr {analysis['params']['motion']['dpr']:g}{loop_text}")


def repeat_hidden_ids(repeats):
    """Events of a repeat group left out of the report table: all but the lead and the outliers, which the shared fit
    clearly misses and which keep their own fits."""
    return {event_id for repeat in repeats for event_id in repeat["events"]
            if event_id != repeat["lead"] and event_id not in repeat["outliers"]}


def bezier_text(bezier):
    return "cubic-bezier(" + ",".join(f"{v + 0.0:g}" for v in bezier) + ")"


def repeat_line(repeat, dpr):
    low, high = repeat["duration_ms_range"]
    curve = bezier_text(repeat["cubic_bezier"]) + ("" if repeat["easing"] == "custom" else f" = {repeat['easing']}")
    ties = [tie["name"] for tie in repeat["near_ties"]]
    curve += f" ~ {', '.join(ties[:TIES_SHOWN])}" if ties else ""
    starts = ", ".join(f"{t:.4f}" for t in repeat["starts_s"])
    outliers = (f" · outliers (own fits, in the table): {', '.join(repeat['outliers'])}" if repeat["outliers"]
                else " · no outliers")
    return (f"- Repeats {repeat['events'][0]}-{repeat['events'][-1]}: {repeat['count']} repeats of one transition every "
            f"{repeat['period_s']:g} s · one curve and duration fitted across {repeat['shared']} of them: "
            f"{repeat['duration_ms']:.0f} ms ({low:.0f}-{high:.0f}) {curve} (rmse {repeat['fit_rmse']:.4f}) · starts {starts} s · "
            f"travel ~{compact_number(repeat['travel_px_median'] / dpr)} css px{outliers} · table row: {repeat['lead']}")


def shown_curves(element):
    """(name, bezier) of the curves an element's rows show: its own fits when split, else its shared fit."""
    if element.get("split"):
        return [(c["own_fit"]["name"], c["own_fit"]["cubic_bezier"]) for c in element["channels"].values() if "own_fit" in c]
    easing = element["easing"]
    return [(easing["name"], easing["cubic_bezier"])] if easing["source"] == "named" else []


def curve_legend(pairs):
    """`Curves: md3-standard = cubic-bezier(0.2,0,0,1) · ...` for the named curves in the table."""
    named = {name: bezier for _, el in pairs if not el["timing"]["instant"] for name, bezier in shown_curves(el)
             if name != "custom"}
    return ["Curves: " + " · ".join(f"{name} = {bezier_text(bezier)}" for name, bezier in named.items())] if named else []


def css_part(motion):
    """The CSS snippets inline when they are short (a table the budget may cut, closing the code fence in its
    pointer), else a pointer to motion/css.txt."""
    snippets = [event["css"]["snippet"] for event in motion["events"] if event["css"]]
    if not snippets:
        return []
    text = "\n\n".join(snippets)
    if len(text) > CSS_INLINE_CHARS:
        return ["CSS: motion/css.txt (measured, fitted and suggested values are labelled)"]
    return Table(["CSS (motion/css.txt; measured, fitted and suggested values are labelled):", "```css"],
                 text.split("\n") + ["```"], lambda first, hidden: f"```\n(+{hidden} lines: Read OUT/motion/css.txt)")


def row_positions(motion):
    """Index of each element's first row in `vl.py rows --kind elements` (a split element has one row per channel)."""
    positions, index = {}, 0
    for event, element in motion_elements(motion):
        positions[element["id"]] = index
        index += len(element_rows(event, element))
    return positions


def motion_parts(analysis):
    motion, content = analysis["motion"], analysis["content"]
    by_id = {event["id"]: event for event in motion["events"]}
    scene = scene_change_ids(analysis)
    repeats = motion["summary"].get("repeats", [])
    hidden = repeat_hidden_ids(repeats)
    parts = [["", motion_header(analysis)]]
    dpr = analysis["params"]["motion"]["dpr"]
    parts.append([repeat_line(repeat, dpr) for repeat in repeats])
    positions = row_positions(motion)
    pairs = [(e, el) for e, el in motion_elements(motion) if e["id"] not in scene and e["id"] not in hidden]
    if pairs:
        listed = [(el["id"], row) for e, el in pairs for row in element_rows(e, el)]
        pointer = lambda first, count: rows_pointer("elements")(positions[listed[first][0]], count)
        parts.append(Table(list(ELEMENT_HEADER), [row for _, row in listed], pointer))
        parts.append(curve_legend(pairs))
    if hidden:
        first = min(positions[el["id"]] for e, el in motion_elements(motion) if e["id"] in hidden) + 1
        parts.append([f"+{len(hidden)} repeat rows ({id_ranges(hidden)}, under the shared fit): vl.py rows OUT --kind elements "
                      f"--from {first}"])
    if scene:
        changes = [f"{event_id} {by_id[event_id]['first_change_s']:.2f} s" for event_id in sorted(scene)]
        parts.append(["Scene changes (instant or a cut, not UI motion; element fits ignored): " + ", ".join(changes)])
    numbered = [(number, line) for number, event in enumerate(motion["events"], 1)
                if event["id"] not in scene and event["id"] not in hidden for line in event_lines(event, content)]
    if numbered:
        parts.append(Table([], [line for _, line in numbered], lambda first, hidden_lines:
                           f"+{hidden_lines} lines: vl.py rows OUT --kind motion --from {numbered[first][0]}"))
    parts.append(css_part(motion))
    return parts


# ---------------------------------------------------------------- content: timeline rows and blocks

@dataclass
class Row:
    t: float
    kind: str           # KEY, SAY, TEXT, EVENT, BLIP, SOUND, MOTION
    text: str
    lines: tuple = ()   # SAY: first and last transcript.txt line numbers
    quote: str = ""     # KEY and TEXT: the first new on-screen text, for the report's block index


def sentences(analysis):
    """(start, end, text, transcript.txt line number) per sentence, as speech.py wrote them."""
    section = analysis["speech"]
    if not section:
        return []
    return [(start, end, text, number) for number, (start, end, text) in enumerate(section.get("sentences") or [], 1)]


def normalized(text):
    return NON_WORD.sub("", text.casefold())


def shown_time(keyframe):
    """When the keyframe's screen state appeared (frame-accurate from the survey); its sample time for old OUTs."""
    return keyframe.get("shown_s", keyframe["t"])


def keyframe_rows(content):
    """KEY rows at the time each state appeared. A `same as` repeat shows only the texts its original lacks."""
    rows, shown_ids = [], set()
    texts = {event["id"]: event["text"] for event in content["text"]["events"]}
    by_id = {keyframe["id"]: keyframe for keyframe in content["keyframes"]}
    for keyframe in content["keyframes"]:
        label = f"{keyframe['id']} {'S' if keyframe['settled'] else 'm'}"
        original = by_id.get(keyframe["same_as"])
        known = {normalized(texts[i]) for i in original["text_ids"] if i in texts} if original else set()
        if original:
            label += f" same as {keyframe['same_as']}"
        new = [texts[i] for i in keyframe["text_ids"]
               if i in texts and i not in shown_ids and normalized(texts[i]) not in known]
        shown_ids.update(keyframe["text_ids"])
        if new:
            label += " · TEXT+ " + " · ".join(text_label(text) for text in new)
        rows.append(Row(shown_time(keyframe), "KEY", label, quote=text_label(new[0]) if new else ""))
    return rows, shown_ids


def leads_to_keyframe(event, keyframes, cell_s):
    """An activity event whose change is the one a keyframe's state begins with (a slide swap on 3 % of the frame)."""
    return any(event["start_s"] <= shown_time(k) <= event["end_s"] + cell_s for k in keyframes)


def is_explained_motion(event, content):
    """A motion event a content row already covers: a scene change, or overlap with an EVENT row's kind."""
    visual = content_event_at(event, content)
    return bool(visual) and (visual["kind"] != "activity" or is_instant_event(event))


def other_rows(analysis, shown_text_ids):
    """Text events no keyframe showed, visual events, audio blips and sounds, motion events."""
    content, audio, motion = analysis["content"], analysis["audio"], analysis["motion"]
    cell_s = analysis["params"]["content"]["cell_s"]
    rows = [Row(e["first_s"], "TEXT", f"{text_label(e['text'])} (conf {e['conf']:.2f})", quote=text_label(e["text"]))
            for e in content["text"]["events"] if e["id"] not in shown_text_ids]
    rows += [Row(e["start_s"], "EVENT", f"{e['kind']} ({e['e2e_frac']:.2f})") for e in content["events"]
             if e["kind"] in TIMELINE_EVENT_KINDS or leads_to_keyframe(e, content["keyframes"], cell_s)]
    for segment in (audio or {}).get("segments", []):
        length = segment["end_s"] - segment["start_s"]
        if segment["kind"] == "blip":
            rows.append(Row(segment["start_s"], "BLIP", f"{length * 1000:.0f} ms"))
        elif segment["kind"] == "sound":
            rows.append(Row(segment["start_s"], "SOUND", f"{length:.1f} s"))
    for event in (motion or {}).get("events", []):
        if event["class"] == "motion" and event["elements"] and not is_explained_motion(event, content):
            first = event["elements"][0]
            what = INSTANT_CELL if first["timing"]["instant"] else f"{first['easing']['name']} {first['timing']['duration_ms']:.0f} ms"
            rows.append(Row(event["first_change_s"], "MOTION", f"{event['id']} {len(event['elements'])} el · {what}"))
    return rows


def say_rows(spoken, stops):
    """Consecutive sentences become one SAY row until the screen changes (a KEY or TEXT row) or >= 2 s of silence."""
    rows, current = [], []

    def close():
        if current:
            rows.append(Row(current[0][0], "SAY", " ".join(s[2] for s in current), (current[0][3], current[-1][3])))
            current.clear()

    stop_times = sorted(stops)
    for sentence in spoken:
        has_stop = current and bisect.bisect_right(stop_times, sentence[0]) > bisect.bisect_right(stop_times, current[-1][0])
        if current and (sentence[0] - current[-1][1] >= SPEECH_GAP_S or has_stop):
            close()
        current.append(sentence)
    close()
    return rows


def speech_before(t, spoken):
    """End of the last sentence that starts before t, or None (spoken is in time order)."""
    before = bisect.bisect_left([s[0] for s in spoken], t)
    return spoken[before - 1][1] if before else None


def is_after_silence(t, spoken):
    """No speech in the 2 s before t (sentences do not overlap)."""
    previous_end = speech_before(t, spoken)
    return previous_end is None or t - previous_end >= SPEECH_GAP_S


def block_starts(analysis, spoken):
    """Shot starts, keyframes that change >= 0.30, and keyframes or speech that follow >= 2 s without speech,
    merged until each block spans >= 20 s (60 s for videos over 10 min). A block opened by speech starts at the
    first keyframe shown in the pause before it, so a slide and what is said about it share a block."""
    content, rng = analysis["content"], analysis["range"]
    key_times = sorted(shown_time(k) for k in content["keyframes"])
    speech_starts = {s[0] for s in spoken}
    candidates = {s["start_s"] for s in content["shots"]}
    candidates |= {shown_time(k) for k in content["keyframes"] if k["change_frac"] >= BLOCK_BREAK_CHANGE}
    candidates |= {t for t in key_times + sorted(speech_starts) if is_after_silence(t, spoken)}
    min_span = LONG_BLOCK_MIN_S if rng["end_s"] - rng["start_s"] > LONG_VIDEO_S else BLOCK_MIN_S
    starts = [rng["start_s"]]
    for t in sorted(candidates):
        if t - starts[-1] < min_span:
            continue
        if t in speech_starts:
            pause_start = speech_before(t, spoken)
            pause_start = starts[-1] if pause_start is None else max(pause_start, starts[-1])
            t = next((k for k in key_times if pause_start < k <= t), t)
        starts.append(t)
    return starts


@dataclass
class Block:
    start_s: float
    end_s: float
    rows: list


def timeline_blocks(analysis):
    content = analysis["content"]
    spoken = sentences(analysis)
    keys, shown_text_ids = keyframe_rows(content)
    others = other_rows(analysis, shown_text_ids)
    rows = keys + others + say_rows(spoken, [r.t for r in keys + others if r.kind in ("KEY", "TEXT")])
    starts = block_starts(analysis, spoken)
    ends = starts[1:] + [analysis["range"]["end_s"]]
    ordered = sorted(rows, key=lambda r: (r.t, r.kind != "KEY"))
    return [Block(a, b, [r for r in ordered if a <= r.t < b or (b == ends[-1] and r.t >= b)]) for a, b in zip(starts, ends)]


def block_title(block, content):
    keys = [k for k in content["keyframes"] if block.start_s <= shown_time(k) < block.end_s]
    shots = sorted({k["shot"] for k in keys})
    sheets = sorted({k["sheet"] for k in keys if k["sheet"]})
    parts = []
    if shots:
        parts.append(f"shot {shots[0][1:].lstrip('0')}" if len(shots) == 1 else
                     f"shots {shots[0][1:].lstrip('0')}-{shots[-1][1:].lstrip('0')}")
    if keys:
        parts.append(keys[0]["id"] if len(keys) == 1 else f"{keys[0]['id']}-{keys[-1]['id']}")
    parts += [", ".join(sheets)] if sheets else []
    return f"[{clock(block.start_s)}-{clock(block.end_s)}] " + " · ".join(parts)


def row_text(row, is_condensed):
    if row.kind == "KEY":
        return row.text
    if row.kind == "SAY" and is_condensed and row.lines[0] != row.lines[1]:
        first = re.split(r"(?<=[.!?。])\s", row.text, maxsplit=1)[0]
        return f"SAY {first} (transcript.txt L{row.lines[0]}-L{row.lines[1]})"
    return f"{row.kind} {row.text}"


def block_lines(block, content, is_condensed):
    """One line per row; minor rows (EVENT, BLIP, SOUND) within 1 s of each other share a line."""
    lines, minor, minor_t = [f"## {block_title(block, content)}"], [], None

    def flush():
        if minor:
            lines.append("- " + " · ".join(minor))
            minor.clear()

    for row in block.rows:
        entry = f"{clock(row.t)} {row_text(row, is_condensed)}"
        if row.kind not in MINOR_ROW_KINDS:
            flush()
            lines.append(f"- {entry}")
            continue
        if minor and row.t - minor_t >= MINOR_JOIN_S:
            flush()
        minor_t = row.t if not minor else minor_t
        minor.append(entry)
    flush()
    return lines


def timeline_header(analysis):
    content = analysis["content"]
    lines = [f"# video-lens timeline · {input_name(analysis)}", media_line(analysis), mode_text(analysis)]
    lines += audio_lines(analysis)
    persistent = persistent_line(content)
    lines += [persistent] if persistent else []
    lines.append("Rows: KEY S = settled, m = moving, at the time its screen state appeared · TEXT+ = on-screen text "
                 "new at that keyframe · SAY = speech (transcript.txt) · EVENT/BLIP/SOUND/MOTION = visual change, short "
                 "sound, longer sound, UI motion.")
    lines.append("Limits: times are seconds on the ffmpeg timeline; OCR text not new at a keyframe is first seen within "
                 f"one cell ({analysis['params']['content']['cell_s']:g} s) of appearing; speech word starts are clamped "
                 "to the voiced onset.")
    return lines


def render_timeline(analysis, blocks, budget):
    """Full rows; above --timeline-chars SAY rows shrink to their first sentence, then trailing blocks are cut."""
    content = analysis["content"]
    header = timeline_header(analysis)
    for is_condensed in (False, True):
        bodies = [block_lines(block, content, is_condensed) for block in blocks]
        text = "\n".join(header + [line for body in bodies for line in [""] + body]) + "\n"
        if len(text) <= budget:
            return text
    kept = list(header)
    for number, body in enumerate(bodies):
        pointer = (f"\n(+{len(bodies) - number} blocks from {clock(blocks[number].start_s)} cut to fit "
                   f"--timeline-chars: vl.py text OUT --from {blocks[number].start_s:g})\n")
        candidate = "\n".join(kept + [""] + body) + "\n"
        if len(candidate) + len(pointer) > budget:
            return ("\n".join(kept) + pointer)[:budget]
        kept += [""] + body
    return "\n".join(kept) + "\n"


def block_index_row(block):
    keys = [r.text.split()[0] for r in block.rows if r.kind == "KEY"]
    parts = []
    if keys:
        parts.append(keys[0] if len(keys) == 1 else f"{keys[0]}-{keys[-1]}")
    first_text = next((r.quote for r in block.rows if r.quote), None)
    if first_text:
        parts.append(first_text)
    said = " ".join(r.text for r in block.rows if r.kind == "SAY")
    if said:
        parts.append(said[:SPEECH_PREVIEW_CHARS] + ("..." if len(said) > SPEECH_PREVIEW_CHARS else ""))
    return f"- [{clock(block.start_s, 0)}-{clock(block.end_s, 0)}] " + " · ".join(parts)


def content_parts(analysis, blocks, timeline_chars):
    content = analysis["content"]
    keyframes = content["keyframes"]
    repeats = sum(1 for k in keyframes if k["same_as"])
    kinds = [e["kind"] for e in content["events"]]
    event_counts = ", ".join(f"{kind} {kinds.count(kind)}" for kind in ("cut", "transition", "transient", "update") if kind in kinds)
    ocr = content["ocr"]
    ocr_text = f"OCR {ocr['samples']} samples at {ocr['frame_width']} px" if ocr else "OCR off"
    shots = len(content["shots"])
    head = [f"## Content: {shots} shot{'s' if shots != 1 else ''} · {len(keyframes)} keyframes"
            + (f" ({repeats} same as an earlier one)" if repeats else "") + (f" · events {event_counts}" if event_counts else "")
            + f" · {len(content['text']['events'])} text events · {ocr_text}"]
    persistent = persistent_line(content)
    head += [persistent] if persistent else []
    head.append(f"Blocks (timeline.md, {timeline_chars:,} chars; keyframe ids, first new on-screen text, speech):")
    rows = [block_index_row(block) for block in blocks]
    return [head, Table([], rows, lambda first, hidden: f"+{hidden} blocks from [{clock(blocks[first].start_s, 0)}]: "
                                                         f"Read timeline.md or vl.py text OUT --from {blocks[first].start_s:g}")]


# ---------------------------------------------------------------- views, warnings, next steps

def fitted_events(motion):
    return [event for event in motion["events"] if event["analysed"] and event["elements"]]


def motion_sheet_row(analysis, event_sheets):
    """One line for the motion sheets: which events have one, and how to see the others."""
    costs = sorted(v["visual_tokens"] for v in event_sheets)
    cost = f"{costs[0]:,}" if costs[0] == costs[-1] else f"{costs[0]:,}-{costs[-1]:,}"
    covered = [v["covers"] for v in event_sheets]
    fitted = [e["id"] for e in fitted_events(analysis["motion"]) if e["id"] not in scene_change_ids(analysis)]
    if len(covered) >= len(fitted):
        return f"- L2 motion sheets {id_ranges(covered)} (motion/Mxx_sheet.png) · {cost} tok each"
    return (f"- L2 motion sheets for {len(covered)} of {len(fitted)} events (largest): {id_ranges(covered)} "
            f"(motion/Mxx_sheet.png) · {cost} tok each · others: vl.py zoom OUT --event Mxx")


def overview_row(analysis, view):
    """Overview line; a motion overview also says which events it leaves out."""
    covers = view["covers"].split(",")
    detail = f"{view['cells']} cells · " if view["cells"] else ""
    motion = analysis["motion"]
    if motion and covers[0].startswith("M"):
        candidates = [e["id"] for e in motion["events"] if e["class"] == "motion" and e["id"] not in scene_change_ids(analysis)]
        left_out = [event_id for event_id in candidates if event_id not in covers]
        if left_out:
            detail = f"{len(candidates) - len(left_out)} of {len(candidates)} events ({id_ranges(left_out)} left out) · " + detail
    return f"- {view['level']} {view['file']} · {detail}{view['visual_tokens']:,} tok"


def view_rows(analysis):
    """One line per view; motion sheets share one line. kf sheets show the time span they cover."""
    keyframe_times = {k["id"]: k["t"] for k in (analysis["content"] or {}).get("keyframes", [])}
    scene = scene_change_ids(analysis)
    rows, event_sheets = [], []
    for view in sorted(analysis["views"], key=lambda v: (v["level"], v["file"])):
        if view["kind"] == "event" and view["level"] == "L2":
            event_sheets += [] if view["covers"] in scene else [view]
            continue
        if view["kind"] == "overview":
            rows.append(overview_row(analysis, view))
            continue
        detail = f"{view['cells']} cells · " if view["cells"] else ""
        first, _, last = view["covers"].partition("-")
        if view["kind"] == "keyframes" and first in keyframe_times and last in keyframe_times:
            detail = f"{view['covers']} {clock(keyframe_times[first], 0)}-{clock(keyframe_times[last], 0)} · "
        rows.append(f"- {view['level']} {view['file']} · {detail}{view['visual_tokens']:,} tok")
    if event_sheets:
        rows.insert(sum(r.startswith("- L1") for r in rows), motion_sheet_row(analysis, event_sheets))
    return rows


def zoom_event_id(analysis):
    """The event the zoom hint and Next name: the first low-confidence UI motion event, else the first fitted one."""
    events = [e for e in fitted_events(analysis["motion"]) if e["id"] not in scene_change_ids(analysis)]
    low = [e for e in events if any(el["confidence"] == "low" for el in e["elements"])]
    return (low or events or [None])[0]


def zoom_hint(run, event):
    grid = sheets_motion.zoom_tokens(run, event)
    crops = sheets_motion.crop_boxes(run, event)
    crop_tokens = sum(len(sheets_motion.CROP_POINTS) * views.visual_tokens(*fitted_size(box)) for _, box in crops)
    crop_text = (f" + {len(crops) * len(sheets_motion.CROP_POINTS)} optional native crops ≈{crop_tokens:,} tok"
                 if crops else "")
    return f"- L2 vl.py zoom OUT --event {event['id']} · 12 cells ≈{grid:,} tok (Read this){crop_text}"


def fitted_size(box):
    """(w, h) of a native crop after views.fit_to_limits."""
    _, _, w, h = box
    scale = min(1.0, views.MAX_SIDE_PX / max(w, h))
    return max(1, math.floor(w * scale)), max(1, math.floor(h * scale))


def view_hints(run):
    """Commands for the next level down: a zoom and one exact frame, each with its token cost."""
    analysis = run.analysis
    hints = []
    motion, content = analysis["motion"], analysis["content"]
    event = zoom_event_id(analysis) if motion else None
    if event:
        element = event["elements"][0]
        x, y, w, h = (int(round(v)) for v in element["bbox_src"])
        hints.append(zoom_hint(run, event))
        hints.append(FRAME_VIEW_LINE.format(t=f"{element['timing']['t50_s']:.4f}", roi=f"--roi {x},{y},{w},{h} "))
    if content and content["keyframes"]:
        width, height = run.display_size
        tokens = views.grid_tokens((height, width), sheets_motion.ZOOM_CELLS, ZOOM_COLUMNS, views.ZOOM_CELL_W)
        hints.append(f"- L2 vl.py zoom OUT --segment S01 (or --range A:B) · 12 cells ≈{tokens:,} tok")
        hints.append(FRAME_VIEW_LINE.format(t=f"{content['keyframes'][0]['t']:g}", roi=""))
    return hints


def text_roi(analysis, event):
    """A text event's box (normalised to the display frame) as display px x,y,w,h."""
    width, height = analysis["video"]["display_width"], analysis["video"]["display_height"]
    bx, by, bw, bh = event["box"]
    return [int(bx * width), int(by * height), max(1, math.ceil(bw * width)), max(1, math.ceil(bh * height))]


def speech_share(analysis):
    section, rng = analysis["speech"], analysis["range"]
    if not section:
        return 0.0
    spoken = sum(s["end_s"] - s["start_s"] for s in section["segments"])
    return spoken / max(1e-9, rng["end_s"] - rng["start_s"])


def suggested_next(run):
    analysis = run.analysis
    suggestions = []
    motion, content = analysis["motion"], analysis["content"]
    scene = scene_change_ids(analysis)
    if motion:
        low = [e["id"] for e in fitted_events(motion)
               if e["id"] not in scene and any(el["confidence"] == "low" for el in e["elements"])]
        suggestions += [f"vl.py zoom OUT --event {event_id}" for event_id in low[:2]]
        sheet = next((e["evidence"]["sheet"] for e in motion["events"] if e["evidence"]["sheet"] and e["id"] not in scene), None)
        if sheet:
            suggestions.append(f"Read OUT/{sheet}")
        if not motion["summary"]["motion_events"] and not motion["summary"]["subtle_events"]:
            suggestions.append("no motion found: rerun analyze with --roi x,y,w,h or --pix-threshold 5")
    if content:
        timeline = f"Read OUT/{TIMELINE_FILE}"
        if speech_share(analysis) > TIMELINE_FIRST_SHARE:
            suggestions.insert(0, timeline)
        else:
            suggestions.append(timeline)
        unsure = sorted((e for e in content["text"]["events"] if e["conf"] < LOW_OCR_CONF), key=lambda e: e["conf"])
        if unsure:
            x, y, w, h = text_roi(analysis, unsure[0])
            suggestions.append(f"vl.py frame OUT --t {unsure[0]['first_s']:g} --roi {x},{y},{w},{h}")
        if analysis["speech"] or content["text"]["events"]:
            suggestions.append('vl.py text OUT --grep "REGEX"')
    return suggestions[:SUGGESTIONS_MAX]


# ---------------------------------------------------------------- report.md

def render_report(run):
    """report.md text within --budget-chars; writes timeline.md in content modes and fills suggested_next."""
    analysis = run.analysis
    run.record_cache()
    parts = [[f"# video-lens · {input_name(analysis)}", media_line(analysis), run_line(analysis), f"OUT = {run.out}"]]
    if analysis["input"]["url"]:
        parts[0].append(f"URL: {analysis['input']['url']}")
    if not analysis["range"]["is_full"]:
        parts[0].append(f"range {analysis['range']['start_s']:g}-{analysis['range']['end_s']:g} s")
    parts[0] += audio_lines(analysis)
    if analysis["content"]:
        blocks = timeline_blocks(analysis)
        timeline = render_timeline(analysis, blocks, analysis["params"]["content"]["timeline_chars"])
        write_atomic(run.out / TIMELINE_FILE, timeline.encode())
        parts += content_parts(analysis, blocks, len(timeline))
    else:
        (run.out / TIMELINE_FILE).unlink(missing_ok=True)     # a stale one from a content run into this OUT
    if analysis["motion"]:
        parts += motion_parts(analysis)
    parts.append(["", "## Views (read only what the question needs)"])
    parts.append(Table([], view_rows(analysis) + view_hints(run),
                       lambda first, hidden: f"+{hidden} views: listed in OUT/analysis.json views"))
    analysis["suggested_next"] = suggested_next(run)
    if analysis["suggested_next"]:
        parts.append(["Next: " + " · ".join(analysis["suggested_next"])])
    if run.warnings:
        parts.append(Table(["Warnings:"], [f"- {w}" for w in run.warnings],
                           lambda first, hidden: f"+{hidden} warnings: OUT/analysis.json warnings"))
    return fit_budget(parts, analysis["params"]["report"]["budget_chars"])


# ---------------------------------------------------------------- vl.py text

def text_rows(analysis, kind):
    """(start, end, line) for SAY sentences, OCR text events (on screen from first_s until the sample that no
    longer saw them) and persistent text, in time order."""
    rows = []
    if kind in ("say", "all"):
        rows += [(start, end, f"{clock(start)}-{clock(end)} SAY {text}") for start, end, text, _ in sentences(analysis)]
    content = analysis["content"]
    if kind in ("ocr", "all") and content:
        keyframe_of = {i: k["id"] for k in content["keyframes"] for i in k["text_ids"]}
        for event in content["text"]["events"]:
            end = event.get("end_s", event["last_s"])
            shown = f", {keyframe_of[event['id']]}" if event["id"] in keyframe_of else ""
            rows.append((event["first_s"], end, f"{clock(event['first_s'])}-{clock(end)} TEXT "
                                                f"{text_label(event['text'], 200)} (conf {event['conf']:.2f}{shown})"))
        rng = analysis["range"]
        for item in content["text"]["persistent"]:
            first, last = item.get("first_s", rng["start_s"]), item.get("last_s", rng["end_s"])
            rows.append((first, last, f"{clock(first)}-{clock(last)} PERSISTENT {text_label(item['text'], 200)} "
                                      f"(in {item['share'] * 100:.0f} % of OCR samples: logo, watermark or a held title)"))
    return sorted(rows, key=lambda row: row[0])


def overlaps(row, start_s, end_s):
    """The row's span [start, end] meets [start_s, end_s); a zero-length row counts at its start."""
    first, last = row[0], max(row[1], row[0] + 1e-6)
    return (start_s is None or last > start_s) and (end_s is None or first < end_s)


def text_view(run, start_s, end_s, grep, kind, max_lines):
    """`vl.py text`: speech and on-screen text on screen or said during [start, end), optionally filtered by a
    regex. Each line is `start-end KIND text`."""
    try:
        pattern = re.compile(grep, re.IGNORECASE) if grep else None
    except re.error as error:
        raise VlError(EXIT_BAD_ARGS, f"--grep is not a valid regex ({error})", "Escape special characters") from None
    analysis = run.analysis
    if not analysis["speech"] and not analysis["content"]:
        return f"no transcript and no on-screen text in this analysis (mode {analysis['mode']}); rerun analyze with --mode content\n"
    rows = [row for row in text_rows(analysis, kind)
            if overlaps(row, start_s, end_s) and (pattern is None or pattern.search(row[2]))]
    if not rows:
        return "no matching lines\n"
    lines = [line for _, _, line in rows[:max_lines]]
    if len(rows) > max_lines:
        flags = (f" --to {end_s:g}" if end_s is not None else "") + (f' --grep "{grep}"' if grep else "") + (
            f" --kind {kind}" if kind != "all" else "")
        lines.append(f"+{len(rows) - max_lines} lines: vl.py text OUT --from {rows[max_lines][0]:g}{flags}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- vl.py rows

def motion_event_rows(analysis):
    header = ["| id | class | first change | last change | mode | elements | notes |", "|---|---|---|---|---|---|---|"]
    rows = []
    for event in analysis["motion"]["events"]:
        notes = [line[2:] for line in event_lines(event, analysis["content"])]
        rows.append(f"| {event['id']} | {event['class']} | {event['first_change_s']:.4f} | {event['last_change_s']:.4f} | "
                    f"{event['mode']} | {len(event['elements'])} | {'; '.join(notes) or '-'} |")
    return header, rows


def element_table_rows(analysis):
    header = [ELEMENT_HEADER[0][:-1] + "| bbox src | notes |", ELEMENT_HEADER[1] + "---|---|"]
    rows = []
    for event, element in motion_elements(analysis["motion"]):
        box = ",".join(f"{v:g}" for v in element["bbox_src"])
        rows += element_rows(event, element, (box, "; ".join(element["notes"]) or "-"))
    return header, rows


def content_event_rows(analysis):
    header = ["| id | kind | start | end | duration ms | e2e | peak |", "|---|---|---|---|---|---|---|"]
    return header, [f"| {e['id']} | {e['kind']} | {e['start_s']:.3f} | {e['end_s']:.3f} | {e['duration_ms']} | "
                    f"{e['e2e_frac']:.3f} | {e['peak_frac']:.3f} |" for e in analysis["content"]["events"]]


def keyframe_table_rows(analysis):
    header = ["| id | t | shown from | clock | shot | settled | hold s | change | reason | same as | sheet cell | text ids |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    return header, [f"| {k['id']} | {k['t']:.3f} | {shown_time(k):.3f} | {clock(k['t'])} | {k['shot']} | "
                    f"{'yes' if k['settled'] else 'no'} | {k['hold_s']:g} | {k['change_frac']:.4f} | {k['reason']} | "
                    f"{k['same_as'] or '-'} | {k['sheet'] or '-'} {'' if k['cell'] is None else k['cell']} | "
                    f"{','.join(k['text_ids']) or '-'} |" for k in analysis["content"]["keyframes"]]


def text_table_rows(analysis):
    header = ["| id | first | last seen | gone by | text | conf | samples | box | px h | legible in sheet |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    return header, [f"| {e['id']} | {e['first_s']:.3f} | {e['last_s']:.3f} | {e.get('end_s', e['last_s']):.3f} | "
                    f"{text_label(e['text'], 200)} | {e['conf']:.2f} | {e['samples']} | "
                    f"{','.join(f'{v:.3f}' for v in e['box'])} | {e['px_h']} | {'yes' if e['legible_in_sheet'] else 'no'} |"
                    for e in analysis["content"]["text"]["events"]]


def shot_table_rows(analysis):
    header = ["| id | start | end | keyframes |", "|---|---|---|---|"]
    return header, [f"| {s['id']} | {s['start_s']:.3f} | {s['end_s']:.3f} | {','.join(s['keyframes'])} |"
                    for s in analysis["content"]["shots"]]


ROW_TABLES = {"motion": ("motion", motion_event_rows), "elements": ("motion", element_table_rows),
              "events": ("content", content_event_rows), "keyframes": ("content", keyframe_table_rows),
              "text": ("content", text_table_rows), "shots": ("content", shot_table_rows)}


def rows_view(run, kind, start, count):
    """`vl.py rows`: rows start..start+count-1 (1-based) of one analysis table, with a pointer to the rest."""
    section, build = ROW_TABLES[kind]
    if not run.analysis[section]:
        raise VlError(EXIT_BAD_ARGS, f"this OUT has no {section} analysis, so it has no {kind} rows",
                      f"Rerun analyze with --mode {section} or both")
    header, rows = build(run.analysis)
    if start > max(1, len(rows)):
        raise VlError(EXIT_BAD_ARGS, f"--from {start} is past the last {kind} row ({len(rows)})", "Pick a smaller --from")
    shown = rows[start - 1:start - 1 + count]
    rest = len(rows) - (start - 1 + len(shown))
    lines = header + shown + ([rows_pointer(kind)(start - 1 + len(shown), rest)] if rest else [])
    return "\n".join(lines) + "\n"
