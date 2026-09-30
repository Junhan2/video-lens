"""CSS snippets (spec 7.8.7): css px = video px / --dpr; one snippet per event, written to OUT/motion/css.txt.

A staggered event (>= 3 timing groups with one shared animation) becomes one @keyframes with
`animation-delay: calc(var(--i) * STEP)`; events that repeat one transition (motion.summary.repeats) share one
rule with the median duration and the most common easing; anything else becomes one `transition` per timing
group with its from/to states in comments. Instant changes (swaps, cuts) get no rule. Every number is labelled
measured, fitted or suggested.
"""
from .easing import css_number

CSS_FILE = "motion/css.txt"
RETINA_MIN_WIDTH = 2000
DPR_WARNING = "dpr assumed 1: pass --dpr 2 for Retina captures before using px values in CSS"
START_UNKNOWN_COMMENT = "/* start state not observed: easing, duration and travel unreliable */"
AT_EDGE_COMMENT = "/* {state} state assumed just beyond the {edge} edge (the least travel the hidden frames allow) */"
CHANNEL_PROPERTIES = {"translate": "transform", "scale": "transform", "rotate": "transform", "opacity": "opacity",
                      "color": "background-color"}


def px(value, dpr):
    return f"{css_number(value / dpr, 1)}px"


def timing_function(element):
    """cubic-bezier of the fit, or the measured linear() when the motion overshoots and no bezier fits it well."""
    easing = element["easing"]
    if easing["source"] == "measured-linear":
        return easing["css_linear"], element["timing"]["duration_ms"], "measured linear()"
    x1, y1, x2, y2 = easing["cubic_bezier"]
    label = easing["name"] if easing["source"] == "named" else "custom"
    return f"cubic-bezier({css_number(x1)}, {css_number(y1)}, {css_number(x2)}, {css_number(y2)})", element["timing"]["duration_ms"], f"fitted {label}"


def offset_transform(element, dpr, sign):
    """The transform that holds the other rest state, relative to this element's reference rest frame."""
    geometry = element["geometry"]
    parts = []
    dx, dy = geometry["translate_px"]
    if "translate" in element["types"] and (dx or dy):
        parts.append(f"translate({px(sign * dx, dpr)}, {px(sign * dy, dpr)})")
    if "scale" in element["types"]:
        ratio = geometry["scale_from"] if sign < 0 else geometry["scale_to"]
        parts.append(f"scale({css_number(ratio)})")
    if "rotate" in element["types"]:
        parts.append(f"rotate({css_number(sign * geometry['rotate_deg'], 2)}deg)")
    return " ".join(parts)


def states(element, dpr):
    """(from declarations, to declarations) of one element; an entrance ends at its layout position."""
    is_exit = element["origin"] == "start"
    offset = offset_transform(element, dpr, 1 if is_exit else -1)
    before, after = [], []
    if offset:
        (after if is_exit else before).append(f"transform: {offset}")
        (before if is_exit else after).append("transform: none")
    if "fade" in element["types"]:
        before.append(f"opacity: {1 if is_exit else 0}")
        after.append(f"opacity: {0 if is_exit else 1}")
    colour = element["channels"].get("color", {})
    if "from_hex" in colour:
        before.append(f"background-color: {colour['from_hex']}")
        after.append(f"background-color: {colour['to_hex']}")
    return before, after


def properties(element):
    names = []
    if any(t in element["types"] for t in ("translate", "scale", "rotate")):
        names.append("transform")
    if "fade" in element["types"]:
        names.append("opacity")
    if "color" in element["types"]:
        names.append("background-color")
    return names


def css_class(element_id):
    return element_id.lower().replace(".", "-")


def is_uniform_stagger(event, groups, by_id):
    """One animation repeated with a delay: same types and easing name in every group, channels not split. `groups`
    are the timing groups led by a fitted element (an instant one has no rule)."""
    if not event["stagger"] or len(groups) < 2:
        return False
    firsts = [by_id[group["elements"][0]] for group in groups]
    if any(e.get("split") for e in firsts):
        return False
    return len({(tuple(e["types"]), e["easing"]["name"], e["origin"]) for e in firsts}) == 1


def stagger_snippet(event, groups, by_id, dpr):
    first = by_id[groups[0]["elements"][0]]
    function, duration, easing_label = timing_function(first)
    before, after = states(first, dpr)
    name = f"{event['id'].lower()}-item"
    step = event["stagger"]["step_ms_median"]
    return "\n".join([
        f"/* {event['id']}: {len(groups)} groups, order {event['stagger']['order']}, "
        f"t50 step {step} ms (measured), values from {first['id']} (fitted) */",
        f"@keyframes {name} {{",
        f"  from {{ {'; '.join(before)}; }}",
        f"  to {{ {'; '.join(after)}; }}",
        "}",
        f".{name} {{",
        f"  animation: {name} {css_number(duration, 0)}ms {function} both;   /* duration fitted, easing {easing_label} */",
        f"  animation-delay: calc(var(--i) * {css_number(step, 0)}ms);   /* suggested from the measured step */",
        "}",
    ])


def is_start_known(element):
    return any(channel["amplitude_fixed"] for channel in element["channels"].values())


def split_transition(element, group):
    """One transition per property, each with its channel's own fit (the first geometric channel sets transform)."""
    parts = {}
    for name, channel in element["channels"].items():
        own = channel.get("own_fit")
        prop = CHANNEL_PROPERTIES.get(name)
        if own is None or prop is None or prop in parts:
            continue
        x1, y1, x2, y2 = own["cubic_bezier"]
        delay = group["delay_ms"] + (own["start_s"] - element["timing"]["start_s"]) * 1000
        parts[prop] = (f"{prop} {css_number(own['duration_ms'], 0)}ms cubic-bezier({css_number(x1)}, {css_number(y1)}, "
                       f"{css_number(x2)}, {css_number(y2)}) {css_number(max(delay, 0.0), 0)}ms")
    return ", ".join(parts.values())


def start_comment(element):
    clip = element.get("clip")
    if clip and clip["reading"] == "at-edge":
        return [AT_EDGE_COMMENT.format(state="start" if element["origin"] == "end" else "end", edge=clip["edge"])]
    return [] if is_start_known(element) else [START_UNKNOWN_COMMENT]


def group_snippet(event, group, by_id, dpr):
    lead = by_id[group["elements"][0]]
    function, duration, easing_label = timing_function(lead)
    names = properties(lead) or ["all"]
    transition = ", ".join(f"{name} {css_number(duration, 0)}ms {function} {css_number(group['delay_ms'], 0)}ms" for name in names)
    if lead.get("split"):
        transition, easing_label = split_transition(lead, group), "each channel's own fit (channels split)"
    before, after = states(lead, dpr)
    lines = start_comment(lead)
    lines.append(f"/* {event['id']} group: {', '.join(group['elements'])} · {'+'.join(lead['types'])} · start "
                 f"{lead['timing']['start_s']:.4f} s (fitted), delay {css_number(group['delay_ms'], 1)} ms (fitted) */")
    if before or after:
        lines.append(f"/* from {{ {'; '.join(before)} }} to {{ {'; '.join(after)} }} (fitted amplitude) */")
    fitted = "durations fitted" if lead.get("split") else "duration fitted"
    lines.append(f".{css_class(lead['id'])} {{ transition: {transition}; }}   /* {fitted}, easing {easing_label} */")
    return "\n".join(lines)


def repeat_snippet(repeat, lead, dpr):
    """One rule for events that repeat one transition: the curve and duration fitted across the repeats at once."""
    x1, y1, x2, y2 = repeat["cubic_bezier"]
    function = f"cubic-bezier({css_number(x1)}, {css_number(y1)}, {css_number(x2)}, {css_number(y2)})"
    duration = css_number(repeat["duration_ms"], 0)
    low, high = (css_number(v, 0) for v in repeat["duration_ms_range"])
    transition = ", ".join(f"{name} {duration}ms {function}" for name in properties(lead) or ["all"])
    before, after = states(lead, dpr)
    outliers = f"; outliers {', '.join(repeat['outliers'])} keep their own rules" if repeat["outliers"] else ""
    lines = [f"/* {repeat['events'][0]}-{repeat['events'][-1]}: {repeat['count']} repeats of one transition every "
             f"{repeat['period_s']:g} s (measured); duration {duration} ms, range {low}-{high}, easing {repeat['easing']} "
             f"(one fit across {repeat['shared']} repeats){outliers}; states from {lead['id']} */"]
    if before or after:
        lines.append(f"/* from {{ {'; '.join(before)} }} to {{ {'; '.join(after)} }} (fitted amplitude) */")
    lines.append(f".{css_class(lead['id'])} {{ transition: {transition}; }}   /* suggested for every repeat */")
    return "\n".join(lines)


def event_snippet(event, dpr):
    by_id = {e["id"]: e for e in event["elements"] if not e["timing"]["instant"]}
    if not by_id:
        return None
    groups = [g for g in event["timing_groups"] if g["elements"][0] in by_id]
    if is_uniform_stagger(event, groups, by_id):
        return stagger_snippet(event, groups, by_id, dpr)
    return "\n".join(group_snippet(event, group, by_id, dpr) for group in groups) or None


def apply_dpr(run, motion):
    """Adds css px fields and CSS snippets for --dpr, writes OUT/motion/css.txt (kept out of the cached result)."""
    dpr = run.params["motion"]["dpr"]
    units = f"css px (dpr {css_number(dpr, 2)})"
    snippets = []
    repeat_of = {event_id: repeat for repeat in motion["summary"].get("repeats", []) for event_id in repeat["events"]}
    by_id = {event["id"]: event for event in motion["events"]}
    for event in motion["events"]:
        for element in event["elements"]:
            element["bbox_css"] = [round(v / dpr, 1) for v in element["bbox_src"]]
            element["geometry"]["translate_css_px"] = [round(v / dpr, 1) for v in element["geometry"]["translate_px"]]
    for event in motion["events"]:
        repeat = repeat_of.get(event["id"])
        if repeat and event["id"] != repeat["events"][0] and event["id"] not in repeat["outliers"]:
            continue                        # the group's rule is written once, at its first event
        if repeat and event["id"] not in repeat["outliers"]:
            lead = next(e for e in by_id[repeat["lead"]]["elements"] if e["id"] == repeat["lead_element"])
            snippet = repeat_snippet(repeat, lead, dpr)
        else:
            snippet = event_snippet(event, dpr) if event["analysed"] else None
        if snippet:
            event["css"] = {"units": units, "file": CSS_FILE, "snippet": snippet}
            snippets.append(snippet)
    if not snippets:
        (run.out / CSS_FILE).unlink(missing_ok=True)       # a stale one from an earlier run into this OUT
    else:
        path = run.out_path(CSS_FILE)
        path.write_text(f"/* video-lens motion CSS · {units} · measured = read from frames, fitted = model fit, "
                        f"suggested = rounded for use */\n\n" + "\n\n".join(snippets) + "\n", encoding="utf-8")
    if dpr == 1 and run.display_size[0] >= RETINA_MIN_WIDTH and snippets:
        run.warn(DPR_WARNING)
