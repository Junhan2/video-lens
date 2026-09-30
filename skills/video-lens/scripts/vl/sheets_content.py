"""Content views (spec 7.10): keyframe sheets (L2 sheets/kf_NN.jpg), the overview cells the integrator tiles into
overview.jpg (L1), and zoom sheets of a segment or a time range (L2 zoom/*.jpg). Ported from D2 proto_sheet.py.
"""
import math

import cv2
import numpy as np

from . import decode, views
from .context import FRAME_MATCH_S, clock
from .errors import EXIT_BAD_ARGS, EXIT_EMPTY_RANGE, VlError

KF_COLS = 4
KF_ROWS = 4
KF_TILE_W = 384
ZOOM_COLS = 4
ZOOM_MAX_CELLS = 48             # 12 rows: tiles stay about 140 px tall inside 1932 px
OVERVIEW_MAX_CELLS = 40
SHOT_REASONS = ("first", "shot")


def keyframe_label(keyframe):
    """`K012 03:12.4 S`: S = settled (held >= 0.2 s), m = moving."""
    return f"{keyframe['id']} {clock(keyframe['t'])} {'S' if keyframe['settled'] else 'm'}"


def write_keyframe_sheets(run, keyframes):
    """Every keyframe that is not a same_as repeat, in order, 4x4 per sheet; fills each keyframe's sheet and cell."""
    shown = [k for k in keyframes if k["same_as"] is None]
    per_sheet = KF_COLS * KF_ROWS
    written = []
    for number, start in enumerate(range(0, len(shown), per_sheet), 1):
        chunk = shown[start:start + per_sheet]
        images = [cv2.imread(str(run.out / k["file"])) for k in chunk]
        tile_w, tile_h = views.tile_size(images[0].shape, KF_ROWS, KF_TILE_W)
        sheet = views.tile_sheet(images, [keyframe_label(k) for k in chunk], KF_COLS, tile_w, tile_h)
        rel = f"sheets/kf_{number:02d}.jpg"
        written.append(views.write_view(run, sheet, rel, level="L2", kind="keyframes",
                                        covers=f"{chunk[0]['id']}-{chunk[-1]['id']}", cells=len(chunk),
                                        cell_px=(tile_w, tile_h)))
        for cell, keyframe in enumerate(chunk):
            keyframe["sheet"], keyframe["cell"] = rel, cell
    return written


def overview_cells(run):
    """Up to 40 (BGR image, label) cells for overview.jpg: shot starts first (evenly thinned when there are more
    than 40), then the keyframes with the largest change; same_as repeats never; returned in time order."""
    content = run.analysis.get("content")
    if not content:
        return []
    keyframes = [k for k in content["keyframes"] if k["same_as"] is None]
    starts = [k for k in keyframes if k["reason"] in SHOT_REASONS]
    if len(starts) > OVERVIEW_MAX_CELLS:
        starts = [starts[i] for i in np.unique(np.round(np.linspace(0, len(starts) - 1, OVERVIEW_MAX_CELLS)).astype(int))]
    others = sorted((k for k in keyframes if k["reason"] not in SHOT_REASONS), key=lambda k: -k["change_frac"])
    chosen = sorted((starts + others)[:OVERVIEW_MAX_CELLS], key=lambda k: k["t"])
    return [(cv2.imread(str(run.out / k["file"])), keyframe_label(k)) for k in chosen]


def zoom_segment(run, segment_id, cells):
    """zoom/Sxx.jpg: `cells` frames spread evenly over shot Sxx (from the content analysis)."""
    content = run.analysis.get("content")
    if not content:
        raise VlError(EXIT_BAD_ARGS, "this OUT has no content analysis, so it has no segments",
                      "Use zoom --range A:B, or rerun analyze with --mode content or both")
    shot = next((s for s in content["shots"] if s["id"] == segment_id), None)
    if shot is None:
        raise VlError(EXIT_BAD_ARGS, f"no segment {segment_id}", f"Segments are S01 to {content['shots'][-1]['id']}")
    return [zoom_sheet(run, shot["start_s"], shot["end_s"], cells, f"zoom/{segment_id}.jpg", segment_id)]


def zoom_range(run, start_s, end_s, cells):
    """zoom/r_<A>-<B>.jpg: `cells` frames spread evenly over [A, B); works for any analysis mode."""
    return [zoom_sheet(run, start_s, end_s, cells, f"zoom/r_{start_s:.3f}-{end_s:.3f}.jpg", f"{start_s:g}-{end_s:g}")]


def zoom_sheet(run, start_s, end_s, cells, rel, covers):
    """4 columns of 480 px cells, each labelled with its clock time and exact frame time (for vl.py frame --t)."""
    if cells > ZOOM_MAX_CELLS:
        raise VlError(EXIT_BAD_ARGS, f"--cells {cells} does not fit one 1932 px sheet",
                      f"Use --cells {ZOOM_MAX_CELLS} or fewer, or zoom a shorter range")
    frame_times = run.packet_times[(run.packet_times >= start_s - FRAME_MATCH_S) & (run.packet_times < end_s - FRAME_MATCH_S)]
    if frame_times.size == 0:
        raise VlError(EXIT_EMPTY_RANGE, f"no video frame between {start_s:g} and {end_s:g} s", "Pick a range inside the video")
    picks = np.unique(np.round(np.linspace(0, frame_times.size - 1, min(cells, frame_times.size))).astype(int))
    times = [float(frame_times[p]) for p in picks]
    images = decode.read_frames(run.input_path, times, width=views.ZOOM_CELL_W, stream_index=run.probe["video"]["index"])
    rows = math.ceil(len(images) / ZOOM_COLS)
    tile_w, tile_h = views.tile_size(images[0].shape, rows, views.ZOOM_CELL_W)
    sheet = views.tile_sheet(images, [f"{clock(t)}  t={t:.3f}" for t in times], min(ZOOM_COLS, len(images)), tile_w, tile_h)
    return views.write_view(run, sheet, rel, level="L2", kind="segment", covers=covers, cells=len(images),
                            cell_px=(tile_w, tile_h))
