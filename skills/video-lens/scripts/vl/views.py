"""Image limits and visual-token cost (spec 5.4), and the one grid tiler every sheet uses. Every image a view
writes goes through write_view."""
import math

import cv2
import numpy as np

PATCH_PX = 28
MAX_SIDE_PX = 1932              # largest multiple of 28 under Claude Code's 2000 px cap: nothing gets rescaled
MAX_VISUAL_TOKENS = 4761        # 69 x 69 patches
JPEG_QUALITY = 90
LABEL_FONT = cv2.FONT_HERSHEY_SIMPLEX
ZOOM_CELL_W = 480               # zoom and overview cells never grow wider
LABEL_BAR_PX = 22
LABEL_SCALE = 0.5
BAR_BGR = (0, 0, 0)
LABEL_BGR = (255, 255, 255)


def visual_tokens(width, height):
    return math.ceil(width / PATCH_PX) * math.ceil(height / PATCH_PX)


def fit_to_limits(image, max_side=MAX_SIDE_PX):
    """Downscales (INTER_AREA) so the longer side is at most max_side; smaller images are returned unchanged."""
    height, width = image.shape[:2]
    scale = min(1.0, max_side / max(height, width))
    if scale == 1.0:
        return image
    size = (max(1, math.floor(width * scale)), max(1, math.floor(height * scale)))
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)


def check_limits(width, height):
    """Raises instead of `assert`, so the check also holds under `python -O`."""
    if max(width, height) > MAX_SIDE_PX or visual_tokens(width, height) > MAX_VISUAL_TOKENS:
        raise AssertionError(f"view {width}x{height} breaks the image limit "
                             f"({MAX_SIDE_PX} px per side, {MAX_VISUAL_TOKENS} tokens)")


def write_view(run, image, rel_path, *, level, kind, covers="", cells=None, cell_px=None):
    """Writes OUT/rel_path (JPEG q90 for .jpg, PNG otherwise), records it in run.views and returns the record.

    The recorded w, h and visual_tokens are those of the written pixels, which is what O1 checks.
    """
    height, width = image.shape[:2]
    check_limits(width, height)
    path = run.out / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    is_jpeg = path.suffix.lower() in (".jpg", ".jpeg")
    ok, data = cv2.imencode(path.suffix, image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY] if is_jpeg else [])
    if not ok:
        raise ValueError(f"cannot encode {path.suffix} image for {rel_path}")
    path.write_bytes(data.tobytes())
    view = {"file": str(rel_path), "level": level, "kind": kind, "covers": covers, "cells": cells,
            "cell_px": list(cell_px) if cell_px else None, "w": width, "h": height,
            "visual_tokens": visual_tokens(width, height)}
    run.views[:] = [v for v in run.views if v["file"] != view["file"]] + [view]
    return view


def view_line(run, view):
    return f"{run.out / view['file']} · {view['w']}x{view['h']} · {view['visual_tokens']:,} tok"


def ascii_label(text):
    """Hershey fonts draw ASCII only; anything else becomes '?'. Korean belongs in text files, not on images."""
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in text)


def draw_label(image, text, x, y, scale=0.5, color=(255, 255, 255), thickness=1):
    cv2.putText(image, ascii_label(text), (x, y), LABEL_FONT, scale, color, thickness, cv2.LINE_AA)


def tile_size(image_shape, rows, max_tile_w):
    """(tile_w, tile_h): at most max_tile_w wide, shrunk so `rows` tiles plus label bars fit 1932 px in height. With
    at most 4 columns of 480 px a sheet stays within 1932 px per side, hence within 4,761 tokens (1932 = 69 x 28)."""
    height, width = image_shape[:2]
    tallest = MAX_SIDE_PX // rows - LABEL_BAR_PX
    tile_w = max(1, min(max_tile_w, math.floor(tallest * width / height)))
    return tile_w, max(1, round(tile_w * height / width))


def tile_sheet(images, labels, cols, tile_w, tile_h):
    """Tiles in reading order, each with a black label bar below it (Hershey, ASCII)."""
    rows = math.ceil(len(images) / cols)
    sheet = np.full((rows * (tile_h + LABEL_BAR_PX), cols * tile_w, 3), 255, np.uint8)
    for number, (image, label) in enumerate(zip(images, labels)):
        row, col = divmod(number, cols)
        x, y = col * tile_w, row * (tile_h + LABEL_BAR_PX)
        is_tile_sized = image.shape[:2] == (tile_h, tile_w)
        sheet[y:y + tile_h, x:x + tile_w] = image if is_tile_sized else cv2.resize(image, (tile_w, tile_h), interpolation=cv2.INTER_AREA)
        cv2.rectangle(sheet, (x, y + tile_h), (x + tile_w - 1, y + tile_h + LABEL_BAR_PX - 1), BAR_BGR, -1)
        draw_label(sheet, label, x + 6, y + tile_h + 16, LABEL_SCALE, LABEL_BGR)
        cv2.rectangle(sheet, (x, y), (x + tile_w - 1, y + tile_h + LABEL_BAR_PX - 1), LABEL_BGR, 1)
    return sheet


def grid_tokens(image_shape, count, cols, max_tile_w):
    """Visual tokens of a tile_sheet of `count` images shaped like image_shape: what a zoom will cost."""
    cols = max(1, min(cols, count))
    rows = math.ceil(count / cols)
    tile_w, tile_h = tile_size(image_shape, rows, max_tile_w)
    return visual_tokens(cols * tile_w, rows * (tile_h + LABEL_BAR_PX))
