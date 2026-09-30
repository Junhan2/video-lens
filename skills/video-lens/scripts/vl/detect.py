"""Motion stage (spec 7.8): the detection pass at a fixed 640 px scale, events and their classes, the loop period,
then one windowed decode at work scale that feeds elements, fits, timing groups, stagger, CSS and sheets.

`analyze_motion(run)` is the stage `vl.py analyze` calls in motion and both modes.
"""
from collections import deque
from dataclasses import dataclass, replace

import cv2
import numpy as np

from . import css, decode, easing, elements, pixels, sheets_motion, views
from .cache import write_atomic
from .context import motion_time

ALGORITHM = 11                  # bump when a change alters cached detect/motion results
CHANGED_MIN_PX = 5              # changed pixels (detection scale) for a frame to join an event
SUBTLE_BLOB_FRAC = 0.002
MICRO_BLOB_FRAC = 0.0005
LOOP_MIN_EVENTS = 3
LOOP_MAX_CV = 0.1
LISTED_TIMES_MAX = 20
TAIL_S = 0.1                    # the window runs 100 ms past the last change: ease-out frames below the threshold
LEAD_S = 0.1                    # a colour change is measured from a rest frame up to 100 ms before the window (lead_start)
CROP_MARGIN, CROP_MIN_MARGIN_PX = elements.CROP_MARGIN, elements.CROP_MIN_MARGIN_PX
MAX_EVENT_FRAMES = 240          # frames held in memory for one event; longer continuous motion is listed, not fitted
WINDOW_MATCH_S = 0.0001
ANALYSED_CLASSES = ("motion", "subtle")
SUBTLE_NOTE = "subtle: faint change (peak max diff 16 to 31 at the 640 px detection scale); check the sheet"
ORDER_MIN_SPREAD = 0.5          # x the median element size: box jitter along an axis is not an order
ONE_FRAME_NOTE = "instant: one frame changes (a cut or swap); no easing or duration to measure, not fitted"
CUT_AT_START_NOTE = ("motion was already running at the first frame of the range (--start or the file start): start, "
                     "duration and easing describe the visible rest only; widen the range")
CUT_AT_END_NOTE = ("motion was still running at the last frame of the range (--end or the file end): end, duration and "
                   "easing describe the visible part only; widen the range")
REPEAT_MIN_EVENTS = 3
HELD_FRAME_INTERVALS = 1.5
SETTLING_SHARE = 0.05           # a last frame changing by less than this share of the event's peak energy is settling
REPEAT_TRAVEL_TOLERANCE = 0.15  # events repeating one transition move within 15 % of their median travel
REPEAT_PASSES = 2               # shared fit, then each repeat's start under it, twice
# a repeat is an outlier when the shared curve and duration miss it by more than 1.5 x its own fit plus the whole RMSE a
# high-confidence fit may have (the channel-split rule)
REPEAT_OUTLIER_RATIO, REPEAT_OUTLIER_MARGIN = elements.SPLIT_RATIO, elements.SPLIT_MARGIN


@dataclass
class Series:
    """Per detection frame: time, changed px, max diff, largest blob, change energy, change box (x0, y0, x1, y1)."""
    t: np.ndarray
    changed: np.ndarray
    maxd: np.ndarray
    blob: np.ndarray
    energy: np.ndarray
    box: np.ndarray
    size: np.ndarray            # detection frame (w, h)
    region: np.ndarray          # source region (x, y, w, h) the detection frame covers

    @property
    def area(self):
        return int(self.size[0] * self.size[1])

    def box_to_source(self, box):
        x0, y0, x1, y1 = box
        rx, ry, rw, rh = self.region
        sx, sy = rw / self.size[0], rh / self.size[1]
        return [rx + x0 * sx, ry + y0 * sy, rx + x1 * sx, ry + y1 * sy]


@dataclass
class Event:
    first: int                  # series index of the first changed frame
    last: int
    frames: int
    peak_maxd: int
    peak_blob: int
    box: list                   # detection px (x0, y0, x1, y1)
    cls: str = ""
    id: str | None = None
    is_selected: bool = False


def even_region(x, y, w, h, width, height):
    """Even-aligned region inside the frame, so a yuv420p crop is never rounded by ffmpeg."""
    x0, y0 = max(0, int(x) // 2 * 2), max(0, int(y) // 2 * 2)
    x1, y1 = min(width, -(-int(np.ceil(x + w)) // 2) * 2), min(height, -(-int(np.ceil(y + h)) // 2) * 2)
    return x0, y0, max(2, x1 - x0), max(2, y1 - y0)


def detection_region(run):
    width, height = run.display_size
    roi = run.params["roi"]
    return even_region(*roi, width, height) if roi else (0, 0, width, height)


def detection_filter(run, region):
    side = run.params["motion"]["det_side"]
    width, height = run.display_size
    crop = "" if region == (0, 0, width, height) else "crop={2}:{3}:{0}:{1},".format(*region)
    return f"{crop}scale=w={side}:h={side}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=area"


def frame_row(image, previous, threshold):
    """(changed, maxd, blob, energy, box) of one frame against the previous one; max over B, G, R."""
    diff = pixels.channel_max(cv2.absdiff(image, previous))
    mask = (diff > threshold).astype(np.uint8)
    changed = int(cv2.countNonZero(mask))
    maxd = int(diff.max())
    energy = float(diff[mask.astype(bool)].sum(dtype=np.int64)) if changed else 0.0
    if changed < CHANGED_MIN_PX:
        return changed, maxd, 0, energy, (-1, -1, -1, -1)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    parts = stats[1:]
    blob = int(parts[:, 4].max())
    boxed = parts[parts[:, 4] >= elements.BOX_COMPONENT_MIN_PX]
    boxed = boxed if len(boxed) else parts
    box = (int(boxed[:, 0].min()), int(boxed[:, 1].min()), int((boxed[:, 0] + boxed[:, 2]).max()), int((boxed[:, 1] + boxed[:, 3]).max()))
    return changed, maxd, blob, energy, box


def detection_series(run):
    """Streaming detection pass over the range (cached as detect.npz)."""
    motion = run.params["motion"]
    region = detection_region(run)
    params = {"algorithm": ALGORITHM, "range": [run.range.start_s, run.range.end_s], "region": region,
              "det_side": motion["det_side"], "pix_threshold": motion["pix_threshold"]}
    entry = run.cache.entry("detect", params, "npz")
    if entry.is_hit:
        return Series(**entry.load_npz())
    expected = run.frames_in_range()
    stream = decode.stream_run(run, detection_filter(run, region))
    rows, times, previous = [], [], None
    for frame in stream:
        rows.append((0, 0, 0, 0.0, (-1, -1, -1, -1)) if previous is None
                    else frame_row(frame.image, previous, motion["pix_threshold"]))
        times.append(frame.t)
        previous = frame.image
        run.progress("motion detect", frame.index + 1, max(expected, frame.index + 1))
    columns = list(zip(*rows)) if rows else [[]] * 5
    series = Series(t=np.array(times, float), changed=np.array(columns[0], np.int64), maxd=np.array(columns[1], np.int64),
                    blob=np.array(columns[2], np.int64), energy=np.array(columns[3], float),
                    box=np.array(columns[4], np.int64).reshape(-1, 4), size=np.array(stream.size or (0, 0), np.int64),
                    region=np.array(region, np.int64))
    entry.save_npz(**series.__dict__)
    return series


def is_noise_level(maxd, blob, area, noise_maxd):
    """The noise rule of spec 7.8.1 (max diff < 16, or < 32 with a blob < 0.2 % of the frame)."""
    return maxd < noise_maxd // 2 or is_speck(maxd, blob, area, noise_maxd)


def is_speck(maxd, blob, area, noise_maxd):
    """Faint and small: max diff < 32 and the largest changed blob < 0.2 % of the frame; works on arrays."""
    return (maxd < noise_maxd) & (blob < SUBTLE_BLOB_FRAC * area)


def gap_runs(indices, times, gap_s):
    """Sorted frame indices split wherever two consecutive frames are more than gap_s apart."""
    if not len(indices):
        return []
    return np.split(indices, np.nonzero(np.diff(times[indices]) > gap_s)[0] + 1)


def make_event(indices, series):
    boxes = series.box[indices]
    return Event(int(indices[0]), int(indices[-1]), len(indices), int(series.maxd[indices].max()),
                 int(series.blob[indices].max()), [int(boxes[:, 0].min()), int(boxes[:, 1].min()),
                                                   int(boxes[:, 2].max()), int(boxes[:, 3].max())])


def group_events(series, gap_s, noise_maxd):
    """Frames with >= 5 changed px, grouped by the gap rule. Specks (faint and small) never carry an event across a
    gap: they join the event they lie within gap_s of (lead-in, tail) and otherwise form events of their own.
    Orbit's encoder shimmer (max diff 10 to 24, blobs <= 94 px) otherwise chains its 11 transitions into 9 events,
    while a fade's faint tail (max diff 11 over the whole title) still carries its event."""
    changed = series.changed >= CHANGED_MIN_PX
    weak = changed & is_speck(series.maxd, series.blob, series.area, noise_maxd)
    cores = [list(run) for run in gap_runs(np.nonzero(changed & ~weak)[0], series.t, gap_s)]
    firsts = np.array([series.t[core[0]] for core in cores])
    lasts = np.array([series.t[core[-1]] for core in cores])
    loose = []
    for index in np.nonzero(weak)[0]:
        distance = np.maximum(np.maximum(firsts - series.t[index], series.t[index] - lasts), 0.0)
        nearest = int(np.argmin(distance)) if len(cores) else -1
        if nearest >= 0 and distance[nearest] <= gap_s:
            cores[nearest].append(int(index))
        else:
            loose.append(int(index))
    groups = [np.sort(np.array(core)) for core in cores] + gap_runs(np.array(loose, int), series.t, gap_s)
    return sorted((make_event(indices, series) for indices in groups), key=lambda event: event.first)


def classify(event, area, noise_maxd):
    """noise / subtle / micro / motion from the peak max diff and the largest blob (spec 7.8.1)."""
    if is_noise_level(event.peak_maxd, event.peak_blob, area, noise_maxd):
        return "noise"
    if event.peak_maxd < noise_maxd:
        return "subtle"
    return "micro" if event.peak_blob < MICRO_BLOB_FRAC * area else "motion"


def loop_period(events, series):
    """median spacing of motion-event starts, reported when their cv < 0.1 (>= 3 motion events)."""
    starts = [series.t[e.first] for e in events if e.cls == "motion"]
    if len(starts) < LOOP_MIN_EVENTS:
        return None
    steps = np.diff(starts)
    cv = float(np.std(steps) / np.mean(steps))
    return {"period_s": round(float(np.median(steps)), 3), "cv": round(cv, 3)} if cv < LOOP_MAX_CV else None


def is_selected(event, series, selection):
    if not selection:
        return True
    first, last = series.t[event.first], series.t[event.last]
    return any(item.get("id") == event.id or ("range" in item and first <= item["range"][1] and last >= item["range"][0])
               for item in selection)


def list_events(run, series):
    """Classify every event, give ids to the listed ones (motion, subtle, micro with --micro), mark the selection."""
    motion = run.params["motion"]
    events = group_events(series, motion["gap_ms"] / 1000, motion["noise_maxd"])
    listed_classes = ANALYSED_CLASSES + (("micro",) if motion["micro"] else ())
    for event in events:
        event.cls = classify(event, series.area, motion["noise_maxd"])
    listed = [e for e in events if e.cls in listed_classes]
    for number, event in enumerate(listed, 1):
        event.id = f"M{number:02d}"
    for event in listed:
        event.is_selected = is_selected(event, series, motion["events"])
    known = {e.id for e in listed}
    for item in motion["events"] or []:
        if "id" in item and item["id"] not in known:
            run.warn(f"--events {item['id']}: no such motion event")
    return events, listed


def window_bounds(event, series):
    """[frame before the first change, last frame within 100 ms after the last change]: exact detection times."""
    tail = int(np.searchsorted(series.t, series.t[event.last] + TAIL_S + WINDOW_MATCH_S, side="right")) - 1
    return float(series.t[event.first - 1]), float(series.t[max(tail, event.last)])


def lead_start(event, series):
    """The earliest frame within LEAD_S before the window's first, TAIL_S clear of an earlier change (as that event's
    window is): the rest a colour change is measured from. A colour change moves the detection map far less than an
    edge does: an ease one 180 ms long stayed under the threshold for its first 14 ms, and measured from the window's
    first frame, 6 % into it, it began 19 ms late and read easeOutCubic."""
    earliest = series.t[event.first - 1] - LEAD_S
    earlier = np.flatnonzero(series.changed[:event.first] >= CHANGED_MIN_PX)
    if earlier.size:
        earliest = max(earliest, series.t[earlier[-1]] + TAIL_S)
    lead = int(np.searchsorted(series.t, earliest - WINDOW_MATCH_S, side="left"))
    return float(series.t[min(lead, event.first - 1)])


def work_crop(run, series, events):
    """Union of the analysed events' activity boxes + 10 % margin, at least CROP_MIN_MARGIN_PX (source px,
    even-aligned), and the work size."""
    boxes = np.array([series.box_to_source(e.box) for e in events])
    x0, y0 = boxes[:, 0].min(), boxes[:, 1].min()
    x1, y1 = boxes[:, 2].max(), boxes[:, 3].max()
    mx, my = (max(CROP_MIN_MARGIN_PX, CROP_MARGIN * size) for size in (x1 - x0, y1 - y0))
    rx, ry, rw, rh = (int(v) for v in series.region)
    left, top = max(rx, x0 - mx), max(ry, y0 - my)
    crop = even_region(left, top, min(rx + rw, x1 + mx) - left, min(ry + rh, y1 + my) - top, rx + rw, ry + rh)
    side = run.params["motion"]["work_side"]
    scale = min(1.0, side / max(crop[2], crop[3])) if side > 0 else 1.0
    return crop, (max(2, int(round(crop[2] * scale))), max(2, int(round(crop[3] * scale))))


def work_filter(crop, size):
    x, y, w, h = crop
    return f"crop={w}:{h}:{x}:{y}" + ("" if size == (w, h) else f",scale={size[0]}:{size[1]}:flags=area")


def stream_windows(run, series, events, crop, size):
    """Yields (event, times, images, rest) per event as soon as its window is complete; memory holds one window. `rest`
    is the frame at lead_start, decoded with the window from there."""
    spans = [(lead_start(e, series), *window_bounds(e, series)) for e in events]
    stream = decode.stream_run(run, work_filter(crop, size), windows=[(lead, end) for lead, _, end in spans])
    pending = deque(zip(events, spans))
    buffered = []
    for frame in stream:
        buffered.append((frame.t, frame.image))
        while pending and frame.t > pending[0][1][2] + WINDOW_MATCH_S:
            yield window_of(pending.popleft(), buffered)
            buffered = [f for f in buffered if pending and f[0] >= pending[0][1][0] - WINDOW_MATCH_S]
    while pending:
        yield window_of(pending.popleft(), buffered)


def window_of(item, buffered):
    event, (lead, start, end) = item
    frames = [(t, image) for t, image in buffered if start - WINDOW_MATCH_S <= t <= end + WINDOW_MATCH_S]
    rest = next((image for t, image in buffered if abs(t - lead) <= WINDOW_MATCH_S), frames[0][1] if frames else None)
    return event, np.array([t for t, _ in frames]), [image for _, image in frames], rest


def times_match(times, series, event):
    """Work-frame times must equal detection times within 0.1 ms (spec 7.8.2)."""
    start, end = window_bounds(event, series)
    expected = series.t[(series.t >= start - WINDOW_MATCH_S) & (series.t <= end + WINDOW_MATCH_S)]
    return len(expected) == len(times) and bool(np.all(np.abs(expected - times) <= WINDOW_MATCH_S))


def timing_groups(records, dt):
    """Elements whose start is within 1 frame and duration within 2 frames of a group's first element."""
    groups = []
    for record in sorted(records, key=lambda r: r["timing"]["start_s"]):
        start, duration = record["timing"]["start_s"], record["timing"]["duration_ms"]
        if groups and abs(start - groups[-1]["start_s"]) <= dt + 1e-9 and abs(duration - groups[-1]["duration_ms"]) <= 2000 * dt:
            groups[-1]["elements"].append(record["id"])
        else:
            groups.append({"start_s": start, "duration_ms": duration, "elements": [record["id"]]})
    for group in groups:
        group["delay_ms"] = round((group["start_s"] - groups[0]["start_s"]) * 1000, 1)
    return groups


def correlation(a, b):
    if np.std(a) < 1e-9 or np.std(b) < 1e-9:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def start_clusters(records, dt):
    """Element ids whose starts lie within 1 frame of the cluster's first, whatever their durations: the parts of one
    list row without a background start together, and their fitted durations differ (339 to 447 ms for one 400 ms
    row), which put them in separate timing groups and read as 0 ms stagger steps."""
    clusters = []
    for record in sorted(records, key=lambda r: r["timing"]["start_s"]):
        start = record["timing"]["start_s"]
        if clusters and start - clusters[-1][0] <= dt + 1e-9:
            clusters[-1][1].append(record["id"])
        else:
            clusters.append((start, [record["id"]]))
    return [ids for _, ids in clusters]


def stagger(records, dt):
    """Median-t50 differences between consecutive start clusters (>= 3), linearity and spatial order (spec 7.8.6).
    Split elements stay out while others remain: one timing misses one of their channels, so their start places no
    step (fading 2 px separators split and started up to 18 ms off their rows)."""
    clusters = start_clusters([r for r in records if not r["split"]] or records, dt)
    if len(clusters) < 3:
        return None
    by_id = {r["id"]: r for r in records}
    t50 = np.array([np.median([by_id[i]["timing"]["t50_s"] for i in ids]) for ids in clusters])
    boxes = [[by_id[i]["bbox_src"] for i in ids] for ids in clusters]
    centres = np.array([np.mean([[b[0] + b[2] / 2, b[1] + b[3] / 2] for b in group], axis=0) for group in boxes])
    extent = np.median([[b[2], b[3]] for group in boxes for b in group], axis=0)
    steps = np.diff(t50) * 1000
    index = np.arange(len(t50))
    line = np.polyval(np.polyfit(index, t50, 1), index)
    delays = t50 - t50[0]
    distance = np.hypot(*(centres - centres.mean(axis=0)).T)
    spread_x, spread_y = (np.ptp(centres[:, axis]) >= ORDER_MIN_SPREAD * extent[axis] for axis in (0, 1))
    corr_x = correlation(delays, centres[:, 0]) if spread_x else 0.0
    corr_y = correlation(delays, centres[:, 1]) if spread_y else 0.0
    corr_d = correlation(delays, distance) if spread_x or spread_y else 0.0
    return {"step_ms_median": round(float(np.median(steps)), 1), "step_ms": [round(float(s), 1) for s in steps],
            "linearity_ms": round(float(np.max(np.abs(t50 - line))) * 1000, 1), "method": "t50-diff",
            "order": stagger_order(corr_x, corr_y, corr_d), "order_corr_x": round(corr_x, 2), "order_corr_y": round(corr_y, 2)}


def stagger_order(corr_x, corr_y, corr_d, minimum=0.8):
    candidates = [(abs(corr_y), "top-to-bottom" if corr_y > 0 else "bottom-to-top"),
                  (abs(corr_x), "left-to-right" if corr_x > 0 else "right-to-left"), (corr_d, "from-centre")]
    strength, name = max(candidates)
    return name if strength >= minimum else "none"


def event_base(event, series):
    first, last = series.t[event.first], series.t[event.last]
    return {"id": event.id, "class": event.cls, "first_change_s": motion_time(first), "last_change_s": motion_time(last),
            "features": {"peak_maxdiff": event.peak_maxd, "peak_blob_frac": round(event.peak_blob / series.area, 5),
                         "changed_frames": event.frames},
            "box_src": source_box(series.box_to_source(event.box)), "window_s": [motion_time(v) for v in window_bounds(event, series)],
            "analysed": False, "mode": None, "segmentation": None, "elements": [], "timing_groups": [], "stagger": None,
            "scroll": None, "css": None, "evidence": {"sheet": None, "sheet_tokens": None, "crops": []},
            "notes": [SUBTLE_NOTE] if event.cls == "subtle" else []}


def source_box(corners):
    x0, y0, x1, y1 = corners
    return [int(np.floor(x0)), int(np.floor(y0)), int(np.ceil(x1 - np.floor(x0))), int(np.ceil(y1 - np.floor(y0)))]


def scroll_record(element, element_record, window):
    """The event's scroll block from its scroll element: travel, fitted and per-step peak speed, responses."""
    measure = element.scroll
    moving = measure.is_moving()
    steps = measure.shifts[1:] / np.array(window.geometry.scale)
    speeds = np.linalg.norm(steps, axis=1) / np.maximum(np.diff(window.times), 1e-6)
    reliable = measure.reliable[1:]
    return {"element": element_record["id"],
            "travel_px": [round(float(v), 1) for v in window.geometry.vector_to_source(measure.travel)],
            "peak_speed_px_s": element_record["geometry"]["peak_speed_px_s"],
            "peak_step_speed_px_s": round(float(speeds[reliable].max()) if reliable.any() else 0.0, 1),
            "response_min": round(float(measure.responses[moving].min()) if moving.any() else 1.0, 3),
            "unreliable_frac": round(measure.unreliable_frac(moving), 3)}


@dataclass
class MainElement:
    """An event's main element kept for the shared repeat fit: the Element, its position in the event record, a
    window without images (records need only its times and geometry) and the event's segmentation."""
    element: elements.Element
    index: int
    window: elements.Window
    segmentation: str


@dataclass(frozen=True)
class FitContext:
    run: object
    series: Series
    settings: elements.Settings
    geometry: elements.WorkGeometry


def main_index(found, records):
    """The element a repeat is judged by: the scroll element, else the largest box; None when every one is instant.
    A card cut at a carousel's edge can add a small element to some repeats and not to others."""
    fitted = [k for k, record in enumerate(records) if not record["timing"]["instant"]]
    if not fitted:
        return None
    return max(fitted, key=lambda k: (found[k].scroll is not None, found[k].box[2] * found[k].box[3]))


def analyse_event(record, event, times, images, rest, context):
    """Fills one event record from its window frames (and the rest frame of its lead-in); returns (its elements,
    MainElement or None)."""
    run, series, settings, geometry = context.run, context.series, context.settings, context.geometry
    if not times_match(times, series, event):
        run.warn(f"{event.id}: window frames do not match the detection times; timings of this event may be off")
    window = elements.make_window(times, images, rest, settings.pix_threshold, geometry)
    mode, segmentation, found = elements.analyse_window(window, settings)
    found.sort(key=lambda e: (round(e.fit.start_s / settings.dt), e.box[1], e.box[0]))
    records = [elements.element_record(e, n, event.id, window, settings, segmentation) for n, e in enumerate(found, 1)]
    boundary_notes = cut_by_range_notes(event, series)
    for element in records if boundary_notes else []:
        element["confidence"] = "low"
    record["notes"] += boundary_notes
    record.update(analysed=True, mode=mode, segmentation=segmentation, elements=records)
    record["timing_groups"] = timing_groups(records, settings.dt)
    record["stagger"] = stagger(records, settings.dt)
    scrolled = next(((e, r) for e, r in zip(found, records) if e.scroll is not None), None)
    if scrolled:
        record["scroll"] = scroll_record(*scrolled, window)
    if not records:
        record["notes"].append("no element could be fitted in this event")
    main = main_index(found, records)
    light = elements.Window(window.times, [], [], None, window.activity_box, window.geometry)
    return found, None if main is None else MainElement(found[main], main, light, segmentation)


def cut_by_range_notes(event, series):
    """Notes for motion the range cuts off: its first change is the range's second frame (so the frame before may
    not be at rest), or its last change is the range's last frame. A first frame held longer than 1.5 frame intervals
    is a rest state (VFR files drop the repeats of a still frame). A file that drops frames also ends at its last change,
    which the file does not show held: there a last frame changing by less than SETTLING_SHARE of the event's peak is
    the motion settling (a drawer's last sub-pixel row, codec specks at 0.06 to 0.8 % of the peak), not a cut."""
    steps = np.diff(series.t)
    dt = float(np.median(steps)) if steps.size else 0.0
    is_first_held = steps.size > 0 and steps[0] > HELD_FRAME_INTERVALS * dt
    notes = [CUT_AT_START_NOTE] if event.first <= 1 and not is_first_held else []
    drops_frames = steps.size > 0 and steps.max() > HELD_FRAME_INTERVALS * dt
    is_last_settling = drops_frames and series.energy[-1] < SETTLING_SHARE * series.energy[event.first:event.last + 1].max()
    return notes + ([CUT_AT_END_NOTE] if event.last >= len(series.t) - 1 and not is_last_settling else [])


def render_sheets(run, series, events, crop, size, records, found, settings):
    """Sheets of the largest events, drawn after the repeat fit so they show the final records. Their windows (at most
    --sheets) are decoded a second time rather than holding every window's frames until the repeat fit is done."""
    geometry = elements.WorkGeometry((crop[0], crop[1]), (size[0] / crop[2], size[1] / crop[3]))
    sheets = {}
    for event, times, images, rest in stream_windows(run, series, events, crop, size):
        window = elements.make_window(times, images, rest, settings.pix_threshold, geometry)
        sheets[event.id] = sheets_motion.event_sheet(records[event.id], window, found[event.id], settings.pix_threshold)
    return sheets


def fit_events(run, series, events, loop):
    """Window decode of the selected events, the per-event analysis, the shared fit of repeat groups, then the
    sheets (cached next to motion.json). Returns (event records, sheets, work, repeat groups). One-frame events are
    swaps or cuts: tracking a full-frame 'element' there cost 32 s and fitted nothing."""
    motion = run.params["motion"]
    fitted = [e for e in events if e.is_selected and e.cls in ANALYSED_CLASSES + ("micro",)]
    too_long = [e for e in fitted if e.last - e.first + 1 > MAX_EVENT_FRAMES]
    one_frame = [e for e in fitted if e.frames <= 1]
    fitted = [e for e in fitted if e not in too_long and e not in one_frame]
    records = {e.id: event_base(e, series) for e in events if e.id}
    for event in too_long:
        records[event.id]["notes"].append(f"continuous motion over {event.last - event.first + 1} frames: not fitted; "
                                          "use --start/--end or --roi around one animation")
    for event in one_frame:
        records[event.id]["notes"].append(ONE_FRAME_NOTE)
    found, mains = {}, {}
    if not fitted:
        return list(records.values()), {}, None, []
    crop, size = work_crop(run, series, fitted)
    geometry = elements.WorkGeometry((crop[0], crop[1]), (size[0] / crop[2], size[1] / crop[3]))
    dt = run.frame_interval_s or float(np.median(np.diff(series.t)))
    settings = elements.Settings(dt, easing.named_curves(motion["easings"]), motion["joint"], motion["motion_mode"],
                                 motion["pix_threshold"], motion["max_elements"])
    sheet_ids = frozenset(e.id for e in sorted(fitted, key=lambda e: -e.peak_blob)[:max(0, motion["sheets"])])
    context = FitContext(run, series, settings, geometry)
    for number, (event, times, images, rest) in enumerate(stream_windows(run, series, fitted, crop, size), 1):
        if len(times) < 2:
            continue
        elements_, main = analyse_event(records[event.id], event, times, images, rest, context)
        if event.id in sheet_ids:
            found[event.id] = elements_
        if main is not None:
            mains[event.id] = main
        run.progress("motion fit", number, len(fitted))
    repeats = fit_repeats(list(records.values()), mains, settings, loop)
    sheeted = [e for e in fitted if e.id in sheet_ids and e.id in found]
    sheets = render_sheets(run, series, sheeted, crop, size, records, found, settings)
    work = {"crop_src": list(crop), "size": list(size), "scale": round(size[0] / crop[2], 4)}
    return list(records.values()), sheets, work, repeats


def summary(events, series, listed):
    count = {c: sum(e.cls == c for e in events) for c in ("motion", "subtle", "micro", "noise")}
    return {"motion_events": count["motion"], "subtle_events": count["subtle"], "micro_events": count["micro"],
            "noise_events": count["noise"],
            "noise_times_s": [motion_time(series.t[e.first]) for e in events if e.cls == "noise"][:LISTED_TIMES_MAX],
            "micro_times_s": [motion_time(series.t[e.first]) for e in events if e.cls == "micro"][:LISTED_TIMES_MAX],
            "loop": loop_period(events, series), "listed_events": len(listed),
            "detect_size": [int(v) for v in series.size]}


def sheet_dir(entry):
    return entry.path.with_name(entry.path.name + ".sheets")


def motion_params(run):
    motion = run.params["motion"]
    table = easing.NAMED_TABLE if motion["easings"] is None else motion["easings"]
    keys = ("det_side", "pix_threshold", "gap_ms", "noise_maxd", "joint", "motion_mode", "max_elements", "micro",
            "sheets", "events", "work_side")
    return {"algorithm": ALGORITHM, "range": [run.range.start_s, run.range.end_s], "roi": run.params["roi"],
            "easings": easing.table_digest(table), **{k: motion[k] for k in keys}}


def compute_motion(run, entry):
    with run.timer("motion_detect"):
        series = detection_series(run)
        events, listed = list_events(run, series)
    with run.timer("motion_fit"):
        fitted = fit_events(run, series, events, loop_period(events, series)) if len(series.t) >= 2 else ([], {}, None, [])
        records, sheets, work, repeats = fitted
    folder = sheet_dir(entry)
    for event_id, image in sheets.items():
        ok, data = cv2.imencode(".png", views.fit_to_limits(image))
        write_atomic(folder / f"{event_id}_sheet.png", data.tobytes())
    result = {"summary": {**summary(events, series, listed), "repeats": repeats}, "work": work, "events": records}
    entry.save_json(result)
    return result


def write_sheets(run, result, entry):
    """Copies the cached sheet images into OUT/motion/ through write_view (limits checked, views recorded)."""
    folder = sheet_dir(entry)
    for record in result["events"]:
        path = folder / f"{record['id']}_sheet.png"
        if not record["analysed"] or not path.is_file():
            continue
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        view = views.write_view(run, image, f"motion/{record['id']}_sheet.png", level="L2", kind="event", covers=record["id"])
        record["evidence"].update(sheet=view["file"], sheet_tokens=view["visual_tokens"])


def main_record(event, mains):
    main = mains[event["id"]]
    return event["elements"][main.index]


def travel(element_record):
    return float(np.hypot(*element_record["geometry"]["translate_px"]))


def repeat_groups(events, mains, loop):
    """Events that repeat one transition, judged by their main elements (same types). With a loop (motion starts
    every period) that is every such event; otherwise runs of >= 3 consecutive events whose travel stays within
    15 % of the run's median. A loop or carousel then reads as one transition. Motion the range cuts off is only a
    part of the transition and stays out."""
    candidates = [e for e in events if e["analysed"] and e["id"] in mains
                  and not {CUT_AT_START_NOTE, CUT_AT_END_NOTE} & set(e["notes"])]
    if loop:
        types = [tuple(main_record(e, mains)["types"]) for e in candidates]
        common = max(set(types), key=types.count) if types else None
        group = [e for e, kind in zip(candidates, types) if kind == common]
        return [group] if len(group) >= REPEAT_MIN_EVENTS else []
    groups, current = [], []
    for event in candidates:
        record = main_record(event, mains)
        median = np.median([travel(main_record(e, mains)) for e in current]) if current else 0.0
        is_same = (current and record["types"] == main_record(current[0], mains)["types"]
                   and abs(travel(record) - median) <= REPEAT_TRAVEL_TOLERANCE * max(median, 1e-9))
        if is_same:
            current.append(event)
            continue
        groups.append(current)
        current = [event]
    groups.append(current)
    return [group for group in groups if len(group) >= REPEAT_MIN_EVENTS]


def stalled_mask(member):
    stalled = member.element.fit.stalled
    return np.zeros(len(member.window.times), bool) if stalled is None else stalled


def merged_progress(members, starts):
    """The members' channels as progress (0 at the start state, 1 at the end, from each member's own fit), every
    sample timed from its member's start: one data set of the repeated transition, sampled at many sub-frame phases.
    Frames a member's own fit found stalled stay out."""
    names = [c.name for c in members[0].element.channels]
    names = [n for n in names if all(n in [c.name for c in m.element.channels] for m in members)]
    t = np.concatenate([m.window.times - start for m, start in zip(members, starts)])
    order = np.argsort(t, kind="stable")
    channels = []
    for name in names:
        values, weights = [], []
        for member in members:
            index = next(k for k, c in enumerate(member.element.channels) if c.name == name)
            channel = member.element.channels[index]
            values.append(member.element.fit.progress(channel, index))
            weights.append(np.where(stalled_mask(member), 0.0, channel.weights))
        channels.append(easing.Channel(name, np.concatenate(values)[order], np.concatenate(weights)[order], 0.0, 1.0))
    return t[order], channels


def shared_fit(members, settings):
    """(shared Fit, starts): one curve and duration fitted to all members' samples at once, each member keeping its
    own start, refined under that curve and duration (REPEAT_PASSES times)."""
    starts = [m.element.fit.start_s for m in members]
    shared = None
    for _ in range(REPEAT_PASSES):
        t, channels = merged_progress(members, starts)
        shared = easing.fit_channels(t, channels, settings.dt, settings.curves)
        if shared is None:
            return None, starts
        starts = [easing.fit_start(m.window.times, [c.without(stalled_mask(m)) for c in m.element.channels], shared.curve,
                                   shared.duration_s, start + shared.start_s, settings.dt) for m, start in zip(members, starts)]
    return shared, starts


def member_fit(member, shared, start, settings):
    """A member's Fit under the shared curve and duration at its own start; ranges and ties are the shared fit's, its
    stalled frames its own fit's."""
    stalled = stalled_mask(member)
    fit = easing.fit_channels(member.window.times, [c.without(stalled) for c in member.element.channels], settings.dt,
                              (shared.curve,), with_free=False, timing=(start, shared.duration_s))
    return replace(fit, duration_range_s=shared.duration_range_s, near_ties=shared.near_ties, shape_tie=shared.shape_tie,
                   stalled=stalled)


def is_outlier(own, under_shared):
    return under_shared.rmse > REPEAT_OUTLIER_RATIO * own.rmse + REPEAT_OUTLIER_MARGIN


def shared_repeat(group, mains, settings):
    """(shared Fit, {event id: member Fit}, outlier ids) of one repeat group; outliers are left out of a second fit.
    A shared Fit of None: fewer than REPEAT_MIN_EVENTS events follow one curve and duration, so they do not repeat one
    transition after all."""
    members = [mains[e["id"]] for e in group]
    shared, starts = shared_fit(members, settings)
    if shared is None:
        return None, {}, []
    fits = {e["id"]: member_fit(m, shared, s, settings) for e, m, s in zip(group, members, starts)}
    outliers = [e["id"] for e in group if is_outlier(mains[e["id"]].element.fit, fits[e["id"]])]
    inliers = [e for e in group if e["id"] not in outliers]
    if len(inliers) < REPEAT_MIN_EVENTS:
        return None, {}, []
    if outliers:
        members = [mains[e["id"]] for e in inliers]
        shared, starts = shared_fit(members, settings)
        fits = {e["id"]: member_fit(m, shared, s, settings) for e, m, s in zip(inliers, members, starts)}
    return shared, {event_id: fit for event_id, fit in fits.items() if event_id not in outliers}, outliers


def apply_member(event, main, fit, note, settings):
    """Rebuilds the main element's record, the event's timing groups, stagger and scroll block under the shared fit."""
    main.element.fit = fit
    main.element.notes.append(note)
    number = main.index + 1
    event["elements"][main.index] = elements.element_record(main.element, number, event["id"], main.window, settings,
                                                            main.segmentation)
    event["timing_groups"] = timing_groups(event["elements"], settings.dt)
    event["stagger"] = stagger(event["elements"], settings.dt)
    if main.element.scroll is not None:
        event["scroll"] = scroll_record(main.element, event["elements"][main.index], main.window)


def fit_repeats(records, mains, settings, loop):
    """The analysis.json repeat groups. Each group gets ONE curve and duration fitted across its repeats with a start
    per repeat, and its members' main elements are re-recorded under it: per-repeat fits let each repeat trade its
    own curve and duration against its start, so starts of identical transitions jittered by tens of ms."""
    groups = []
    for group in repeat_groups(records, mains, loop):
        shared, fits, outliers = shared_repeat(group, mains, settings)
        if shared is None:
            continue
        ids = [e["id"] for e in group]
        note = (f"timing and curve from the shared fit of the {len(fits)} repeats {ids[0]}-{ids[-1]} (one curve and "
                "duration, own start)")
        for event in group:
            if event["id"] in fits:
                apply_member(event, mains[event["id"]], fits[event["id"]], note, settings)
        groups.append(repeat_summary(group, mains, shared, outliers))
    return groups


def repeat_summary(group, mains, shared, outliers):
    members = [(e, main_record(e, mains)) for e in group]
    inlier_starts = [r["timing"]["start_s"] for e, r in members if e["id"] not in outliers]
    low, high = shared.duration_range_s
    lead = next(e for e in group if e["id"] not in outliers)
    return {"events": [e["id"] for e in group], "lead": lead["id"], "lead_element": main_record(lead, mains)["id"],
            "count": len(group), "shared": len(group) - len(outliers),
            "outliers": outliers, "period_s": motion_time(np.median(np.diff(inlier_starts))),
            "starts_s": [r["timing"]["start_s"] for _, r in members],
            "duration_ms": round(shared.duration_s * 1000, 1), "duration_ms_range": [round(low * 1000, 1), round(high * 1000, 1)],
            "easing": shared.curve.name, "cubic_bezier": [round(v, 3) for v in shared.curve.bezier],
            "fit_rmse": round(shared.rmse, 4), "near_ties": shared.near_ties, "shape_tie": shared.shape_tie,
            "travel_px_median": round(float(np.median([travel(r) for _, r in members])), 1)}


def analyze_motion(run):
    """Stage entry (spec 7.8): returns the analysis.json `motion` section."""
    entry = run.cache.entry("motion", motion_params(run), "json")
    result = entry.load_json() if entry.is_hit else compute_motion(run, entry)
    write_sheets(run, result, entry)
    css.apply_dpr(run, result)
    for record in result["events"]:
        if record["class"] in ANALYSED_CLASSES:
            run.visual_onsets.append((record["first_change_s"], "motion"))
    return result
