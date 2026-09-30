"""Motion fixtures (spec 10.1), ported from D1 gen.py (cards composited sub-pixel with cv2.warpAffine) and D3
make_ui_clip.py (title fade + three staggered cards). Encoded with fixtures_core (libx264 crf 18, yuv420p); VFR
copies via mpdecimate.

Truth curves use their own bisection bezier, independent of vl/easing.py, so a bug there cannot hide in the truth.
Each build_* function writes one video at the path it is given.
"""
import functools

import cv2
import numpy as np

import fixtures_core as fc

W, H, FPS = 960, 600, 60
BG = (247, 245, 245)                        # BGR
BISECTION_STEPS = 60

EASE = (0.25, 0.1, 0.25, 1.0)
LINEAR = (0.0, 0.0, 1.0, 1.0)
EASE_OUT_CUBIC = (0.33, 1.0, 0.68, 1.0)
EASE_IN_CUBIC = (0.32, 0.0, 0.67, 0.0)
EASE_IN_OUT_CUBIC = (0.65, 0.0, 0.35, 1.0)
MD3_STANDARD = (0.2, 0.0, 0.0, 1.0)
MD2_STANDARD = (0.4, 0.0, 0.2, 1.0)
MD2_DECELERATE = (0.0, 0.0, 0.2, 1.0)

TRANSLATE = {"start": 0.2, "duration": 0.3, "name": "easeOutCubic", "bezier": EASE_OUT_CUBIC, "dx": 240}
TRANSLATE_SNAP = {"start": 0.2, "duration": 0.3, "name": "easeOutCubic", "bezier": EASE_OUT_CUBIC, "dx": 100}
SCALEFADE = {"start": 0.2, "duration": 0.25, "name": "ease", "bezier": EASE, "scale_from": 0.85}
ROTATE = {"start": 0.2, "duration": 0.4, "name": "easeInOutCubic", "bezier": EASE_IN_OUT_CUBIC, "degrees": 20}
COLOR = {"start": 0.2, "duration": 0.2, "name": "linear", "bezier": LINEAR,
         "from_bgr": (246, 130, 59), "to_bgr": (68, 68, 239)}          # #3b82f6 -> #ef4444
STAGGER = {"starts": [0.2 + 0.06 * k for k in range(5)], "duration": 0.4, "name": "md3-standard",
           "bezier": MD3_STANDARD, "dy": 24, "step_ms": 60}
FROZEN_TIMES = (0.3, 0.3167, 0.3333)        # frames 18, 19, 20 repeat frame 17 (t = 0.2833)
FROZEN_INDICES = (18, 19, 20)
EXIT = {"start": 0.2, "duration": 0.25, "name": "easeInCubic", "bezier": EASE_IN_CUBIC, "dy": 24}
CARET = {"period_s": 0.53, "size": (2, 24), "seconds": 3.0}
SPRING = {"start": 0.2, "duration": 0.6, "dx": 200, "stops": 41}
ADJACENT = {"start": 0.2, "duration": 0.4, "name": "md3-standard", "bezier": MD3_STANDARD, "dy": 24}
UI = {"size": (1280, 800), "seconds": 3.0,
      "title": {"start": 0.2, "duration": 0.4, "bezier": MD2_DECELERATE, "box": (340, 120, 600, 64)},
      "cards": [{"start": 0.8 + 0.1 * i, "duration": 0.5, "bezier": MD2_STANDARD, "box": (140 + i * 360, 420, 280, 200),
                 "dy": 60, "color": (70 + i * 40, 120, 200 - i * 30)} for i in range(3)]}
# Toasts that cross an edge (D1): each starts (or ends) exactly outside the edge it crosses, so the hidden part of
# the travel is the distance from its visible rest box to that edge. Boxes are (x, y, w, h) of the visible rest state.
TOAST_SIZE = (320, 72)
EDGE_TOASTS = {
    "right": {"start": 0.25, "duration": 0.35, "name": "ease-out", "bezier": (0.0, 0.0, 0.58, 1.0),
              "rest": (616, 40), "hidden": (W, 40), "origin": "end", "edge": "right"},
    "bottom": {"start": 0.25, "duration": 0.4, "name": "custom", "bezier": (0.1, 0.9, 0.2, 1.0),
               "rest": (320, 504), "hidden": (320, H), "origin": "end", "edge": "bottom"},
    "exit_left": {"start": 0.3, "duration": 0.25, "name": "easeInCubic", "bezier": EASE_IN_CUBIC,
                  "rest": (24, 260), "hidden": (-TOAST_SIZE[0], 260), "origin": "start", "edge": "left"},
}
# Six list rows slide in from the left while fading in, one custom curve, overlapping in time (D2): 30 fps, drawn at
# DPR 2 (CSS px x 2 in the video). Each row is one CSS animation of translateX and opacity.
ROWS = {"size": (1280, 960), "fps": 30, "dpr": 2, "count": 6, "start": 0.2, "step": 0.07, "duration": 0.45,
        "bezier": (0.25, 0.1, 0.1, 1.0), "dx_css": -32, "box_css": (80, 60, 480, 44), "pitch_css": 58, "seconds": 1.6}
# A flat list (D2 follow-up): rows without a background, so each row's avatar, name, detail line, badge and 1 CSS px
# separator are elements of their own that start together. DPR 2, 60 fps, one named curve, translateY + fade.
FLAT_LIST = {"count": 4, "start": 0.2, "step": 0.07, "duration": 0.36, "bezier": EASE_OUT_CUBIC, "dy_css": 12, "dpr": 2,
             "left_css": 40, "top_css": 30, "width_css": 400, "pitch_css": 60, "seconds": 1.1}
# A looping carousel (D4): a strip of cards inside a panel shifts one card pitch per transition, the same custom curve
# and duration every time. The period is not a whole number of frames (each repeat samples the curve at another
# sub-frame phase) and every frame gets mild pixel noise.
CAROUSEL = {"first": 0.3, "period": 0.9 + 0.37 / FPS, "count": 9, "duration": 0.5, "bezier": (0.6, 0.05, 0.25, 1.0),
            "panel": (60, 170, 840, 260), "card": (200, 220), "pitch": 240, "noise": 2.0, "seed": 4}
# Short modal exits (D3): scale 1 -> 0.95 and opacity 1 -> 0 under one accelerate curve, 60 fps.
MODAL_EXITS = {
    "md2-accelerate": {"start": 0.3, "duration": 0.16, "bezier": (0.4, 0.0, 1.0, 1.0)},
    "md3-emphasized-accelerate": {"start": 0.3, "duration": 0.2, "bezier": (0.3, 0.0, 0.8, 0.15)},
    "easeInQuad": {"start": 0.3, "duration": 0.18, "bezier": (0.11, 0.0, 0.5, 0.0)},
}
MODAL_SCALE_TO = 0.95
# A card whose transform and opacity run as two transitions (D2 control): the channels must split.
SPLIT_CARD = {"start": 0.2, "move": {"duration": 0.5, "bezier": EASE_OUT_CUBIC, "dy": 48},
              "fade": {"duration": 0.15, "bezier": LINEAR}}
# A toast sliding into a panel that clips it (overflow: hidden) well inside the frame (D1, clipping container).
PANEL = {"box": (140, 120, 600, 320), "bgr": (228, 222, 214)}
PANEL_TOAST = {"start": 0.25, "duration": 0.3, "name": "md3-emphasized-decelerate", "bezier": (0.05, 0.7, 0.1, 1.0),
               "rest": (396, 150), "hidden": (740, 150), "origin": "end", "edge": "right"}
# Slides with a CSS box-shadow (D1 follow-up): the blurred shadow reaches past the element, so a strip of it still shows
# inside the edge at the hidden rest state. A toast whose shadow falls below it (entering, and leaving), and a
# full-height drawer flush with the right edge (translateX(100%)) whose shadow falls to its left. Shadow blur is the
# CSS blur radius (sigma = blur / 2).
SHADOW_BGR = (42, 23, 15)
TOAST_SHADOW = {"offset": (0, 6), "blur": 18, "opacity": 0.22}
SHADOW_SLIDES = {
    "shadow_toast": {"start": 0.28, "duration": 0.44, "name": "custom", "bezier": (0.22, 0.9, 0.12, 1.0), "size": TOAST_SIZE,
                     "rest": (620, 508), "hidden": (W, 508), "shadow": TOAST_SHADOW,
                     "origin": "end", "edge": "right", "sprite": "toast"},
    "shadow_exit": {"start": 0.3, "duration": 0.3, "name": "easeInCubic", "bezier": EASE_IN_CUBIC, "size": TOAST_SIZE,
                    "rest": (620, 508), "hidden": (W, 508), "shadow": TOAST_SHADOW,
                    "origin": "start", "edge": "right", "sprite": "toast"},
    "drawer": {"start": 0.2, "duration": 0.3, "name": "md2-decelerate", "bezier": MD2_DECELERATE, "size": (300, H),
               "rest": (W - 300, 0), "hidden": (W, 0), "shadow": {"offset": (-4, 0), "blur": 16, "opacity": 0.2},
               "origin": "end", "edge": "right", "sprite": "drawer"},
}
# White sheets entering across the frame edge under long-tail curves (W1): their last 10 to 20 % moves below a pixel. A
# full-width bottom sheet taller than half the frame, and full-height right sheets beside a white top bar of their own
# colour, which their rest components merge with. Boxes are (x, y) of the visible rest state; hidden = translate(100 %).
VAUL = (0.32, 0.72, 0.0, 1.0)
EASE_OUT_EXPO = (0.16, 1.0, 0.3, 1.0)
TOP_BAR_H = 56
SHEETS = {
    "bottom_vaul": {"start": 0.25, "duration": 0.45, "name": "custom", "bezier": VAUL, "size": (W, 360), "rest": (0, 240),
                    "hidden": (0, H), "origin": "end", "edge": "bottom", "top_bar": False},
    "right_expo": {"start": 0.25, "duration": 0.5, "name": "easeOutExpo", "bezier": EASE_OUT_EXPO, "size": (340, H),
                   "rest": (W - 340, 0), "hidden": (W, 0), "origin": "end", "edge": "right", "top_bar": True},
    "right_vaul": {"start": 0.25, "duration": 0.45, "name": "custom", "bezier": VAUL, "size": (340, H),
                   "rest": (W - 340, 0), "hidden": (W, 0), "origin": "end", "edge": "right", "top_bar": True},
}
# Pops with an overshoot about the centre (W2): notification badges (a red disc, a white digit) growing from scale 0, one
# in a white nav bar 11 px from outline icons at its peak, and a chip growing from 0.6. The peak (scale 1.1) falls between frames.
EASE_OUT_BACK = (0.34, 1.56, 0.64, 1.0)
NAV_ICONS_X = (560, 640, 720, 800)
POPS = {
    "nav_badge": {"start": 0.3 + 0.37 / FPS, "duration": 0.35, "name": "easeOutBack", "bezier": EASE_OUT_BACK, "size": (26, 26),
                  "centre": (612.3, 32.6), "scale_from": 0.0, "sprite": "badge", "nav": True},
    "page_badge": {"start": 0.3 + 0.61 / FPS, "duration": 0.35, "name": "easeOutBack", "bezier": EASE_OUT_BACK,
                   "size": (40, 40), "centre": (480.6, 330.3), "scale_from": 0.0, "sprite": "badge", "nav": False},
    "chip": {"start": 0.3, "duration": 0.3, "name": "easeOutBack", "bezier": EASE_OUT_BACK, "size": (132, 40),
             "centre": (480.4, 330.7), "scale_from": 0.6, "sprite": "chip", "nav": False},
}
# Motion over and beside page content (weakness follow-ups): a page of grey text lines under the moving elements.
# A sheet taller than half the frame sliding over the text; top shades whose leading 76 px hold only a grab handle; a
# sheet over a page that dims to 45 % black on the sheet's curve; a drawer closing across the bottom edge (fast start).
PAGE_ROWS = 14
TEXT_SHEET = {"start": 0.21 + 0.27 / FPS, "duration": 0.47, "name": "easeOutQuart", "bezier": (0.25, 1.0, 0.5, 1.0),
              "size": (W, 380), "rest": (0, H - 380), "hidden": (0, H), "origin": "end", "edge": "bottom"}
SHADES = {
    "quint": {"start": 0.24 + 0.43 / FPS, "duration": 0.42, "name": "easeOutQuint", "bezier": (0.22, 1.0, 0.36, 1.0),
              "size": (W, 240), "rest": (0, 0), "hidden": (0, -240), "origin": "end", "edge": "top"},
    "expo": {"start": 0.22 + 0.61 / FPS, "duration": 0.38, "name": "easeOutExpo", "bezier": EASE_OUT_EXPO,
             "size": (W, 240), "rest": (0, 0), "hidden": (0, -240), "origin": "end", "edge": "top"},
}
SCRIM_SHEET = {"start": 0.23 + 0.13 / FPS, "duration": 0.46, "name": "custom", "bezier": VAUL, "size": (W, 330),
               "rest": (0, H - 330), "hidden": (0, H), "origin": "end", "edge": "bottom", "scrim": 0.45}
EXIT_SHEET = {"start": 0.3 + 0.41 / FPS, "duration": 0.4, "name": "custom", "bezier": VAUL, "size": (W, 280),
              "rest": (0, H - 280), "hidden": (0, H), "origin": "start", "edge": "bottom"}
# Pops that cover content (weakness follow-ups): a badge on an avatar's corner (entrance) and one on a nav icon's corner
# (exit, scale to 0), a badge popping 14 px beside a chip that slides, both over one text line, a dot popping on a white
# card while a label fading in far away widens the work crop across the card's edge, and a tooltip (scale + fade) over a
# text line.
AVATAR = {"centre": (452, 318), "radius": 22, "bgr": (160, 120, 88)}
ANCHORED_POPS = {
    "avatar_badge": {"start": 0.28 + 0.21 / FPS, "duration": 0.3, "name": "custom", "bezier": (0.18, 1.6, 0.5, 1.0),
                     "size": 24, "centre": (468.4, 301.7), "scale_from": 0.0, "scale_to": 1.0, "anchor": "avatar"},
    "icon_badge_exit": {"start": 0.3 + 0.36 / FPS, "duration": 0.28, "name": "easeInBack", "bezier": (0.36, 0.0, 0.66, -0.56),
                        "size": 28, "centre": (664.6, 21.3), "scale_from": 1.0, "scale_to": 0.0, "anchor": "nav"},
}
LINE_PAIR = {
    "chip": {"start": 0.18, "duration": 0.28, "name": "md2-decelerate", "bezier": MD2_DECELERATE, "size": (140, 40),
             "centre": (392.0, 302.0), "dx": 48},
    "badge": {"start": 0.24 + 0.23 / FPS, "duration": 0.46, "name": "custom", "bezier": (0.25, 1.7, 0.5, 1.0), "size": 44,
              "centre": (498.4, 302.3), "scale_from": 0.0},
}
CARD_DOT = {"card": (600, 170, 300, 230), "start": 0.3 + 0.19 / FPS, "duration": 0.26, "name": "custom",
            "bezier": (0.2, 1.45, 0.45, 1.0), "size": 34, "centre": (741.3, 262.6), "scale_from": 0.0,
            "far": {"start": 0.72, "duration": 0.2, "box": (36, 586, 180, 8)}}
TOOLTIP = {"start": 0.3 + 0.47 / FPS, "duration": 0.17, "name": "ease-out", "bezier": (0.0, 0.0, 0.58, 1.0),
           "size": (128, 32), "centre": (452.6, 305.2), "scale_from": 0.88}
# A primary button whose fill changes in place from blue to a red of the same BT.709 luma (rgb(40, 110, 230), luma
# 103.8, to rgb(235, 70, 60), luma 104.4), linear; its label and the chroma fringe of its edge look alike at both rests.
# Later a badge pops beside it (easeOutBack from scale 0), big enough to be a motion event of its own.
ISO_BUTTON = {"start": 0.45 + 0.29 / FPS, "duration": 0.22, "name": "linear", "bezier": LINEAR,
              "box": (372, 268, 216, 60), "radius": 10, "from_bgr": (230, 110, 40), "to_bgr": (60, 70, 235)}
ISO_BADGE = {"start": 1.05 + 0.41 / FPS, "duration": 0.32, "name": "easeOutBack", "bezier": EASE_OUT_BACK, "size": 32,
             "centre": (626.4, 280.3), "scale_from": 0.0}
# More colour changes in place, each alone, drawn 4x supersampled at sub-pixel positions: a fill of near the same BT.709
# luma (rgb(20, 160, 220), 134.6, to rgb(225, 110, 60), 130.8) inside a static 6 px grey border, linear; a ghost button
# filling (page to violet, its label violet to white, its 2 px violet border static), ease; a dark button lightening
# (rgb(24, 24, 27) to rgb(113, 113, 122)), ease, whose first frames change by less than the detection threshold.
BUTTON_SUPERSAMPLE = 4
BORDERED_BUTTON = {"start": 0.45 + 0.23 / FPS, "duration": 0.24, "name": "linear", "bezier": LINEAR,
                   "box": (360.4, 262.3, 232, 64), "radius": 16, "border": (6, (219, 213, 209)),
                   "from_bgr": (220, 160, 20), "to_bgr": (60, 110, 225), "label": "Download", "label_from_bgr": None}
GHOST_BUTTON = {"start": 0.45 + 0.37 / FPS, "duration": 0.2, "name": "ease", "bezier": EASE,
                "box": (388.3, 270.6, 184, 48), "radius": 12, "border": (2, (237, 58, 124)),
                "from_bgr": BG, "to_bgr": (237, 58, 124), "label": "Subscribe", "label_from_bgr": (237, 58, 124)}
DARK_BUTTON = {"start": 0.45 + 0.17 / FPS, "duration": 0.2, "name": "ease", "bezier": EASE,
               "box": (380.2, 270.4, 200, 52), "radius": 8, "border": None,
               "from_bgr": (27, 24, 24), "to_bgr": (122, 113, 113), "label": "Log in", "label_from_bgr": None}


@functools.cache
def bezier(x1, y1, x2, y2):
    """y(x) of cubic-bezier(x1, y1, x2, y2) by bisection on x(s)."""
    def curve(x):
        x = np.atleast_1d(np.asarray(x, float))
        low, high = np.zeros_like(x), np.ones_like(x)
        for _ in range(BISECTION_STEPS):
            s = (low + high) / 2
            below = 3 * (1 - s) ** 2 * s * x1 + 3 * (1 - s) * s ** 2 * x2 + s ** 3 < x
            low, high = np.where(below, s, low), np.where(below, high, s)
        s = (low + high) / 2
        return 3 * (1 - s) ** 2 * s * y1 + 3 * (1 - s) * s ** 2 * y2 + s ** 3
    return curve


def progress(t, start, duration, curve):
    return float(bezier(*curve)(np.clip((t - start) / duration, 0, 1))[0])


def spring_stops():
    """CSS linear() stops of a damped spring with two overshoots (1.31 at x 0.29, 1.03 at x 0.86); ends at exactly 1."""
    x = np.linspace(0, 1, SPRING["stops"])
    return x, 1 - np.exp(-4 * x) * np.cos(3.5 * np.pi * x)


def spring_progress(x):
    stops_x, stops_y = spring_stops()
    return float(np.interp(np.clip(x, 0, 1), stops_x, stops_y))


def card_sprite(w=220, h=120, seed=0, color=(255, 255, 255)):
    rng = np.random.default_rng(seed)
    image = np.zeros((h, w, 3), np.uint8)
    image[:] = color
    alpha = np.full((h, w), 255, np.uint8)
    cv2.circle(image, (28, 28), 16, (200, 140, 60), -1, cv2.LINE_AA)
    for i, y in enumerate(range(56, h - 10, 16)):
        length = int(rng.integers(80, w - 30))
        cv2.rectangle(image, (16, y), (16 + length, y + 7), (90, 90, 90) if i == 0 else (180, 180, 180), -1)
    cv2.putText(image, "Card %d" % seed, (54, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (40, 40, 40), 1, cv2.LINE_AA)
    cv2.rectangle(image, (0, 0), (w - 1, h - 1), (220, 220, 220), 1)
    return image, alpha


def composite(frame, sprite, alpha, cx, cy, scale=1.0, rot=0.0, opacity=1.0, snap=False):
    """Draws the sprite centred at (cx, cy), sub-pixel unless snap."""
    h, w = alpha.shape
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), rot, scale)
    if snap:
        cx, cy = round(cx), round(cy)
    matrix[0, 2] += cx - w / 2
    matrix[1, 2] += cy - h / 2
    height, width = frame.shape[:2]
    corners = matrix @ np.array([[0, w, 0, w], [0, 0, h, h], [1, 1, 1, 1]], float)
    x0, y0 = max(0, int(np.floor(corners[0].min())) - 2), max(0, int(np.floor(corners[1].min())) - 2)
    x1, y1 = min(width, int(np.ceil(corners[0].max())) + 2), min(height, int(np.ceil(corners[1].max())) + 2)
    if x1 <= x0 or y1 <= y0:
        return
    matrix[:, 2] -= (x0, y0)                # warp only the sprite's footprint: same pixels, a fraction of the work
    warped = cv2.warpAffine(sprite, matrix, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR, borderValue=0)
    weight = cv2.warpAffine(alpha, matrix, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR, borderValue=0).astype(np.float32) / 255 * opacity
    region = frame[y0:y1, x0:x1]
    frame[y0:y1, x0:x1] = (region * (1 - weight[..., None]) + warped * weight[..., None]).astype(np.uint8)


def blank(size=(W, H)):
    frame = np.zeros((size[1], size[0], 3), np.uint8)
    frame[:] = BG
    return frame


def translate_frames(fps, truth, snap=False, frozen=()):
    sprite, alpha = card_sprite(seed=1)
    for i in range(int(1.0 * fps)):
        t = (min(frozen) - 1 if i in frozen else i) / fps
        frame = blank()
        composite(frame, sprite, alpha, 250 + truth["dx"] * progress(t, truth["start"], truth["duration"], truth["bezier"]), 300, snap=snap)
        yield frame


def scalefade_frames():
    sprite, alpha = card_sprite(seed=2)
    for i in range(FPS):
        p = progress(i / FPS, SCALEFADE["start"], SCALEFADE["duration"], SCALEFADE["bezier"])
        frame = blank()
        composite(frame, sprite, alpha, 480, 300, scale=SCALEFADE["scale_from"] + (1 - SCALEFADE["scale_from"]) * p, opacity=p)
        yield frame


def rotate_frames():
    sprite, alpha = card_sprite(seed=3)
    for i in range(FPS):
        frame = blank()
        composite(frame, sprite, alpha, 480, 300, rot=ROTATE["degrees"] * progress(i / FPS, ROTATE["start"], ROTATE["duration"], ROTATE["bezier"]))
        yield frame


def color_frames():
    for i in range(FPS):
        p = progress(i / FPS, COLOR["start"], COLOR["duration"], COLOR["bezier"])
        colour = (1 - p) * np.array(COLOR["from_bgr"]) + p * np.array(COLOR["to_bgr"])
        frame = blank()
        cv2.rectangle(frame, (400, 270), (560, 318), tuple(int(round(v)) for v in colour), -1)
        cv2.putText(frame, "Buy now", (440, 302), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        yield frame


def fade_up_frames(sprites, starts, truth, centres, seconds):
    for i in range(int(seconds * FPS)):
        frame = blank()
        for (sprite, alpha), start, (cx, cy) in zip(sprites, starts, centres):
            p = progress(i / FPS, start, truth["duration"], truth["bezier"])
            if p > 0:
                composite(frame, sprite, alpha, cx, cy + truth["dy"] * (1 - p), opacity=p)
        yield frame


def stagger_frames():
    sprites = [card_sprite(w=300, h=96, seed=10 + k) for k in range(5)]
    centres = [(480, 60 + 104 * k + 48) for k in range(5)]
    return fade_up_frames(sprites, STAGGER["starts"], STAGGER, centres, 1.2)


def adjacent_frames():
    sprites = [card_sprite(w=200, h=110, seed=30 + k) for k in range(2)]
    centres = [(376, 300), (584, 300)]                  # 8 px apart
    return fade_up_frames(sprites, [ADJACENT["start"]] * 2, ADJACENT, centres, 1.0)


def exit_frames():
    sprite, alpha = card_sprite(seed=4)
    for i in range(FPS):
        p = progress(i / FPS, EXIT["start"], EXIT["duration"], EXIT["bezier"])
        frame = blank()
        if p < 1:
            composite(frame, sprite, alpha, 480, 300 + EXIT["dy"] * p, opacity=1 - p)
        yield frame


def static_page():
    """A text-heavy page with a soft gradient panel: the encoder has detail to be noisy about."""
    frame = blank()
    gradient = np.linspace(0, 1, 360)[None, :, None]
    frame[60:260, 520:880] = (np.array([200, 170, 120]) * (1 - gradient) + np.array([120, 90, 200]) * gradient).astype(np.uint8)
    for row in range(12):
        cv2.putText(frame, f"Line {row + 1}: the quick brown fox jumps over the lazy dog", (40, 320 + row * 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (40, 40, 40), 1, cv2.LINE_AA)
    cv2.putText(frame, "Settings", (40, 90), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (20, 20, 20), 2, cv2.LINE_AA)
    return frame


def caret_frames():
    page = static_page()
    caret_w, caret_h = CARET["size"]
    for i in range(int(CARET["seconds"] * FPS)):
        frame = page.copy()
        if int(i / FPS / CARET["period_s"]) % 2 == 0:
            frame[140:140 + caret_h, 300:300 + caret_w] = (30, 30, 30)
        yield frame


def spring_frames():
    sprite, alpha = card_sprite(seed=5)
    for i in range(int(1.2 * FPS)):
        x = (i / FPS - SPRING["start"]) / SPRING["duration"]
        frame = blank()
        composite(frame, sprite, alpha, 250 + SPRING["dx"] * spring_progress(x), 300)
        yield frame


def toast_sprite(seed):
    """A dark, fully opaque toast: icon, a bold and a light text line, a lighter rim."""
    w, h = TOAST_SIZE
    image = np.full((h, w, 3), (58, 52, 48), np.uint8)
    cv2.rectangle(image, (0, 0), (w - 1, h - 1), (110, 104, 100), 2)
    cv2.circle(image, (36, h // 2), 16, (90, 200, 120), -1, cv2.LINE_AA)
    cv2.putText(image, f"Saved draft {seed}", (64, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (250, 250, 250), 1, cv2.LINE_AA)
    cv2.rectangle(image, (64, 44), (64 + 150 + 20 * seed, 52), (170, 170, 170), -1)
    return image, np.full((h, w), 255, np.uint8)


def slide_position(truth, t):
    """Top-left of the toast at t: hidden -> rest for an entrance, rest -> hidden for an exit."""
    p = progress(t, truth["start"], truth["duration"], truth["bezier"])
    (rx, ry), (hx, hy) = truth["rest"], truth["hidden"]
    share = 1 - p if truth["origin"] == "end" else p
    return rx + (hx - rx) * share, ry + (hy - ry) * share


def edge_toast_frames(truth, seed):
    sprite, alpha = toast_sprite(seed)
    w, h = TOAST_SIZE
    for i in range(int(1.0 * FPS)):
        frame = blank()
        x, y = slide_position(truth, i / FPS)
        composite(frame, sprite, alpha, x + w / 2, y + h / 2)
        yield frame


def drawer_sprite(w, h):
    """A white side drawer: header band, title, filter rows with check boxes."""
    image = np.full((h, w, 3), 255, np.uint8)
    cv2.rectangle(image, (0, 0), (w - 1, 64), (240, 236, 233), -1)
    cv2.putText(image, "Filters", (24, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (30, 30, 30), 2, cv2.LINE_AA)
    for row in range(10):
        y = 96 + 44 * row
        cv2.rectangle(image, (24, y), (42, y + 18), (150, 140, 130), 2)
        cv2.rectangle(image, (56, y + 4), (56 + 90 + 13 * (row * 7 % 11), y + 12), (175, 175, 175), -1)
    return image, np.full((h, w), 255, np.uint8)


def shadowed(sprite, alpha, shadow):
    """The sprite over its box-shadow, as one larger sprite centred on the same point."""
    h, w = alpha.shape
    (ox, oy), sigma = shadow["offset"], shadow["blur"] / 2
    margin = int(np.ceil(3 * sigma)) + max(abs(ox), abs(oy))
    body = np.zeros((h + 2 * margin, w + 2 * margin), np.float32)
    body[margin:margin + h, margin:margin + w] = alpha / 255
    cast = cv2.GaussianBlur(np.roll(body, (oy, ox), axis=(0, 1)), (0, 0), sigma) * shadow["opacity"] * (1 - body)
    weight = body + cast
    colour = np.zeros((*body.shape, 3), np.float32)
    colour[margin:margin + h, margin:margin + w] = sprite
    colour = (colour * body[..., None] + np.float32(SHADOW_BGR) * cast[..., None]) / np.maximum(weight, 1e-6)[..., None]
    return np.clip(colour + 0.5, 0, 255).astype(np.uint8), np.clip(weight * 255 + 0.5, 0, 255).astype(np.uint8)


def shadow_slide_frames(key):
    truth = SHADOW_SLIDES[key]
    w, h = truth["size"]
    sprite, alpha = shadowed(*(toast_sprite(4) if truth["sprite"] == "toast" else drawer_sprite(w, h)), truth["shadow"])
    for i in range(int(1.0 * FPS)):
        frame = blank()
        x, y = slide_position(truth, i / FPS)
        composite(frame, sprite, alpha, x + w / 2, y + h / 2)
        yield frame


def sheet_sprite(w, h):
    """A white sheet: grab handle, title, list rows of avatars and two grey lines."""
    image = np.full((h, w, 3), 255, np.uint8)
    cv2.rectangle(image, (w // 2 - 24, 10), (w // 2 + 24, 15), (205, 205, 205), -1)
    cv2.putText(image, "Share to", (28, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (30, 30, 30), 2, cv2.LINE_AA)
    for row in range((h - 90) // 52):
        y = 84 + 52 * row
        cv2.circle(image, (48, y + 18), 16, (200 - 20 * row % 160, 120 + 30 * row % 120, 80 + 40 * row % 170), -1, cv2.LINE_AA)
        cv2.rectangle(image, (80, y + 8), (80 + 120 + 37 * row % (w // 2), y + 16), (90, 90, 90), -1)
        cv2.rectangle(image, (80, y + 24), (80 + 80 + 53 * row % (w // 3), y + 30), (185, 185, 185), -1)
    return image, np.full((h, w), 255, np.uint8)


def top_bar(frame):
    """A white app bar with a title and a hairline below it."""
    cv2.rectangle(frame, (0, 0), (frame.shape[1], TOP_BAR_H - 1), (255, 255, 255), -1)
    cv2.line(frame, (0, TOP_BAR_H), (frame.shape[1], TOP_BAR_H), (225, 222, 220), 1)
    cv2.putText(frame, "Photos", (24, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (30, 30, 30), 2, cv2.LINE_AA)


def sheet_frames(key):
    truth = SHEETS[key]
    w, h = truth["size"]
    sprite, alpha = sheet_sprite(w, h)
    for i in range(int(1.1 * FPS)):
        frame = blank()
        if truth["top_bar"]:
            top_bar(frame)
        x, y = slide_position(truth, i / FPS)
        composite(frame, sprite, alpha, x + w / 2, y + h / 2)
        yield frame


def badge_sprite(d):
    """A red disc with a centred white digit, drawn 4x and averaged down."""
    k, digit = 4, "3"
    image = np.zeros((d * k, d * k, 3), np.uint8)
    alpha = np.zeros((d * k, d * k), np.uint8)
    cv2.circle(image, (d * k // 2, d * k // 2), d * k // 2 - 1, (60, 60, 230), -1, cv2.LINE_AA)
    cv2.circle(alpha, (d * k // 2, d * k // 2), d * k // 2 - 1, 255, -1, cv2.LINE_AA)
    scale, thickness = d * k / 60, max(1, d * k // 14)
    (tw, th), _ = cv2.getTextSize(digit, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    cv2.putText(image, digit, ((d * k - tw) // 2, (d * k + th) // 2), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255),
                thickness, cv2.LINE_AA)
    return (cv2.resize(image, (d, d), interpolation=cv2.INTER_AREA),
            cv2.resize(alpha, (d, d), interpolation=cv2.INTER_AREA))


def chip_sprite(w, h):
    """A rounded blue chip with a white label."""
    image = np.zeros((h, w, 3), np.uint8)
    alpha = np.zeros((h, w), np.uint8)
    for target, colour in ((image, (200, 110, 40)), (alpha, 255)):
        cv2.rectangle(target, (h // 2, 0), (w - h // 2, h - 1), colour, -1)
        cv2.circle(target, (h // 2, h // 2), h // 2, colour, -1, cv2.LINE_AA)
        cv2.circle(target, (w - h // 2 - 1, h // 2), h // 2, colour, -1, cv2.LINE_AA)
    cv2.putText(image, "Copied", (26, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    return image, alpha


def nav_bar(frame):
    """A white nav bar with outline icons."""
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 63), (255, 255, 255), -1)
    cv2.line(frame, (0, 64), (frame.shape[1], 64), (225, 222, 220), 1)
    for x in NAV_ICONS_X:
        cv2.rectangle(frame, (x, 20), (x + 26, 46), (120, 110, 100), 2)


def pop_frames(key):
    truth = POPS[key]
    sprite, alpha = badge_sprite(truth["size"][0]) if truth["sprite"] == "badge" else chip_sprite(*truth["size"])
    for i in range(int(1.0 * FPS)):
        frame = blank()
        if truth["nav"]:
            nav_bar(frame)
        scale = truth["scale_from"] + (1 - truth["scale_from"]) * progress(i / FPS, truth["start"], truth["duration"],
                                                                           truth["bezier"])
        if scale > 1e-3:
            composite(frame, sprite, alpha, *truth["centre"], scale=scale)
        yield frame


def text_page(bar=True):
    """A page of grey text lines, two per row, under an optional white app bar."""
    frame = blank()
    if bar:
        cv2.rectangle(frame, (0, 0), (W, 51), (255, 255, 255), -1)
        cv2.line(frame, (0, 52), (W, 52), (226, 223, 221), 1)
        cv2.putText(frame, "Library", (22, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (32, 32, 32), 2, cv2.LINE_AA)
    for row in range(PAGE_ROWS):
        y = 76 + 37 * row
        cv2.rectangle(frame, (36, y), (36 + 250 + 43 * (row * 7 % 11), y + 8), (188, 186, 184), -1)
        cv2.rectangle(frame, (36, y + 15), (36 + 130 + 31 * (row * 5 % 9), y + 20), (212, 210, 208), -1)
    return frame


def shade_sprite(w, h):
    """A white notification shade: message rows in its upper part, a grab handle alone in its lower 76 px."""
    image = np.full((h, w, 3), 255, np.uint8)
    for row in range((h - 90) // 50):
        y = 14 + 50 * row
        cv2.rectangle(image, (22, y), (56, y + 34), (70 + 45 * row, 160 - 25 * row, 210 - 30 * row), -1)
        cv2.putText(image, f"Reminder {row + 1}", (72, y + 15), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (28, 28, 28), 1, cv2.LINE_AA)
        cv2.rectangle(image, (72, y + 23), (72 + 170 + 47 * row, y + 29), (178, 178, 178), -1)
    cv2.rectangle(image, (w // 2 - 22, h - 13), (w // 2 + 22, h - 8), (200, 200, 200), -1)
    return image, np.full((h, w), 255, np.uint8)


def page_slide_frames(truth, sprite, alpha, page, scrim=0.0, seconds=1.1):
    """A sheet sliding over a page (slide_position), the page dimmed by `scrim` x progress toward black."""
    w, h = truth["size"]
    for i in range(int(seconds * FPS)):
        t = i / FPS
        dim = 1 - scrim * progress(t, truth["start"], truth["duration"], truth["bezier"])
        frame = np.clip(page.astype(np.float32) * dim + 0.5, 0, 255).astype(np.uint8) if scrim else page.copy()
        x, y = slide_position(truth, t)
        composite(frame, sprite, alpha, x + w / 2, y + h / 2)
        yield frame


def text_sheet_frames():
    return page_slide_frames(TEXT_SHEET, *sheet_sprite(*TEXT_SHEET["size"]), text_page())


def shade_frames(key):
    truth = SHADES[key]
    return page_slide_frames(truth, *shade_sprite(*truth["size"]), text_page(bar=False))


def scrim_sheet_frames():
    return page_slide_frames(SCRIM_SHEET, *sheet_sprite(*SCRIM_SHEET["size"]), text_page(), scrim=SCRIM_SHEET["scrim"])


def exit_sheet_frames():
    return page_slide_frames(EXIT_SHEET, *sheet_sprite(*EXIT_SHEET["size"]), text_page())


def pop_scale(truth, t):
    return truth["scale_from"] + (truth.get("scale_to", 1.0) - truth["scale_from"]) * progress(t, truth["start"],
                                                                                              truth["duration"], truth["bezier"])


def anchored_pop_frames(key):
    """A badge popping on (or off) content it covers: an avatar's corner, or a nav icon's corner."""
    truth = ANCHORED_POPS[key]
    page = text_page(bar=False)
    if truth["anchor"] == "nav":
        nav_bar(page)
    else:
        cv2.circle(page, AVATAR["centre"], AVATAR["radius"], AVATAR["bgr"], -1, cv2.LINE_AA)
    sprite, alpha = badge_sprite(truth["size"])
    for i in range(int(1.0 * FPS)):
        frame, scale = page.copy(), pop_scale(truth, i / FPS)
        if scale > 1e-3:
            composite(frame, sprite, alpha, *truth["centre"], scale=scale)
        yield frame


def line_pair_frames():
    """A chip sliding right and a badge popping 14 px beside its rest position, both over one text line."""
    chip, badge = LINE_PAIR["chip"], LINE_PAIR["badge"]
    page = text_page()
    chip_sprite_, chip_alpha = chip_sprite(*chip["size"])
    badge_sprite_, badge_alpha = badge_sprite(badge["size"])
    for i in range(int(1.0 * FPS)):
        t = i / FPS
        frame = page.copy()
        shift = chip["dx"] * (1 - progress(t, chip["start"], chip["duration"], chip["bezier"]))
        composite(frame, chip_sprite_, chip_alpha, chip["centre"][0] - shift, chip["centre"][1])
        scale = pop_scale(badge, t)
        if scale > 1e-3:
            composite(frame, badge_sprite_, badge_alpha, *badge["centre"], scale=scale)
        yield frame


def card_dot_frames():
    """A dot popping on a white card, and later a grey label fading in far away: the work crop spans both events and
    cuts through the card."""
    truth, far = CARD_DOT, CARD_DOT["far"]
    page = text_page()
    x, y, w, h = truth["card"]
    cv2.rectangle(page, (x, y), (x + w, y + h), (255, 255, 255), -1)
    cv2.rectangle(page, (x, y), (x + w, y + h), (221, 217, 213), 1)
    cv2.putText(page, "Status", (x + 18, y + 34), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (40, 40, 40), 1, cv2.LINE_AA)
    sprite, alpha = badge_sprite(truth["size"])
    fx, fy, fw, fh = far["box"]
    for i in range(int(1.2 * FPS)):
        t = i / FPS
        frame = page.copy()
        scale = pop_scale(truth, t)
        if scale > 1e-3:
            composite(frame, sprite, alpha, *truth["centre"], scale=scale)
        shown = progress(t, far["start"], far["duration"], LINEAR)
        region = frame[fy:fy + fh, fx:fx + fw].astype(np.float32)
        frame[fy:fy + fh, fx:fx + fw] = np.clip(region * (1 - shown) + np.float32((150, 146, 142)) * shown + 0.5, 0, 255)
        yield frame


def tooltip_sprite(w, h):
    """A dark rounded tooltip with a white label."""
    image = np.full((h, w, 3), (46, 41, 38), np.uint8)
    alpha = np.zeros((h, w), np.uint8)
    radius = 6
    cv2.rectangle(alpha, (radius, 0), (w - 1 - radius, h - 1), 255, -1)
    cv2.rectangle(alpha, (0, radius), (w - 1, h - 1 - radius), 255, -1)
    for cx, cy in ((radius, radius), (w - 1 - radius, radius), (radius, h - 1 - radius), (w - 1 - radius, h - 1 - radius)):
        cv2.circle(alpha, (cx, cy), radius, 255, -1, cv2.LINE_AA)
    cv2.putText(image, "Pin to top", (16, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (250, 250, 250), 1, cv2.LINE_AA)
    return image, alpha


def tooltip_frames():
    truth = TOOLTIP
    page = text_page()
    sprite, alpha = tooltip_sprite(*truth["size"])
    for i in range(int(1.0 * FPS)):
        p = progress(i / FPS, truth["start"], truth["duration"], truth["bezier"])
        frame = page.copy()
        if p > 0:
            composite(frame, sprite, alpha, *truth["centre"], scale=truth["scale_from"] + (1 - truth["scale_from"]) * p,
                      opacity=p)
        yield frame


def draw_iso_button(frame, colour):
    """ISO_BUTTON's rounded button in `colour` (BGR, float) with its white label."""
    x, y, w, h = ISO_BUTTON["box"]
    radius, fill = ISO_BUTTON["radius"], tuple(int(round(v)) for v in colour)
    cv2.rectangle(frame, (x + radius, y), (x + w - 1 - radius, y + h - 1), fill, -1, cv2.LINE_AA)
    cv2.rectangle(frame, (x, y + radius), (x + w - 1, y + h - 1 - radius), fill, -1, cv2.LINE_AA)
    for cx, cy in ((x + radius, y + radius), (x + w - 1 - radius, y + radius), (x + radius, y + h - 1 - radius),
                   (x + w - 1 - radius, y + h - 1 - radius)):
        cv2.circle(frame, (cx, cy), radius, fill, -1, cv2.LINE_AA)
    cv2.putText(frame, "Continue", (x + 52, y + 39), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)


def iso_button_frames(with_badge):
    """The button's colour change, then (with_badge) the badge popping beside it."""
    truth = ISO_BUTTON
    sprite, alpha = badge_sprite(ISO_BADGE["size"])
    for i in range(int((1.7 if with_badge else 1.0) * FPS)):
        t = i / FPS
        p = progress(t, truth["start"], truth["duration"], truth["bezier"])
        frame = blank()
        draw_iso_button(frame, (1 - p) * np.array(truth["from_bgr"], float) + p * np.array(truth["to_bgr"], float))
        scale = pop_scale(ISO_BADGE, t) if with_badge else 0.0
        if scale > 1e-3:
            composite(frame, sprite, alpha, *ISO_BADGE["centre"], scale=scale)
        yield frame


def rounded_mask(shape, box, radius):
    """The pixels (bool) of a filled rounded rectangle (x, y, w, h, float) in an image of `shape`."""
    x, y, w, h = box
    mask = np.zeros(shape, np.uint8)
    cv2.rectangle(mask, (int(x + radius), int(y)), (int(x + w - radius) - 1, int(y + h) - 1), 1, -1)
    cv2.rectangle(mask, (int(x), int(y + radius)), (int(x + w) - 1, int(y + h - radius) - 1), 1, -1)
    for cx, cy in ((x + radius, y + radius), (x + w - radius, y + radius), (x + radius, y + h - radius),
                   (x + w - radius, y + h - radius)):
        cv2.circle(mask, (int(round(cx)), int(round(cy))), int(round(radius)), 1, -1)
    return mask.astype(bool)


def colour_button_frames(truth):
    """One button of BORDERED_BUTTON, GHOST_BUTTON or DARK_BUTTON changing colour in place, drawn BUTTON_SUPERSAMPLE x
    and area-averaged: its fill (inside the static border, when it has one) and its label (from label_from_bgr to
    white, or white throughout)."""
    ss, (x, y, w, h) = BUTTON_SUPERSAMPLE, truth["box"]
    border_px, border_bgr = truth["border"] or (0, None)
    x0, y0 = int(np.floor(x)) - 2, int(np.floor(y)) - 2
    pw, ph = int(np.ceil(w)) + 5, int(np.ceil(h)) + 5
    shape, ox, oy = (ph * ss, pw * ss), (x - x0) * ss, (y - y0) * ss
    body = rounded_mask(shape, (ox, oy, w * ss, h * ss), truth["radius"] * ss)
    inset = border_px * ss
    fill = rounded_mask(shape, (ox + inset, oy + inset, w * ss - 2 * inset, h * ss - 2 * inset),
                        (truth["radius"] - border_px) * ss)
    text = np.zeros(shape, np.uint8)
    (tw, th), _ = cv2.getTextSize(truth["label"], cv2.FONT_HERSHEY_SIMPLEX, 0.7 * ss, 2 * ss)
    cv2.putText(text, truth["label"], (int(ox + (w * ss - tw) / 2), int(oy + (h * ss + th) / 2)), cv2.FONT_HERSHEY_SIMPLEX,
                0.7 * ss, 1, 2 * ss)
    label = text.astype(bool) & fill
    white = np.array((255, 255, 255), float)
    for i in range(int(1.0 * FPS)):
        p = progress(i / FPS, truth["start"], truth["duration"], truth["bezier"])
        colour = np.empty((*shape, 3), np.float32)
        colour[:] = BG
        if border_bgr is not None:
            colour[body] = border_bgr
        colour[fill] = (1 - p) * np.array(truth["from_bgr"], float) + p * np.array(truth["to_bgr"], float)
        colour[label] = white if truth["label_from_bgr"] is None else (1 - p) * np.array(truth["label_from_bgr"], float) + p * white
        frame = blank()
        frame[y0:y0 + ph, x0:x0 + pw] = np.clip(cv2.resize(colour, (pw, ph), interpolation=cv2.INTER_AREA) + 0.5, 0, 255)
        yield frame


def panel_toast_frames():
    """The panel is the clipping container: the toast is composited into the panel's own pixels only."""
    sprite, alpha = toast_sprite(7)
    w, h = TOAST_SIZE
    px, py, pw, ph = PANEL["box"]
    for i in range(int(1.0 * FPS)):
        frame = blank()
        panel = frame[py:py + ph, px:px + pw]
        panel[:] = PANEL["bgr"]
        x, y = slide_position(PANEL_TOAST, i / FPS)
        composite(panel, sprite, alpha, x - px + w / 2, y - py + h / 2)
        yield frame


def row_sprite(index, w, h, dpr):
    """A white list row at DPR 2: avatar, name and a grey detail line, 1 CSS px border."""
    image = np.full((h, w, 3), 255, np.uint8)
    cv2.rectangle(image, (0, 0), (w - 1, h - 1), (214, 210, 206), dpr)
    cv2.circle(image, (h // 2, h // 2), 14 * dpr, (200 - 25 * index, 120 + 15 * index, 90 + 20 * index), -1, cv2.LINE_AA)
    cv2.putText(image, f"Row {index + 1} - Invoice {1040 + index}", (h + 8 * dpr, 19 * dpr), cv2.FONT_HERSHEY_SIMPLEX,
                0.45 * dpr, (40, 40, 40), dpr, cv2.LINE_AA)
    cv2.rectangle(image, (h + 8 * dpr, 28 * dpr), (h + (120 + 30 * index) * dpr, 33 * dpr), (190, 190, 190), -1)
    return image, np.full((h, w), 255, np.uint8)


def row_frames():
    dpr, fps = ROWS["dpr"], ROWS["fps"]
    x, y, w, h = (v * dpr for v in ROWS["box_css"])
    sprites = [row_sprite(i, w, h, dpr) for i in range(ROWS["count"])]
    for k in range(int(ROWS["seconds"] * fps)):
        frame = blank(ROWS["size"])
        for i, (sprite, alpha) in enumerate(sprites):
            p = progress(k / fps, ROWS["start"] + i * ROWS["step"], ROWS["duration"], ROWS["bezier"])
            if p > 0:
                cx = x + w / 2 + ROWS["dx_css"] * dpr * (1 - p)
                composite(frame, sprite, alpha, cx, y + i * ROWS["pitch_css"] * dpr + h / 2, opacity=p)
        yield frame


def flat_row_sprite(index):
    """One list row with no background of its own: avatar, name, detail line, badge, separator along its bottom."""
    dpr, w, h = FLAT_LIST["dpr"], FLAT_LIST["width_css"] * FLAT_LIST["dpr"], FLAT_LIST["pitch_css"] * FLAT_LIST["dpr"]
    image = np.full((h, w, 3), BG, np.uint8)
    cv2.circle(image, (26 * dpr, 28 * dpr), 14 * dpr, (60 + 45 * index, 150 - 20 * index, 220 - 30 * index), -1, cv2.LINE_AA)
    cv2.putText(image, f"Account {index + 1}", (52 * dpr, 24 * dpr), cv2.FONT_HERSHEY_SIMPLEX, 0.45 * dpr, (40, 40, 40), dpr,
                cv2.LINE_AA)
    cv2.rectangle(image, (52 * dpr, 34 * dpr), ((152 + 22 * index) * dpr, 38 * dpr), (185, 185, 185), -1)
    cv2.rectangle(image, (w - 64 * dpr, 18 * dpr), (w - 16 * dpr, 36 * dpr), (214, 242, 225), -1)
    cv2.putText(image, "Paid", (w - 58 * dpr, 31 * dpr), cv2.FONT_HERSHEY_SIMPLEX, 0.32 * dpr, (60, 120, 30), 1, cv2.LINE_AA)
    image[h - dpr:h] = (222, 219, 217)
    drawn = (np.abs(image.astype(np.int16) - np.int16(BG)).max(axis=2) > 0).astype(np.uint8)
    return image, cv2.dilate(drawn, np.ones((3, 3), np.uint8)) * 255


def flat_list_frames():
    dpr, truth = FLAT_LIST["dpr"], FLAT_LIST
    w, h = truth["width_css"] * dpr, truth["pitch_css"] * dpr
    sprites = [flat_row_sprite(i) for i in range(truth["count"])]
    for k in range(int(truth["seconds"] * FPS)):
        frame = blank()
        for i, (sprite, alpha) in enumerate(sprites):
            p = progress(k / FPS, truth["start"] + i * truth["step"], truth["duration"], truth["bezier"])
            if p > 0:
                cy = (truth["top_css"] + i * truth["pitch_css"] + truth["dy_css"] * (1 - p)) * dpr + h / 2
                composite(frame, sprite, alpha, truth["left_css"] * dpr + w / 2, cy, opacity=p)
        yield frame


def carousel_card(index):
    w, h = CAROUSEL["card"]
    image = np.full((h, w, 3), (250, 248, 246), np.uint8)
    cv2.rectangle(image, (0, 0), (w - 1, h - 1), (200, 196, 192), 2)
    cv2.rectangle(image, (12, 12), (w - 13, 120), (60 + 37 * index % 180, 110 + 53 * index % 130, 200 - 29 * index % 150), -1)
    cv2.putText(image, f"Slide {index + 1}", (16, 160), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (40, 40, 40), 2, cv2.LINE_AA)
    cv2.rectangle(image, (16, 180), (16 + 60 + 11 * index, 190), (170, 170, 170), -1)
    return image


def carousel_shift(t):
    """Strip offset in card pitches at t: one pitch per finished transition plus the running one."""
    shift = 0.0
    for k in range(CAROUSEL["count"]):
        shift += progress(t, CAROUSEL["first"] + k * CAROUSEL["period"], CAROUSEL["duration"], CAROUSEL["bezier"])
    return shift


def carousel_frames():
    """Cards are drawn at sub-pixel strip offsets (warpAffine) into the panel, which clips them."""
    px, py, pw, ph = CAROUSEL["panel"]
    w, h = CAROUSEL["card"]
    pitch, cards = CAROUSEL["pitch"], [carousel_card(i) for i in range(8)]
    rng = np.random.default_rng(CAROUSEL["seed"])
    seconds = CAROUSEL["first"] + CAROUSEL["count"] * CAROUSEL["period"] + 0.2
    for i in range(int(seconds * FPS)):
        offset = carousel_shift(i / FPS) * pitch
        strip = np.full((ph, pw + 2 * pitch, 3), (236, 232, 226), np.float32)
        first = int(offset // pitch)
        for slot in range(len(range(0, pw + 2 * pitch, pitch))):
            x = 20 + slot * pitch
            if x + w <= strip.shape[1]:
                strip[20:20 + h, x:x + w] = cards[(first + slot) % len(cards)]
        moved = cv2.warpAffine(strip, np.float32([[1, 0, -(offset - first * pitch)], [0, 1, 0]]), (pw, ph),
                               flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        frame = blank().astype(np.float32)
        frame[py:py + ph, px:px + pw] = moved
        frame += rng.normal(0, CAROUSEL["noise"], frame.shape)
        yield np.clip(frame + 0.5, 0, 255).astype(np.uint8)


def modal_sprite():
    """A white dialog: title, two text lines, two buttons."""
    image = np.full((220, 360, 3), 255, np.uint8)
    cv2.rectangle(image, (0, 0), (359, 219), (205, 200, 196), 2)
    cv2.putText(image, "Discard changes?", (24, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (30, 30, 30), 2, cv2.LINE_AA)
    for row, length in enumerate((290, 210)):
        cv2.rectangle(image, (24, 72 + 22 * row), (24 + length, 82 + 22 * row), (175, 175, 175), -1)
    cv2.rectangle(image, (160, 160), (250, 196), (225, 225, 225), -1)
    cv2.rectangle(image, (262, 160), (340, 196), (200, 90, 40), -1)
    return image, np.full((220, 360), 255, np.uint8)


def modal_exit_frames(truth):
    sprite, alpha = modal_sprite()
    for i in range(int(0.8 * FPS)):
        p = progress(i / FPS, truth["start"], truth["duration"], truth["bezier"])
        frame = blank()
        if p < 1:
            composite(frame, sprite, alpha, 480, 300, scale=1 - (1 - MODAL_SCALE_TO) * p, opacity=1 - p)
        yield frame


def split_card_frames():
    sprite, alpha = card_sprite(w=260, h=120, seed=8)
    move, fade = SPLIT_CARD["move"], SPLIT_CARD["fade"]
    for i in range(int(1.0 * FPS)):
        t = i / FPS
        shown = progress(t, SPLIT_CARD["start"], fade["duration"], fade["bezier"])
        frame = blank()
        if shown > 0:
            lift = move["dy"] * (1 - progress(t, SPLIT_CARD["start"], move["duration"], move["bezier"]))
            composite(frame, sprite, alpha, 480, 300 + lift, opacity=shown)
        yield frame


def ui_frames():
    """D3 make_ui_clip.py: title rectangle fades in; three cards slide 60 px up while fading in, 100 ms apart."""
    width, height = UI["size"]
    title = UI["title"]
    for k in range(int(UI["seconds"] * FPS)):
        t = k / FPS
        image = np.full((height, width, 3), 245, np.float32)
        a = progress(t, title["start"], title["duration"], title["bezier"])
        x, y, w, h = title["box"]
        image[y:y + h, x:x + w] = image[y:y + h, x:x + w] * (1 - a) + np.array((40, 40, 40), np.float32) * a
        for card in UI["cards"]:
            e = progress(t, card["start"], card["duration"], card["bezier"])
            x, y, w, h = card["box"]
            x0, y0, x1, y1 = x - 2, y - 2, x + w + 2, min(height, y + h + card["dy"] + 2)     # the card's travel only
            sprite = np.zeros((y1 - y0, x1 - x0, 4), np.float32)
            sprite[y - y0:y - y0 + h, x - x0:x - x0 + w, :3] = card["color"]
            sprite[y - y0:y - y0 + h, x - x0:x - x0 + w, 3] = 1
            moved = cv2.warpAffine(sprite, np.float32([[1, 0, 0], [0, 1, card["dy"] * (1 - e)]]), (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR)
            weight = moved[:, :, 3:4] * e
            image[y0:y1, x0:x1] = image[y0:y1, x0:x1] * (1 - weight) + moved[:, :, :3] * weight
        yield np.clip(image + 0.5, 0, 255).astype(np.uint8)


def build_translate(path):
    fc.encode_bgr(translate_frames(FPS, TRANSLATE), path, (W, H), FPS)


def build_translate_snap(path):
    fc.encode_bgr(translate_frames(FPS, TRANSLATE_SNAP, snap=True), path, (W, H), FPS)


def build_translate_30fps(path):
    fc.encode_bgr(translate_frames(30, TRANSLATE), path, (W, H), 30)


def build_frozen(path):
    fc.encode_bgr(translate_frames(FPS, TRANSLATE, frozen=FROZEN_INDICES), path, (W, H), FPS)


def build_scalefade(path):
    fc.encode_bgr(scalefade_frames(), path, (W, H), FPS)


def build_rotate(path):
    fc.encode_bgr(rotate_frames(), path, (W, H), FPS)


def build_color(path):
    fc.encode_bgr(color_frames(), path, (W, H), FPS)


def build_stagger(path):
    fc.encode_bgr(stagger_frames(), path, (W, H), FPS)


def build_adjacent(path):
    fc.encode_bgr(adjacent_frames(), path, (W, H), FPS)


def build_exit(path):
    fc.encode_bgr(exit_frames(), path, (W, H), FPS)


def build_caret(path):
    fc.encode_bgr(caret_frames(), path, (W, H), FPS)


def build_spring(path):
    fc.encode_bgr(spring_frames(), path, (W, H), FPS)


def build_ui(path):
    fc.encode_bgr(ui_frames(), path, UI["size"], FPS)


def build_edge_toast(key):
    return lambda path: fc.encode_bgr(edge_toast_frames(EDGE_TOASTS[key], len(key)), path, (W, H), FPS)


def build_shadow_slide(key):
    return lambda path: fc.encode_bgr(shadow_slide_frames(key), path, (W, H), FPS)


def build_sheet(key):
    return lambda path: fc.encode_bgr(sheet_frames(key), path, (W, H), FPS)


def build_pop(key):
    return lambda path: fc.encode_bgr(pop_frames(key), path, (W, H), FPS)


def build_text_sheet(path):
    fc.encode_bgr(text_sheet_frames(), path, (W, H), FPS)


def build_shade(key):
    return lambda path: fc.encode_bgr(shade_frames(key), path, (W, H), FPS)


def build_scrim_sheet(path):
    fc.encode_bgr(scrim_sheet_frames(), path, (W, H), FPS)


def build_exit_sheet(path):
    fc.encode_bgr(exit_sheet_frames(), path, (W, H), FPS)


def build_anchored_pop(key):
    return lambda path: fc.encode_bgr(anchored_pop_frames(key), path, (W, H), FPS)


def build_line_pair(path):
    fc.encode_bgr(line_pair_frames(), path, (W, H), FPS)


def build_card_dot(path):
    fc.encode_bgr(card_dot_frames(), path, (W, H), FPS)


def build_tooltip(path):
    fc.encode_bgr(tooltip_frames(), path, (W, H), FPS)


def build_iso_button(with_badge):
    return lambda path: fc.encode_bgr(iso_button_frames(with_badge), path, (W, H), FPS)


def build_colour_button(truth):
    return lambda path: fc.encode_bgr(colour_button_frames(truth), path, (W, H), FPS)


def build_panel_toast(path):
    fc.encode_bgr(panel_toast_frames(), path, (W, H), FPS)


def build_rows(path):
    fc.encode_bgr(row_frames(), path, ROWS["size"], ROWS["fps"])


def build_flat_list(path):
    fc.encode_bgr(flat_list_frames(), path, (W, H), FPS)


def build_carousel(path):
    fc.encode_bgr(carousel_frames(), path, (W, H), FPS)


def build_modal_exit(name):
    return lambda path: fc.encode_bgr(modal_exit_frames(MODAL_EXITS[name]), path, (W, H), FPS)


def build_split_card(path):
    fc.encode_bgr(split_card_frames(), path, (W, H), FPS)


def build_static_twice(first_path):
    """Second generation of a static page, re-encoded with a keyframe every 30 frames (encoder noise only)."""
    return lambda path: fc.ffmpeg("-i", first_path, "-c:v", "libx264", "-crf", fc.CRF, "-g", "30", "-pix_fmt", "yuv420p", path)


def build_static(path):
    fc.encode_bgr((static_page() for _ in range(2 * FPS)), path, (W, H), FPS)


def build_vfr(cfr_path):
    return lambda path: fc.make_vfr(cfr_path, path)
