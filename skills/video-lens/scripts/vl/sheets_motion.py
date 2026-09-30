"""Motion views (spec 7.10): the event sheet motion/Mxx_sheet.png, `vl.py zoom --event` and the overview cells.

Sheet rows, 1040 px wide in two 520 px columns:
  1 before | after with numbered element boxes, `#i +delay ms` and flow arrows coloured by delay
  2 timing map (energy time-centroid, TURBO) | where it changed (sqrt sum |dF|, INFERNO)
  3 progress plot (measured dots, fitted line, 100 ms ticks) | key numbers per element
  4 filmstrip of the union region at 0/25/50/75/100 %
"""
import math

import cv2
import numpy as np

from . import decode, easing, elements, pixels, views
from .errors import EXIT_BAD_ARGS, VlError

PANEL_W = 520
PANEL_MAX_H = 420
PLOT_H = 300
STRIP_GAP = 4
STRIP_MAX_H = 200
STRIP_POINTS = (0.0, 0.25, 0.5, 0.75, 1.0)
PLOT_Y_RANGE = (-0.15, 1.3)
WHITE = (255, 255, 255)
BOX_RED = (0, 0, 255)
SERIES_COLOURS = [(180, 119, 31), (14, 127, 255), (44, 160, 44), (40, 39, 214), (189, 103, 148), (75, 86, 140)]
TEXT_LINE_PX = 14
ZOOM_COLUMNS = 4
ZOOM_MARGIN_PX = 16
CROP_MARGIN_PX = 8
CROP_MAX_WIDTH_SHARE = 0.5
ZOOM_CELLS = 12                 # vl.py zoom --cells default
CROP_POINTS = (0, 50, 100)
OVERVIEW_EVENTS = 8
OVERVIEW_MARGIN_PX = 24


def outlined(image, text, x, y, scale=0.5):
    views.draw_label(image, text, x, y, scale, (0, 0, 0), 3)
    views.draw_label(image, text, x, y, scale, WHITE, 1)
    return image


def panel(image, title, width=PANEL_W, max_height=PANEL_MAX_H):
    """Fit into width x max_height (area resampling), padded white to the full width, titled top-left."""
    scale = min(width / image.shape[1], max_height / image.shape[0])
    size = (max(1, int(image.shape[1] * scale)), max(1, int(image.shape[0] * scale)))
    fitted = cv2.resize(image, size, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    fitted = cv2.copyMakeBorder(fitted, 0, 0, 0, width - size[0], cv2.BORDER_CONSTANT, value=WHITE)
    return outlined(fitted, title, 8, 20), scale


def side_by_side(left, right):
    height = max(left.shape[0], right.shape[0])
    pad = lambda image: cv2.copyMakeBorder(image, 0, height - image.shape[0], 0, 0, cv2.BORDER_CONSTANT, value=WHITE)
    return np.hstack([pad(left), pad(right)])


def delay_colour(delay, low, high):
    value = 0 if high - low < 1e-9 else (delay - low) / (high - low)
    colour = cv2.applyColorMap(np.array([[int(value * 255)]], np.uint8), cv2.COLORMAP_TURBO)[0, 0]
    return tuple(int(c) for c in colour)


def displacement(element):
    """Work-px vector from the start state to the end state, or None for elements that do not move."""
    for index, channel in enumerate(element.channels):
        if channel.name in ("translate", "scroll") and element.axis is not None:
            return element.axis * element.fit.amplitudes[index]
    return None


def after_panel(record, window, found):
    image = window.images[-1].copy()
    starts = [e["timing"]["start_s"] for e in record["elements"]]
    low, high = (min(starts), max(starts)) if starts else (0.0, 0.0)
    for number, (element, element_record) in enumerate(zip(found, record["elements"]), 1):
        x, y, w, h = element.box
        start = element_record["timing"]["start_s"]
        colour = delay_colour(start, low, high)
        cv2.rectangle(image, (x, y), (x + w, y + h), BOX_RED, 2)
        vector = displacement(element)
        if vector is not None and np.hypot(*vector) >= 2:
            centre = np.array([x + w / 2, y + h / 2])
            tail, head = (centre - vector, centre) if element.origin == "end" else (centre, centre + vector)
            cv2.arrowedLine(image, tuple(int(v) for v in tail), tuple(int(v) for v in head), colour, 3, cv2.LINE_AA, tipLength=0.15)
        views.draw_label(image, f"#{number} +{round((start - low) * 1000)}ms", x + 4, y + 18, 0.6, BOX_RED, 2)
    return panel(image, f"after {window.times[-1]:.3f}s (elements, delay)")[0]


def change_maps(window, threshold):
    """(time-centroid map, active mask, summed change) over the event's consecutive frame differences."""
    images, times = window.images, window.times
    total = np.zeros(images[0].shape[:2], np.float32)
    weighted = np.zeros_like(total)
    for k in range(1, len(images)):
        diff = pixels.channel_max(cv2.absdiff(images[k], images[k - 1])).astype(np.float32)
        diff[diff <= threshold] = 0
        total += diff
        weighted += diff * float(times[k] - times[0])
    size = max(5, int(0.02 * math.hypot(*total.shape))) | 1
    summed = cv2.boxFilter(total, -1, (size, size), normalize=False)
    centroid = cv2.boxFilter(weighted, -1, (size, size), normalize=False) / np.maximum(summed, 1e-6)
    return centroid, total > 0, total


def map_panels(window, threshold):
    centroid, active, total = change_maps(window, threshold)
    span = max(1e-6, float(window.times[-1] - window.times[0]))
    coloured = cv2.applyColorMap(np.clip(centroid / span * 255, 0, 255).astype(np.uint8), cv2.COLORMAP_TURBO)
    grey = cv2.cvtColor(cv2.cvtColor(window.images[-1], cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    timing = np.where(active[..., None], coloured, (grey * 0.35 + 150).astype(np.uint8))
    heat = cv2.applyColorMap(np.clip(np.sqrt(total / (total.max() + 1e-6)) * 255, 0, 255).astype(np.uint8), cv2.COLORMAP_INFERNO)
    return panel(timing, "timing map: blue=early red=late")[0], panel(heat, "where it changed (sum |diff|)")[0]


def progress_plot(found, record, times, width=PANEL_W, height=PLOT_H):
    image = np.full((height, width, 3), 255, np.uint8)
    left, right, top, bottom = 40, 10, 30, 30
    t0, t1 = float(times[0]), float(times[-1])
    low, high = PLOT_Y_RANGE
    to_x = lambda t: int(left + (t - t0) / max(t1 - t0, 1e-6) * (width - left - right))
    to_y = lambda p: int(height - bottom - (np.clip(p, low, high) - low) / (high - low) * (height - top - bottom))
    for level in (0, 0.5, 1):
        cv2.line(image, (left, to_y(level)), (width - right, to_y(level)), (220, 220, 220), 1)
    for ms in range(0, int((t1 - t0) * 1000) + 1, 100):
        x = to_x(t0 + ms / 1000)
        cv2.line(image, (x, height - bottom), (x, height - bottom + 4), (0, 0, 0), 1)
        views.draw_label(image, str(ms), x - 10, height - 10, 0.35, (0, 0, 0))
    dense = np.linspace(t0, t1, 200)
    for number, (element, element_record) in enumerate(zip(found, record["elements"]), 1):
        colour = SERIES_COLOURS[(number - 1) % len(SERIES_COLOURS)]
        index = elements.primary_index(element)
        channel = element.channels[index]
        progress = element.fit.progress(channel, index)
        line = np.array([[to_x(t), to_y(p)] for t, p in zip(dense, element.fit.easing(dense))], np.int32)
        cv2.polylines(image, [line], False, colour, 1, cv2.LINE_AA)
        for t, p, is_reliable in zip(times, progress, channel.reliable):
            cv2.circle(image, (to_x(t), to_y(p)), 2 if is_reliable else 1, colour, -1)
        views.draw_label(image, f"#{number} {element_record['easing']['name']} {element_record['timing']['duration_ms']:.0f}ms",
                         left + 4, top + 14 * number, 0.38, colour)
    return outlined(image, "progress (dots measured, line fitted), ms from rest", 8, 16, 0.45)


def element_lines(number, element, first_start):
    """Key numbers of one element: its shared fit, or each channel's own fit when the channels split (as the report's
    rows show them)."""
    timing, fit = element["timing"], element["easing"]
    span = timing["duration_ms_range"]
    duration = f"{timing['duration_ms']:.0f}ms" + (f" ({span[0]:.0f}-{span[1]:.0f})" if span else "")
    dx, dy = element["geometry"]["translate_px"]
    lines = [f"#{number} {'+'.join(element['types'])} start {timing['start_s']:.4f} +{round((timing['start_s'] - first_start) * 1000)}ms"]
    owns = [(name, channel["own_fit"]) for name, channel in element["channels"].items() if "own_fit" in channel]
    if owns:
        lines += [f"   {name} {own['start_s']:.4f} {own['duration_ms']:.0f}ms {own['name']} rmse {own['rmse']:.4f} (split)"
                  for name, own in owns]
    else:
        lines.append(f"   {duration} {fit['name']} rmse {fit['fit_rmse']:.4f}")
    lines.append(f"   travel {dx:g},{dy:g} src px t50 {timing['t50_s']:.4f} {element['confidence']}")
    return lines


def text_panel(record, width=PANEL_W, height=PLOT_H):
    image = np.full((height, width, 3), 255, np.uint8)
    lines = [f"{record['id']} {record['class']} {record['mode']} {len(record['elements'])} el"]
    first_start = min((e["timing"]["start_s"] for e in record["elements"]), default=0.0)
    blocks = [element_lines(number, element, first_start) for number, element in enumerate(record["elements"], 1)]
    room, shown = (height - 12) // TEXT_LINE_PX, 0
    for block in blocks:
        pointer_lines = 0 if shown + 1 == len(blocks) else 1
        if len(lines) + len(block) + pointer_lines > room:
            break
        lines += block
        shown += 1
    if shown < len(blocks):
        rest = record["elements"][shown:]
        lines.append(f"+{len(rest)} more ({rest[0]['id']}-{rest[-1]['id']}): vl.py rows OUT --kind elements")
    for row, text in enumerate(lines):
        views.draw_label(image, text, 8, 18 + row * TEXT_LINE_PX, 0.4, (0, 0, 0))
    return image


def filmstrip(window, width=2 * PANEL_W):
    """5 cells of the activity box, each labelled in a bar below it (a label drawn over the cell hid a list's top row)."""
    x, y, w, h = window.activity_box
    w, h = max(w, 1), max(h, 1)
    cell_w = (width - STRIP_GAP * (len(STRIP_POINTS) - 1)) // len(STRIP_POINTS)
    scale = min(cell_w / w, STRIP_MAX_H / h)
    size = (max(1, int(w * scale)), max(1, int(h * scale)))
    t0, t1 = window.times[0], window.times[-1]
    cells = []
    for share in STRIP_POINTS:
        k = int(np.argmin(np.abs(window.times - (t0 + share * (t1 - t0)))))
        crop = cv2.resize(window.images[k][y:y + h, x:x + w], size, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        bar = np.full((views.LABEL_BAR_PX, size[0], 3), views.BAR_BGR, np.uint8)
        views.draw_label(bar, f"{int(share * 100)}% {window.times[k]:.3f}s", 4, 15, 0.4, views.LABEL_BGR)
        cells.append(cv2.copyMakeBorder(np.vstack([crop, bar]), 0, 0, 0, cell_w - size[0] + STRIP_GAP,
                                        cv2.BORDER_CONSTANT, value=WHITE))
    strip = np.hstack(cells)[:, :width]
    return cv2.copyMakeBorder(strip, 0, 0, 0, width - strip.shape[1], cv2.BORDER_CONSTANT, value=WHITE)


def event_sheet(record, window, found, threshold):
    """The L2 sheet of one event (BGR image, within the view limits)."""
    before = panel(window.images[0], f"before {window.times[0]:.3f}s")[0]
    timing, heat = map_panels(window, threshold)
    rows = [side_by_side(before, after_panel(record, window, found)), side_by_side(timing, heat),
            side_by_side(progress_plot(found, record, window.times), text_panel(record)), filmstrip(window)]
    return views.fit_to_limits(np.vstack(rows))


def find_event(run, event_id):
    motion = run.analysis.get("motion") or {}
    for event in motion.get("events", []):
        if event["id"] == event_id:
            if not event["analysed"] or not event["elements"]:
                raise VlError(EXIT_BAD_ARGS, f"motion event {event_id} was not fitted", "Pick an event with elements in the report")
            return event
    raise VlError(EXIT_BAD_ARGS, f"no motion event {event_id} in this analysis", "Use an id from the report's motion table")


def clip_box(box, run, margin):
    width, height = run.display_size
    x, y, w, h = box
    x0, y0 = max(0, x - margin), max(0, y - margin)
    x1, y1 = min(width, x + w + margin), min(height, y + h + margin)
    return int(x0), int(y0), int(x1 - x0), int(y1 - y0)


def fitted_times(event, count):
    """count times across the fitted span: progress-uniform for one timing group, time-uniform otherwise."""
    elements = event["elements"]
    start = min(e["timing"]["start_s"] for e in elements)
    end = max(e["timing"]["end_s"] for e in elements)
    shares = (np.arange(count) + 0.5) / count
    if len(event["timing_groups"]) != 1:
        return list(start + shares * (end - start))
    curve = easing.make_curve("fit", elements[0]["easing"]["cubic_bezier"])
    rising = np.maximum.accumulate(curve.table)
    return [start + float(np.interp(p, rising, easing.DENSE_X)) * (end - start) for p in shares]


def zoom_times(run, event, cells):
    first = run.frame_index(event["first_change_s"])
    last = run.frame_index(event["last_change_s"])
    times = [float(run.packet_times[max(0, first - 2)])]
    times += [run.snap_time(t) for t in fitted_times(event, max(1, cells - 2))]
    times.append(float(run.packet_times[min(len(run.packet_times) - 1, last + 3)]))
    return list(dict.fromkeys(times))


def swept_box(element):
    """The element's box at both rest states (source px)."""
    x, y, w, h = element["bbox_src"]
    dx, dy = element["geometry"]["translate_px"]
    sign = -1 if element["origin"] == "end" else 1
    ox, oy = x + sign * dx, y + sign * dy
    x0, y0 = min(x, ox), min(y, oy)
    return [int(math.floor(x0)), int(math.floor(y0)), int(math.ceil(max(x, ox) + w - x0)), int(math.ceil(max(y, oy) + h - y0))]


def crop_boxes(run, event):
    """(element, source box) for the elements that get native crops: none for boxes wider than half the frame (a
    scroll strip), which the zoom grid already shows whole."""
    boxes = [(element, clip_box(swept_box(element), run, CROP_MARGIN_PX)) for element in event["elements"]]
    return [(element, box) for element, box in boxes if box[2] <= CROP_MAX_WIDTH_SHARE * run.display_size[0]]


def element_crops(run, event):
    """Native crops at 0/50/100 % of each element's fitted span."""
    rendered = []
    stream_index = run.probe["video"]["index"]
    for element, box in crop_boxes(run, event):
        timing = element["timing"]
        number = element["id"].split(".")[-1]
        times = [run.snap_time(timing["start_s"] + share / 100 * timing["duration_ms"] / 1000) for share in CROP_POINTS]
        for share, image in zip(CROP_POINTS, decode.read_frames(run.input_path, times, roi=box, stream_index=stream_index)):
            rendered.append(views.write_view(run, views.fit_to_limits(image), f"zoom/{event['id']}_{number}_{share}.png",
                                             level="L3", kind="crop", covers=element["id"]))
    return rendered


def zoom_event(run, event_id, cells):
    """zoom/Mxx.jpg (rest -2 frames, fitted steps, settled +3 frames) plus native crops of each element."""
    event = find_event(run, event_id)
    box = clip_box(event["box_src"], run, ZOOM_MARGIN_PX)
    times = zoom_times(run, event, cells)
    crops = decode.read_frames(run.input_path, times, roi=box, stream_index=run.probe["video"]["index"])
    labels = [f"{t:.4f}s" for t in times]
    labels[0], labels[-1] = f"rest {times[0]:.4f}s", f"settled {times[-1]:.4f}s"
    columns = min(ZOOM_COLUMNS, len(crops))
    tile_w, tile_h = views.tile_size(crops[0].shape, math.ceil(len(crops) / columns), zoom_cell_w(crops[0].shape))
    sheet = views.tile_sheet(crops, labels, columns, tile_w, tile_h)
    view = views.write_view(run, sheet, f"zoom/{event_id}.jpg", level="L2", kind="event", covers=event_id,
                            cells=len(crops), cell_px=(tile_w, tile_h))
    return [view] + element_crops(run, event)


def zoom_cell_w(crop_shape):
    """Zoom cells are native crops shrunk to at most 480 px, never enlarged."""
    return min(views.ZOOM_CELL_W, crop_shape[1])


def zoom_tokens(run, event, cells=ZOOM_CELLS):
    """Visual tokens `vl.py zoom --event` will write for this event's grid (the report prints them beforehand)."""
    _, _, w, h = clip_box(event["box_src"], run, ZOOM_MARGIN_PX)
    return views.grid_tokens((h, w), cells, ZOOM_COLUMNS, zoom_cell_w((h, w)))


def overview_cells(run, skip=frozenset()):
    """3 cells per motion event (rest before, 50 %, rest after), cropped to the event box + 24 px, <= 8 events.
    Events in `skip` (scene changes in mode both) are left out."""
    motion = run.analysis.get("motion") or {}
    events = [e for e in motion.get("events", []) if e["class"] == "motion" and e["id"] not in skip]
    events = sorted(sorted(events, key=lambda e: -e["features"]["peak_blob_frac"])[:OVERVIEW_EVENTS], key=lambda e: e["first_change_s"])
    plan = []
    for event in events:
        middle = (np.mean([e["timing"]["t50_s"] for e in event["elements"]]) if event["elements"]
                  else (event["first_change_s"] + event["last_change_s"]) / 2)
        before, after = event["window_s"]
        plan += [(event, "before", before), (event, "50%", run.snap_time(float(middle))), (event, "after", after)]
    if not plan:
        return []
    paths = decode.cached_frames(run, [t for _, _, t in plan])
    cells = []
    for (event, label, t), path in zip(plan, paths):
        x, y, w, h = clip_box(event["box_src"], run, OVERVIEW_MARGIN_PX)
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        cells.append((image[y:y + h, x:x + w], f"{event['id']} {label} {t:.3f}"))
    return cells
