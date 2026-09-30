"""L1 overview images (spec 7.10), tiled from the cells the content and motion modules pick.

overview.jpg holds the content cells (shot starts, then the largest changes); in motion mode it holds the motion
cells (rest before, 50 %, rest after per event). In mode both the motion cells go to overview_motion.jpg, because
event crops and full frames do not share one cell shape; scene changes (slide swaps, cuts) get no motion cells.
Keyframe sheets, motion sheets and zooms are written by their own modules; this module only lays out the overview
grid.
"""
import math

import cv2
import numpy as np

from . import report, sheets_content, sheets_motion, views

OVERVIEW_FILE = "overview.jpg"
MOTION_OVERVIEW_FILE = "overview_motion.jpg"
CONTENT_COLUMNS = 8             # 8 x 5 cells of 16:9 = 1932 x 819 px, where D3 read 40/40 4-digit codes
MOTION_COLUMNS = 3              # one row per event: rest before, 50 %, rest after
PAD_BGR = (48, 48, 48)


def render_views(run):
    """Writes the overview image(s) and records them in run.views. An overview this run does not write is removed,
    so a rerun into the same OUT in another mode leaves no stale one behind."""
    content_cells = sheets_content.overview_cells(run)
    motion_cells = sheets_motion.overview_cells(run, skip=report.scene_change_ids(run.analysis))
    written = set()
    if content_cells:
        written.add(write_overview(run, content_cells, OVERVIEW_FILE, CONTENT_COLUMNS)["file"])
    if motion_cells:
        name = MOTION_OVERVIEW_FILE if content_cells else OVERVIEW_FILE
        written.add(write_overview(run, motion_cells, name, MOTION_COLUMNS)["file"])
    for name in {OVERVIEW_FILE, MOTION_OVERVIEW_FILE} - written:
        (run.out / name).unlink(missing_ok=True)


def write_overview(run, cells, rel_path, max_columns):
    images = [image for image, _ in cells]
    labels = [label for _, label in cells]
    columns = min(len(images), max_columns)
    by_aspect = sorted(images, key=lambda image: image.shape[1] / image.shape[0])
    max_tile_w = min(views.ZOOM_CELL_W, views.MAX_SIDE_PX // columns, max(image.shape[1] for image in images))
    tile_w, tile_h = views.tile_size(by_aspect[len(images) // 2].shape, math.ceil(len(images) / columns), max_tile_w)
    fitted = [letterbox(image, tile_w, tile_h) for image in images]
    sheet = views.tile_sheet(fitted, labels, columns, tile_w, tile_h)
    covers = ",".join(dict.fromkeys(label.split()[0] for label in labels))
    return views.write_view(run, sheet, rel_path, level="L1", kind="overview", covers=covers, cells=len(images),
                            cell_px=(tile_w, tile_h))


def letterbox(image, width, height):
    """`image` scaled to fit width x height with its aspect kept, centred on a dark pad."""
    img_h, img_w = image.shape[:2]
    scale = min(width / img_w, height / img_h)
    size = (max(1, round(img_w * scale)), max(1, round(img_h * scale)))
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    resized = cv2.resize(image, size, interpolation=interpolation)
    canvas = np.full((height, width, 3), PAD_BGR, np.uint8)
    x, y = (width - size[0]) // 2, (height - size[1]) // 2
    canvas[y:y + size[1], x:x + size[0]] = resized
    return canvas
