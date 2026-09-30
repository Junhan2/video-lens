"""Tracking and channels (spec 7.8.4): ECC affine tracker with NCC reseeding and opacity gain, the colour blend
and change-energy channels, and the phase-correlation measurement used for scroll and carousel motion.

All coordinates are work-scale pixels of the cropped analysis region.
"""
from dataclasses import dataclass

import cv2
import numpy as np

from . import pixels
from .easing import Channel, nelder_mead

ECC_CRITERIA = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6)
# one call on a 1280x720 template took 1.9 s; card-sized templates keep the precise criteria (a looser eps turned a
# clipped toast's translate into translate+scale)
ECC_LARGE_CRITERIA = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-5)
ECC_LARGE_PX = 250_000
TEMPLATE_PAD_PX = 6             # flat fills have no inner gradient without the background border
THIN_PX = 6                     # thinner than this along an axis, an element has no measurable scale along it
ACCEPT_CC = 0.8
RESEED_CC = 0.9
NCC_MIN_PEAK = 0.5
SEARCH_BOX_FRAC, SEARCH_FRAME_FRAC = 0.5, 0.05     # local window: box +- (0.5 x size + 5 % of the frame)
GAIN_SIGMA = 2.0                # low-pass before the gain: cuts its resampling bias from 3-4 % to about 1 %
LOST_GAIN = 0.05                # below this the element is not visible; geometry stops in that direction
# Look-alike siblings (list rows 116 px apart, cc 0.9 by layout alone) took over a fading row's walk. Walking away from
# the rest frame an element only fades, so a match brighter than the last accepted frame by more than GAIN_RISE_MAX
# is not it (14 % gave way to 37 %); nor is one off the constant-velocity prediction by more than JUMP_SHARE of the
# template along an axis (a sibling as faint as the row itself, 6 % against 5 %).
GAIN_RISE_MAX = 0.15
JUMP_SHARE = 0.5
ABSENT_STD_RATIO = 0.35
TYPE_MIN_GAIN = 0.5             # types are decided where the element is at least half visible: faint ECC is noisy
TYPE_MIN_SCALE = 0.5            # ... and at least half its rest size: tiny ECC is noisy too
TRANSLATE_MIN_PX = 1.0
SCALE_MIN = 0.01
ROTATE_MIN_DEG = 0.5
# share of a template's gradient energy that a turn about its centre moves: a disc's edge runs along any turn, so ECC
# cannot hold its angle (badges 0.012 to 0.015, a plain disc 0.004) and codec noise read as 4.5 deg on one; cards,
# toasts and dialogs 0.49 to 0.65, a plain square 0.22
ANGLE_HOLD_MIN = 0.05
ORIGIN_SLACK = 0.1              # a scale origin this close to the box centre (x size) is its centre
COLOR_RESIDUAL_P90 = 0.25
SCROLL_MIN_RESPONSE = 0.3
SCROLL_MOVING_PX = 0.5
CLIP_ACTIVE_SHARE = 0.3         # a column (row) of the element's band belongs to its swept path above this changed share
CLIP_GAP_PX = 2                 # ... with gaps up to this wide
CLIP_MIN_CUT_SHARE = 0.25       # clipped: some tracked frame hides at least this share of the element behind the line
CLIP_OPAQUE_GAIN = 0.85         # ... while its visible part keeps the rest frame's contrast (a fade-in never does)
CLIP_VISIBLE_MIN_PX = 4         # element px on the visible side needed to read its position
# ... and share of the template's contrast they must hold: a toast's shadow fringe alone (0.03) matched 7 px off
CLIP_VISIBLE_CONTRAST = 0.1
CLIP_SEARCH_PX = 8              # search along the motion axis: this plus half the last step
CLIP_SEARCH_ACROSS_PX = 4
CLIP_NCC_MIN = 0.6
CLIP_MIN_MOVE_PX = 2.0
COARSE_MIN_RESPONSE = 0.05     # a phase-correlation peak below this is no shift (flat or changed content)
COARSE_FRAMES = 3               # frames from rest searched for the side an element leaves toward
HIDDEN_STRIP_PX = 3             # px inside the line that stay as in the hidden rest frame while the element is out of view
HIDDEN_CHANGED_SHARE = 0.02
COMPOSITE_REACH = 0.25          # the composite model covers this share of the element's size around its box (overshoot to 1.5)
# an element's pixel differs from what lies behind it by more than codec noise at rest (which reached 11), and is wholly
# the element's from COMPOSITE_OPAQUE on; between the two it is a blend (anti-aliased edge)
COMPOSITE_FOOTPRINT, COMPOSITE_OPAQUE = 16, 48
COMPOSITE_FRINGE_PX = 2
SHRINK_SIGMA = 0.5              # x (1 / scale - 1): the blur that stands in for area averaging when a sprite shrinks
COMPOSITE_SCALES = (0.02, 3.0)
COMPOSITE_STEPS = (1.0, 1.0, 0.05, 0.05)    # first simplex steps: px, px, ln scale, ln scale
COMPOSITE_ITERS = 200


@dataclass
class Track:
    """Per window frame: centre, scale, angle, shear, ECC cc, opacity gain, std ratio and whether it was accepted."""
    ref: int
    box: tuple              # padded template box (x, y, w, h) at the reference frame
    cx: np.ndarray
    cy: np.ndarray
    sx: np.ndarray
    sy: np.ndarray
    rot: np.ndarray
    shear: np.ndarray
    cc: np.ndarray
    gain: np.ndarray
    std_ratio: np.ndarray
    good: np.ndarray
    size: tuple = (0, 0)            # the element's own (w, h) at the reference rest frame (the box is padded)
    angle_hold: float = 1.0         # template_angle_hold of the tracked template

    def visible_tracked_frac(self):
        """Accepted share of the frames where the element is visible: low for content that is not one rigid thing."""
        visible = self.gain >= LOST_GAIN
        return float(self.good[visible].mean()) if visible.any() else 0.0


def decompose(warp, w, h):
    (a, b, tx), (c, d, ty) = warp
    cx, cy = w / 2, h / 2
    sx, sy = float(np.hypot(a, c)), float(np.hypot(b, d))
    return (float(a * cx + b * cy + tx), float(c * cx + d * cy + ty), sx, sy,
            float(np.degrees(np.arctan2(c, a))), float((a * b + c * d) / (sx * sy + 1e-9)))


def ecc(template, image, warp, keep=None):
    criteria = ECC_LARGE_CRITERIA if template.size > ECC_LARGE_PX else ECC_CRITERIA
    try:
        cc, found = cv2.findTransformECC(template, image, warp.copy(), cv2.MOTION_AFFINE, criteria, keep, 1)
        return float(cc), found
    except cv2.error:
        return -1.0, warp


def reseed(template, image, warp, search):
    """Integer translation by NCC inside the local search box; the 2x2 part is reset (a global search jumps to siblings)."""
    x, y, w, h = search
    th, tw = template.shape
    x0, y0 = max(0, x - tw), max(0, y - th)
    x1, y1 = min(image.shape[1], x + w + tw), min(image.shape[0], y + h + th)
    region = image[y0:y1, x0:x1]
    if region.shape[0] < th or region.shape[1] < tw:
        return warp
    _, _, _, (mx, my) = cv2.minMaxLoc(cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED))
    seeded = warp.copy()
    seeded[:, :2] = np.eye(2)
    seeded[0, 2], seeded[1, 2] = x0 + mx, y0 + my
    return seeded


def padded_box(box, size):
    width, height = size
    x0, y0 = max(0, box[0] - TEMPLATE_PAD_PX), max(0, box[1] - TEMPLATE_PAD_PX)
    x1, y1 = min(width, box[0] + box[2] + TEMPLATE_PAD_PX), min(height, box[1] + box[3] + TEMPLATE_PAD_PX)
    return x0, y0, x1 - x0, y1 - y0


def ncc_warp(template, image, last, margin):
    """Contrast-invariant NCC search near the last position; works at low opacity where ECC fails."""
    th, tw = template.shape
    px, py = int(last[0, 2]), int(last[1, 2])
    x0, y0 = max(0, px - margin[0] // 2), max(0, py - margin[1] // 2)
    x1, y1 = min(image.shape[1], px + tw + margin[0] // 2), min(image.shape[0], py + th + margin[1] // 2)
    if x1 - x0 <= tw or y1 - y0 <= th:
        return None
    _, peak, _, (bx, by) = cv2.minMaxLoc(cv2.matchTemplate(image[y0:y1, x0:x1], template, cv2.TM_CCOEFF_NORMED))
    if peak < NCC_MIN_PEAK:
        return None
    found = last.copy()
    found[0, 2], found[1, 2] = x0 + bx, y0 + by
    return found


def template_angle_hold(template):
    """Share of the template's gradient energy, weighted by the squared distance from its centre, that a turn about the
    centre moves: 1 when every gradient runs across the turn, 0 for a disc, whose edge runs along it."""
    gx = cv2.Sobel(template, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(template, cv2.CV_32F, 0, 1, ksize=3)
    height, width = template.shape
    y, x = np.mgrid[0:height, 0:width]
    x, y = x - (width - 1) / 2, y - (height - 1) / 2
    turned = float(((gy * x - gx * y) ** 2).sum())
    return turned / (float(((gx * gx + gy * gy) * (x * x + y * y)).sum()) + 1e-9)


@dataclass(frozen=True)
class TemplateMask:
    """Image masks for tracking one element: `keep` (uint8, 1 = match there) and `hold` (bool), the left-out pixels
    whose template values are held at the background. The element's own pixels over content it sits on are left out
    of the match but keep their values: they are what scales or moves."""
    keep: np.ndarray
    hold: np.ndarray


def with_background(template, hold, surround):
    """The template with its `hold` pixels set to the background, the median of the `surround` pixels (the pad's kept
    ones): ECC masks only the image side, so neighbour pixels left in the template still pulled a scaled warp back
    where they landed off the mask."""
    background = template[surround]
    filled = template.copy()
    filled[hold] = float(np.median(background)) if background.size else float(np.median(template))
    return filled


def predicted_warp(history):
    """Constant-velocity extrapolation of the last two accepted warps (the last one alone at the start)."""
    return history[-1] if len(history) < 2 else history[-1] + (history[-1] - history[-2])


def without_thin_stretch(warp, size, thin):
    """The warp with no scale or shear along the axes the element is thin along, its centre kept. A thin element's
    thickness is below the blur of sub-pixel positioning: ECC stretched a fading 2 px separator 1.3 to 2 x, which read
    as scale and sampled its opacity gain through the stretch."""
    if not any(thin):
        return warp
    centre = np.array(size, np.float32) / 2
    fixed = warp.copy()
    target = warp[:, :2] @ centre + warp[:, 2]
    for axis, is_thin in enumerate(thin):
        if is_thin:
            fixed[:, axis] = np.eye(2, dtype=np.float32)[axis]
    fixed[:, 2] = target - fixed[:, :2] @ centre
    return fixed


class Tracker:
    """ECC walk outward from the reference rest frame, in both directions (spec 7.8.4). A TemplateMask leaves
    neighbours and the content the element sits on out of the match and the gain; the template holds background where
    they are not the element."""

    def __init__(self, greys, box, ref, mask=None):
        height, width = greys[0].shape
        self.greys = greys
        self.ref = ref
        self.keep = None if mask is None else mask.keep
        self.box = padded_box(box, (width, height))
        self.size = tuple(box[2:])
        self.thin = tuple(side < THIN_PX for side in self.size)
        x, y, w, h = self.box
        self.margin = (int(SEARCH_BOX_FRAC * w + SEARCH_FRAME_FRAC * width), int(SEARCH_BOX_FRAC * h + SEARCH_FRAME_FRAC * height))
        mx, my = self.margin
        self.search = (max(0, x - mx), max(0, y - my), min(width, x + w + mx) - max(0, x - mx), min(height, y + h + my) - max(0, y - my))
        self.template = greys[ref][y:y + h, x:x + w].copy()
        if mask is not None:
            pad = np.ones((h, w), bool)
            pad[box[1] - y:box[1] - y + box[3], box[0] - x:box[0] - x + box[2]] = False
            self.template = with_background(self.template, mask.hold[y:y + h, x:x + w], pad & (mask.keep[y:y + h, x:x + w] > 0))
        self.angle_hold = template_angle_hold(self.template)
        blurred = cv2.GaussianBlur(self.template, (0, 0), GAIN_SIGMA)
        self.template_blurred = blurred
        self.template_centred = blurred - blurred.mean()
        self.template_energy = float((self.template_centred ** 2).sum()) + 1e-6
        self.template_std = float(self.template.std()) + 1e-6

    def sample(self, image, warp):
        """(gain, std ratio) of the patch under the warp: gain = cov(patch, template) / var(template), both low-passed."""
        if self.keep is not None:
            return masked_sample(self, image, self.keep, warp)
        _, _, w, h = self.box
        patch = cv2.warpAffine(image, warp, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
        blurred = cv2.GaussianBlur(patch, (0, 0), GAIN_SIGMA)
        gain = float(((blurred - blurred.mean()) * self.template_centred).sum() / self.template_energy)
        return gain, float(patch.std()) / self.template_std

    def is_jump(self, warp, history):
        """The warp's centre is off the constant-velocity prediction by more than JUMP_SHARE of the template."""
        _, _, w, h = self.box
        predicted = predicted_warp(history)
        cx, cy = decompose(warp, w, h)[:2]
        px, py = decompose(predicted, w, h)[:2]
        return abs(cx - px) > JUMP_SHARE * w or abs(cy - py) > JUMP_SHARE * h

    def step(self, image, history):
        """One frame: returns (warp to report, cc, is accepted, warp to extend the history with)."""
        last = history[-1]
        found_ncc = ncc_warp(self.template, image, last, self.margin)
        start = predicted_warp(history)
        cc, warp = ecc(self.template, image, start, self.keep)
        if cc < RESEED_CC:
            cc_seeded, warp_seeded = ecc(self.template, image, reseed(self.template, image, last, self.search), self.keep)
            if cc_seeded > cc:
                cc, warp = cc_seeded, warp_seeded
        if cc < ACCEPT_CC and found_ncc is not None:
            cc_ncc, warp_ncc = ecc(self.template, image, found_ncc, self.keep)
            cc, warp = (cc_ncc, warp_ncc) if cc_ncc > cc else (ACCEPT_CC, found_ncc)
        return (without_thin_stretch(warp, self.box[2:], self.thin), cc) if cc >= ACCEPT_CC else (last, cc)

    def run(self):
        count = len(self.greys)
        rows = [None] * count
        x, y, w, h = self.box
        for order in (range(self.ref, -1, -1), range(self.ref, count)):
            history = [np.array([[1, 0, x], [0, 1, y]], np.float32)]
            is_lost, last_gain = False, 1.0
            for k in order:
                image = self.greys[k]
                if is_lost:
                    gain, ratio = self.sample(image, history[-1])
                    rows[k] = (*decompose(history[-1], w, h), 0.0, gain, ratio, False)
                    continue
                warp, cc = self.step(image, history)
                gain, ratio = self.sample(image, warp)
                is_good = (cc >= ACCEPT_CC and LOST_GAIN <= gain <= last_gain + GAIN_RISE_MAX
                           and not self.is_jump(warp, history))
                if is_good:
                    last_gain = gain
                else:
                    # the walk stops once the element fades out (going on jumps to brighter look-alike siblings), but a
                    # rejected warp can sit on anything (cc 0.99 at gain -0.15 on a 9 % faded card): geometry holds
                    # and the opacity is read where the element was last accepted
                    is_lost = gain < LOST_GAIN
                    warp = history[-1]
                    gain, ratio = self.sample(image, warp)
                rows[k] = (*decompose(warp, w, h), cc, gain, ratio, is_good)
                if is_good:
                    history.append(warp)
        columns = [np.array(column, dtype=float) for column in zip(*rows)]
        return Track(self.ref, self.box, *columns[:9], columns[9].astype(bool), size=self.size, angle_hold=self.angle_hold)


def track_element(greys, box, ref, mask=None):
    return Tracker(greys, box, ref, mask).run()


class CompositeTracker:
    """Walk outward from the reference rest frame for a small element missing from the other rest frame that covers
    content there (a badge on an avatar's corner). Each frame is modelled as what lies behind the element (the other
    rest frame) with the element's rest appearance composited over it, shifted and scaled per axis about its centre, at
    the opacity that fits best (closed form: the gain). The shift and scale minimise the squared difference; cc is the
    normalised correlation of the frame's change from what lies behind with the model's, where the model puts the
    element. The covered content never enters a template: an ECC template of an 18 to 24 px badge with the avatar under
    it masked out fitted the avatar's rim at scale 1.7 in frames without the badge, and turned 13 to 57 deg on its disc
    elsewhere, which read as translate and scale 7 % off. No rotation."""

    def __init__(self, images, behinds, box, ref):
        height, width = images[0].shape[:2]
        x, y, w, h = box
        reach_x, reach_y = (max(TEMPLATE_PAD_PX, int(np.ceil(COMPOSITE_REACH * side))) for side in (w, h))
        x0, y0 = max(0, x - reach_x), max(0, y - reach_y)
        x1, y1 = min(width, x + w + reach_x), min(height, y + h + reach_y)
        self.ref, self.size, self.origin = ref, (w, h), (x0, y0)
        self.frames = [image[y0:y1, x0:x1].astype(np.float32) for image in images]
        self.behinds = [image[y0:y1, x0:x1].astype(np.float32) for image in behinds]
        change = self.frames[ref] - self.behinds[ref]
        # the box holds the element's own changes; its anti-aliased edge row can lie just beyond, and left out of the
        # model it made the fit stretch a tooltip 2 % at rest to cover it
        inside = np.zeros(change.shape[:2], bool)
        fringe = COMPOSITE_FRINGE_PX
        inside[max(0, y - y0 - fringe):y - y0 + h + fringe, max(0, x - x0 - fringe):x - x0 + w + fringe] = True
        # the rest frame is the element (premultiplied colour C, alpha a) over what lies behind it: C = S - (1 - a) B,
        # which gives the rest frame back at identity whatever a is (a binary footprint left out the edge rows of a
        # tooltip over a text line, and the fit stretched it 2 % to cover them)
        level = np.abs(change).max(axis=2)
        alpha = np.where(inside, np.clip((level - COMPOSITE_FOOTPRINT) / (COMPOSITE_OPAQUE - COMPOSITE_FOOTPRINT), 0, 1), 0)
        premultiplied = np.where(alpha[..., None] > 0, self.frames[ref] - (1 - alpha[..., None]) * self.behinds[ref], 0)
        self.sprite = np.dstack([premultiplied, alpha]).astype(np.float32)
        self.centre = (x - x0 + w / 2, y - y0 + h / 2)
        self.rest_norm = float(np.sqrt((change * change)[alpha > 0].sum())) + 1e-6

    def modelled(self, k, p):
        """(the model's change from what lies behind at frame k, its alpha) under p = (tx, ty, ln sx, ln sy)."""
        tx, ty, log_sx, log_sy = p
        sx, sy = np.exp(log_sx), np.exp(log_sy)
        cx, cy = self.centre
        inverse = np.float32([[1 / sx, 0, cx - (cx + tx) / sx], [0, 1 / sy, cy - (cy + ty) / sy]])
        height, width = self.sprite.shape[:2]
        # shrunk, the element is area-averaged on screen: a point-sampled sprite aliased a badge at a quarter of its size
        # (7 px) until the fit rejected the frame
        sigma_x, sigma_y = (SHRINK_SIGMA * max(0.0, 1 / scale - 1) for scale in (sx, sy))
        sprite = self.sprite if max(sigma_x, sigma_y) < 0.1 else cv2.GaussianBlur(self.sprite, (0, 0), max(sigma_x, 0.01),
                                                                                  sigmaY=max(sigma_y, 0.01))
        warped = cv2.warpAffine(sprite, inverse, (width, height), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
        alpha = warped[..., 3:]
        return warped[..., :3] - alpha * self.behinds[k], alpha[..., 0]

    def explained(self, k, p):
        """(squared difference left, cc, gain, std ratio) of frame k under p."""
        model, alpha = self.modelled(k, p)
        seen = self.frames[k] - self.behinds[k]
        energy = float((model * model).sum())
        dot = float((seen * model).sum())
        if energy <= 1e-6 or dot <= 0:
            return float((seen * seen).sum()), 0.0, 0.0, 0.0
        where = alpha > 0
        seen_there = float((seen[where] ** 2).sum())
        return (float((seen * seen).sum()) - dot * dot / energy, dot / np.sqrt(energy * seen_there + 1e-6), dot / energy,
                np.sqrt(seen_there) / self.rest_norm)

    def fit(self, k, starts):
        """The p that leaves the least squared difference, from each start; scales stay within COMPOSITE_SCALES."""
        low, high = np.log(COMPOSITE_SCALES)

        def left(p):
            return self.explained(k, p)[0] if low <= p[2] <= high and low <= p[3] <= high else np.inf
        return min((nelder_mead(left, start, COMPOSITE_STEPS, COMPOSITE_ITERS) for start in starts), key=lambda r: r[1])[0]

    def run(self):
        count = len(self.frames)
        rows = [None] * count
        x0, y0 = self.origin
        for order in (range(self.ref, -1, -1), range(self.ref, count)):
            history, is_lost = [np.zeros(4)], False
            for k in order:
                p = history[-1]
                if k != self.ref and not is_lost:
                    predicted = p if len(history) < 2 else p + (p - history[-2])
                    p = self.fit(k, [predicted, history[-1]])
                _, cc, gain, ratio = self.explained(k, p)
                is_good = k == self.ref or (not is_lost and cc >= ACCEPT_CC and gain >= LOST_GAIN)
                if is_good:
                    if k != self.ref:
                        history.append(p)
                else:
                    is_lost = is_lost or gain < LOST_GAIN
                    _, cc, gain, ratio = self.explained(k, history[-1])
                    p = history[-1]
                tx, ty, log_sx, log_sy = p
                rows[k] = (x0 + self.centre[0] + tx, y0 + self.centre[1] + ty, float(np.exp(log_sx)), float(np.exp(log_sy)),
                           0.0, 0.0, cc, gain, ratio, is_good)
        columns = [np.array(column, dtype=float) for column in zip(*rows)]
        box = (x0, y0, self.frames[0].shape[1], self.frames[0].shape[0])
        return Track(self.ref, box, *columns[:9], columns[9].astype(bool), size=self.size, angle_hold=0.0)


def track_composite(images, behinds, box, ref):
    return CompositeTracker(images, behinds, box, ref).run()


def geometry_channels(track, origin):
    """translate (main motion axis), scale and rotate channels with geometry weights clip(g, 0, 1)^2."""
    good = track.good
    other = 0 if origin == "end" else len(good) - 1
    is_other_known = bool(good[other]) and track.std_ratio[other] >= ABSENT_STD_RATIO
    weights = np.where(good, np.clip(track.gain, 0, 1) ** 2, 0.0)

    # at a fifth of its size a 30 px badge's ECC turned 2.3 deg, read as rotation on its full-size corners
    solid = good & (track.gain >= TYPE_MIN_GAIN) & (np.minimum(track.sx, track.sy) >= TYPE_MIN_SCALE)
    solid = solid if solid.any() else good

    def span(values):
        return float(np.ptp(values[solid])) if solid.any() else 0.0

    def channel(name, values, is_px=False):
        other_value = float(values[other]) if is_other_known else None
        ref_value = float(values[track.ref])
        start, end = (other_value, ref_value) if origin == "end" else (ref_value, other_value)
        return Channel(name, values, weights, start, end, is_px)

    channels, axis = [], None
    vx, vy = track.cx - track.cx[track.ref], track.cy - track.cy[track.ref]
    moved = [values - scale_shift(values, scale - scale[track.ref], side, solid)
             for values, scale, side in ((vx, track.sx, track.size[0]), (vy, track.sy, track.size[1]))]
    if max(span(values) for values in moved) >= TRANSLATE_MIN_PX:
        farthest = int(np.argmax(np.where(solid, np.hypot(vx, vy), -1)))
        axis = np.array([vx[farthest], vy[farthest]])
        axis /= np.hypot(*axis) + 1e-9
        channels.append(channel("translate", vx * axis[0] + vy * axis[1], is_px=True))
    # scale and rotation count where they move the element's edges by a pixel, like a translation: ECC reads sub-pixel
    # noise on small parts as 3 % scale on a 10 px bar (0.3 px) and 0.48 deg on a 20 px glyph (0.13 px at its corners)
    scaling = [values for values, side in zip((track.sx, track.sy), track.size)
               if side >= THIN_PX and span(values) >= SCALE_MIN and span(values) * side >= TRANSLATE_MIN_PX]
    if scaling:
        channels.append(channel("scale", np.mean(scaling, axis=0)))
    # the corners move with the element's size in that frame: 3.3 deg on a badge at 0.55 of its size moved them 0.6 px
    turned = np.radians(np.abs(track.rot - track.rot[track.ref])) * np.minimum(track.sx, track.sy) * np.hypot(*track.size) / 2
    corner_px = float(turned[solid].max()) if solid.any() else 0.0
    if span(track.rot) >= ROTATE_MIN_DEG and corner_px >= TRANSLATE_MIN_PX and track.angle_hold >= ANGLE_HOLD_MIN:
        channels.append(channel("rotate", track.rot))
    return channels, axis, is_other_known


def scale_shift(shift, stretch, side, solid):
    """The part of a centre's shift along one axis that a scale about a point within ORIGIN_SLACK of the box centre
    explains: offset x stretch, the offset fitted by least squares and clamped. A box a few px off its element (codec
    fringe on one side of a badge) moved its centre 1 px in step with a pure scale."""
    weight = float((stretch[solid] ** 2).sum())
    if weight == 0:
        return np.zeros_like(shift)
    offset = float((shift[solid] * stretch[solid]).sum()) / weight
    return np.clip(offset, -ORIGIN_SLACK * side, ORIGIN_SLACK * side) * stretch


def is_absent(track, origin):
    """The element is missing from the other rest frame: its region there has < 0.35 of the template's contrast."""
    other = 0 if origin == "end" else len(track.good) - 1
    return bool(track.std_ratio[other] < ABSENT_STD_RATIO)


SIDES = {"right": (0, 1), "left": (0, -1), "bottom": (1, 1), "top": (1, -1)}     # (axis 0 = x / 1 = y, sign)


@dataclass(frozen=True)
class Clip:
    """A line an element crosses on its way into or out of view: the frame (or ROI) edge, or the edge of a container
    that clips its content. `side` is where the hidden region lies; `line` is the boundary in work px (visible is
    x < line for right, x >= line for left, y < line for bottom, y >= line for top)."""
    side: str
    line: int
    is_frame_edge: bool

    @property
    def axis(self):
        return SIDES[self.side][0]

    @property
    def sign(self):
        return SIDES[self.side][1]

    def visible_mask(self, shape):
        mask = np.zeros(shape, np.uint8)
        index = [slice(None), slice(None)]
        index[1 - self.axis] = slice(0, self.line) if self.sign > 0 else slice(self.line, None)
        mask[tuple(index)] = 1
        return mask

    def cut_px(self, box):
        """How much of a box (x, y, w, h) lies on the hidden side, along the motion axis."""
        start, size = box[self.axis], box[2 + self.axis]
        hidden = start + size - self.line if self.sign > 0 else self.line - start
        return float(np.clip(hidden, 0, size))

    def hidden_vector(self, box, shown=0.0):
        """The shift that puts the box on the hidden side, all but `shown` px of it."""
        start, size = box[self.axis], box[2 + self.axis]
        vector = np.zeros(2)
        vector[self.axis] = self.line - start - shown if self.sign > 0 else self.line - (start + size) + shown
        return vector


@dataclass
class ClipMeasure:
    clip: Clip
    hidden: np.ndarray          # per window frame: the element is on the hidden side (all but `shown_px` of it)
    shown_px: float = 0.0       # how much of it showed inside the line at its hidden rest state (a shadow fringe)
    strip: tuple | None = None  # the rest-frame element box that is that part


def side_of(dx, dy):
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else "left"
    return "bottom" if dy > 0 else "top"


def motion_side(track):
    """The side the element travels toward, away from its rest frame (the farthest accepted centre of the ECC walk),
    or None when it hardly moved."""
    dx, dy = track.cx - track.cx[track.ref], track.cy - track.cy[track.ref]
    moved = np.where(track.good, np.hypot(dx, dy), -1.0)
    k = int(np.argmax(moved))
    if moved[k] < CLIP_MIN_MOVE_PX:
        return None
    return side_of(dx[k], dy[k])


def coarse_shift(greys, box, ref, k):
    """(dx, dy): how far the content of the padded box moved from rest frame `ref` to frame k, by phase correlation
    (even DFT sizes, as measure_scroll); (0, 0) when the correlation finds no clear peak."""
    x, y, w, h = padded_box(box, greys[0].shape[::-1])
    side_w, side_h = unbiased_dft_side(w), unbiased_dft_side(h)
    x, y, w, h = x + (w - side_w) // 2, y + (h - side_h) // 2, side_w, side_h
    window = cv2.createHanningWindow((w, h), cv2.CV_64F)
    (dx, dy), response = cv2.phaseCorrelate(greys[ref][y:y + h, x:x + w].astype(np.float64),
                                            greys[k][y:y + h, x:x + w].astype(np.float64), window)
    return (dx, dy) if response >= COARSE_MIN_RESPONSE else (0.0, 0.0)


def coarse_side(greys, box, ref):
    """The side an element leaves its rest frame toward, from the first frame whose content moved by phase correlation,
    or None. An exiting drawer's first frame moved 20 px (cubic-bezier(0.32, 0.72, 0, 1) starts at slope 2.25) with its
    bottom cut off by the frame edge, and the ECC walk accepted no frame at all."""
    order = range(ref + 1, len(greys)) if ref == 0 else range(ref - 1, -1, -1)
    for k in list(order)[:COARSE_FRAMES]:
        dx, dy = coarse_shift(greys, box, ref, k)
        if np.hypot(dx, dy) >= CLIP_MIN_MOVE_PX:
            return side_of(dx, dy)
    return None


def clip_line(activity, box, side):
    """Where the element's swept path ends beyond its rest box on `side`: the path is every column (row) of the
    element's band that changed, followed outward from the box with gaps of up to CLIP_GAP_PX. Ending at the image
    border means the frame (or ROI) edge, since the work crop holds all activity plus a margin; a box already at that
    border (a drawer flush with the edge, translateX(100%)) crosses it at once. None when the path does not leave the
    box."""
    axis, sign = SIDES[side]
    x, y, w, h = box
    band = activity[y:y + h, :] if axis == 0 else activity[:, x:x + w].T
    share = band.mean(axis=0)
    start, size = box[axis], box[2 + axis]
    border = share.size if sign > 0 else 0
    if (start + size >= border) if sign > 0 else (start <= border):
        return Clip(side, border, True)
    positions = range(start + size, share.size) if sign > 0 else range(start - 1, -1, -1)
    last_active, gap = None, 0
    for position in positions:
        if share[position] >= CLIP_ACTIVE_SHARE:
            last_active, gap = position, 0
            continue
        gap += 1
        if gap > CLIP_GAP_PX:
            break
    if last_active is None:
        return None
    line = last_active + 1 if sign > 0 else last_active
    return Clip(side, int(line), bool(line >= share.size if sign > 0 else line <= 0))


def surely_visible(clip, position, size, margin, minimum):
    """[lo, hi) of template offsets along the motion axis that stay on the visible side for every position within
    +-margin; at least `minimum` long, taken from the far side, when less than that is surely visible."""
    if clip.sign > 0:
        lo, hi = 0, int(np.clip(np.floor(clip.line - (position + margin)), 0, size))
    else:
        lo, hi = int(np.clip(np.ceil(clip.line - (position - margin)), 0, size)), size
    if hi - lo >= minimum:
        return lo, hi
    return (0, minimum) if clip.sign > 0 else (size - minimum, size)


def ncc_seed(template, image, clip, position, margin):
    """(top-left, peak) of the template part surely visible, NCC-searched along the motion axis within +-margin and
    across it within +-CLIP_SEARCH_ACROSS_PX; None when that part holds too little of the template's contrast or the
    search does not fit the image."""
    th, tw = template.shape
    along = (tw, th)[clip.axis]
    lo, hi = surely_visible(clip, position[clip.axis], along, margin, min(along, TEMPLATE_PAD_PX + CLIP_VISIBLE_MIN_PX))
    sub = template[:, lo:hi] if clip.axis == 0 else template[lo:hi, :]
    if sub.std() < max(1.0, CLIP_VISIBLE_CONTRAST * template.std()):
        return None
    offset = (lo, 0) if clip.axis == 0 else (0, lo)
    reach = (margin, CLIP_SEARCH_ACROSS_PX) if clip.axis == 0 else (CLIP_SEARCH_ACROSS_PX, margin)
    x0 = max(0, int(np.floor(position[0] - reach[0])) + offset[0])
    y0 = max(0, int(np.floor(position[1] - reach[1])) + offset[1])
    x1 = min(image.shape[1], int(np.ceil(position[0] + reach[0])) + offset[0] + sub.shape[1])
    y1 = min(image.shape[0], int(np.ceil(position[1] + reach[1])) + offset[1] + sub.shape[0])
    if x1 - x0 < sub.shape[1] or y1 - y0 < sub.shape[0]:
        return None
    _, peak, _, (bx, by) = cv2.minMaxLoc(cv2.matchTemplate(image[y0:y1, x0:x1], sub, cv2.TM_CCOEFF_NORMED))
    return (x0 + bx - offset[0], y0 + by - offset[1]), float(peak)


def predicted_position(path, t):
    """(top-left, step) at time t from the last two accepted positions; constant velocity in time (VFR steps vary)."""
    t1, x1, y1 = path[-1]
    if len(path) < 2:
        return (x1, y1), 0.0
    t0, x0, y0 = path[-2]
    rate = (t - t1) / (t1 - t0)
    return (x1 + (x1 - x0) * rate, y1 + (y1 - y0) * rate), float(np.hypot(x1 - x0, y1 - y0) * abs(rate))


def masked_sample(tracker, image, visible, warp):
    """(gain, std ratio) over the template pixels that land on `visible` image pixels only (the visible side of a clip
    line, or everything but static neighbours); the blur's reach is eroded away from the masked part when enough
    pixels remain."""
    _, _, w, h = tracker.box
    patch = cv2.warpAffine(image, warp, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
    inside = cv2.warpAffine(visible, warp, (w, h), flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP, borderValue=0) > 0
    if inside.sum() < 2:
        return 0.0, 0.0
    reach = int(4 * GAIN_SIGMA) | 1
    core = cv2.erode(inside.astype(np.uint8), np.ones((reach, reach), np.uint8)).astype(bool)
    core = core if core.sum() >= reach * reach else inside
    blurred = cv2.GaussianBlur(patch, (0, 0), GAIN_SIGMA)[core]
    reference = tracker.template_blurred[core] - tracker.template_blurred[core].mean()
    gain = float(((blurred - blurred.mean()) * reference).sum() / ((reference * reference).sum() + 1e-6))
    return gain, float(patch[inside].std() / (tracker.template[inside].std() + 1e-6))


def clipped_step(tracker, image, visible, clip, path, t, guess=None):
    """(top-left, cc) of one frame: NCC seed of the part surely visible near the predicted top-left (or `guess`), then
    translation-only ECC over the visible side (inputMask). None when nothing usable is visible."""
    position, step = predicted_position(path, t) if guess is None else (guess, 0.0)
    margin = CLIP_SEARCH_PX + 0.5 * step
    seed = ncc_seed(tracker.template, image, clip, position, margin)
    if seed is None or seed[1] < CLIP_NCC_MIN:
        return None
    (sx, sy), peak = seed
    criteria = ECC_LARGE_CRITERIA if tracker.template.size > ECC_LARGE_PX else ECC_CRITERIA
    try:
        cc, warp = cv2.findTransformECC(tracker.template, image, np.float32([[1, 0, sx], [0, 1, sy]]),
                                        cv2.MOTION_TRANSLATION, criteria, visible, 1)
    except cv2.error:
        return (float(sx), float(sy)), peak
    moved = (warp[0, 2] - sx, warp[1, 2] - sy)
    # across the motion axis the ECC may move no farther than the seed's search: the first 7 px strip of a sheet showed
    # only its flat top edge, and the ECC slid 19 px along it
    if np.hypot(*moved) > margin or abs(moved[1 - clip.axis]) > CLIP_SEARCH_ACROSS_PX:
        return (float(sx), float(sy)), peak
    return (float(warp[0, 2]), float(warp[1, 2])), float(cc)


def track_clipped(greys, times, box, ref, clip, mask=None):
    """Translation-only walk outward from the rest frame for an element a clip line cuts. A cut element's visible
    part holds no scale or rotation (one visible edge of a flat fill let the affine ECC shrink the template onto it:
    translate + scale + fade for a pure slide), so the rest frame's shape is kept and only template pixels on the
    visible side are matched. `mask` leaves neighbours in the template's pad out, as for the ECC walk: a static text line
    in an exiting drawer's pad held the NCC seed at 0.52 once the drawer had moved on from it."""
    tracker = Tracker(greys, box, ref, mask)
    visible = clip.visible_mask(greys[0].shape)
    if mask is not None:
        visible = visible & mask.keep
    x, y, w, h = tracker.box
    offset = np.array([box[0] - x, box[1] - y], float)
    rows = [None] * len(greys)
    for order in (range(ref, -1, -1), range(ref, len(greys))):
        path, is_lost, last = [(float(times[ref]), float(x), float(y))], False, ref
        for k in order:
            found = ((float(x), float(y)), 1.0) if k == ref else None if is_lost else clipped_step(
                tracker, greys[k], visible, clip, path, times[k])
            if found is None and not is_lost and k != ref:
                found = coarse_step(tracker, greys, visible, clip, path, times[k], box, last, k)
            position, cc = found if found else ((path[-1][1], path[-1][2]), 0.0)
            warp = np.float32([[1, 0, position[0]], [0, 1, position[1]]])
            gain, ratio = masked_sample(tracker, greys[k], visible, warp)
            element = (position[0] + offset[0], position[1] + offset[1], box[2], box[3])
            shown = box[2 + clip.axis] - clip.cut_px(element)
            is_good = found is not None and cc >= ACCEPT_CC and gain >= LOST_GAIN and shown >= CLIP_VISIBLE_MIN_PX
            # the walk ends where the element has gone behind the line; a frame that merely failed does not end it
            is_lost = is_lost or (not is_good and (gain < LOST_GAIN or shown < CLIP_VISIBLE_MIN_PX))
            if is_good and k != ref:
                path.append((float(times[k]), *position))
                last = k
            rows[k] = (position[0] + w / 2, position[1] + h / 2, 1.0, 1.0, 0.0, 0.0, cc, gain, ratio, is_good)
    columns = [np.array(column, dtype=float) for column in zip(*rows)]
    return Track(ref, tracker.box, *columns[:9], columns[9].astype(bool), size=tracker.size)


def coarse_step(tracker, greys, visible, clip, path, t, box, last, k):
    """A step the prediction missed, seeded by the phase-correlation shift along the motion axis of the element's box
    from `last`, the walk's last accepted frame. The first step away from rest has no velocity to predict with (a drawer
    closing fast left its rest by 20 px in one frame, beyond the NCC search), a VFR rest frame held from long before the
    start gives the second a velocity far too low, and a strongly decelerating close overshot the prediction by 14 px with
    71 px of the drawer still in view, where a shift from rest found too little of it left in its rest box."""
    (_, x0, y0), (_, x1, y1) = path[0], path[-1]
    moved = (round(box[0] + x1 - x0), round(box[1] + y1 - y0), box[2], box[3])
    shift = np.zeros(2)
    shift[clip.axis] = coarse_shift(greys, moved, last, k)[clip.axis]
    if not shift.any():
        return None
    return clipped_step(tracker, greys[k], visible, clip, path, t, guess=(x1 + shift[0], y1 + shift[1]))


def element_boxes(tracked, box):
    """The element's box (x, y, w, h) per frame under a translation-only track."""
    _, _, w, h = tracked.box
    dx, dy = box[0] - tracked.box[0], box[1] - tracked.box[1]
    return [(cx - w / 2 + dx, cy - h / 2 + dy, box[2], box[3]) for cx, cy in zip(tracked.cx, tracked.cy)]


def hidden_frames(images, box, clip, rest, threshold):
    """Frames whose strip just inside the line looks as in the rest frame where the element is out of view: while any
    of the element shows, it covers the line (it is one piece crossing it)."""
    x, y, w, h = box
    lo = clip.line - HIDDEN_STRIP_PX if clip.sign > 0 else clip.line
    size = images[0].shape[1 - clip.axis]
    along = slice(int(np.clip(lo, 0, size - HIDDEN_STRIP_PX)), int(np.clip(lo, 0, size - HIDDEN_STRIP_PX)) + HIDDEN_STRIP_PX)
    region = (slice(y, y + h), along) if clip.axis == 0 else (along, slice(x, x + w))
    reference = images[rest][region]
    return np.array([float(np.mean(pixels.channel_max(cv2.absdiff(image[region], reference)) > threshold))
                     <= HIDDEN_CHANGED_SHARE for image in images])


def measure_clip(window_images, greys, times, activity, box, ecc_track, origin, threshold, mask=None):
    """(translation-only Track, ClipMeasure) for an element that enters or leaves view across a clip line, else None.
    Clipped means: the ECC walk lost it at the other rest frame and the clipped walk finds at most three quarters of it
    there (a shadow fringe can show inside the line), its swept path runs from its box to a line, some tracked frame
    hides at least a quarter of it behind that line, and what shows keeps its full contrast (a fade-in whose faint
    start ends the swept path early is never at full contrast there). The ECC walk's std ratio cannot tell an empty
    rest frame at the frame border: warpAffine fills the outside black, which reads as texture."""
    rest = 0 if origin == "end" else len(greys) - 1
    if ecc_track.good[rest]:
        return None
    side = motion_side(ecc_track) or coarse_side(greys, box, ecc_track.ref)
    clip = clip_line(activity, box, side) if side else None
    if clip is None:
        return None
    tracked = track_clipped(greys, times, box, ecc_track.ref, clip, mask)
    cut = np.array([clip.cut_px(b) if good else 0.0 for b, good in zip(element_boxes(tracked, box), tracked.good)])
    crossing = tracked.good & (cut >= 1.0)
    least_cut = CLIP_MIN_CUT_SHARE * box[2 + clip.axis]
    if not crossing.any() or cut.max() < least_cut or (tracked.good[rest] and cut[rest] < least_cut):
        return None
    if np.median(tracked.gain[crossing]) < CLIP_OPAQUE_GAIN:
        return None
    tracked_frames = np.flatnonzero(tracked.good)
    beyond = np.arange(len(greys)) < tracked_frames[0] if origin == "end" else np.arange(len(greys)) > tracked_frames[-1]
    hidden = beyond & hidden_frames(window_images, box, clip, rest, threshold)
    shown = box[2 + clip.axis] - cut[rest] if tracked.good[rest] else 0.0
    return tracked, ClipMeasure(clip, hidden, float(shown))


def is_opaque_when_seen(track):
    """Every accepted frame where the element is at least half its size keeps the rest frame's contrast: an element
    missing from the other rest frame that never shows faint did not fade (a badge growing from scale 0 held gain 0.92
    at a fifth of its size, and 0.79 at a tenth where resampling blurs it)."""
    sized = track.good & (np.minimum(track.sx, track.sy) >= TYPE_MIN_SCALE)
    seen = sized if sized.any() else track.good
    return bool(seen.any()) and float(track.gain[seen].min()) >= CLIP_OPAQUE_GAIN


def opacity_channel(track, origin):
    """Gain with fixed amplitude: 0 -> 1 for an entrance, 1 -> 0 for an exit."""
    values = np.clip(track.gain, -0.1, 1.2)
    start, end = (0.0, 1.0) if origin == "end" else (1.0, 0.0)
    return Channel("opacity", values, np.ones_like(values), start, end)


def blend_channel(images, rest, box):
    """alpha_t = <F_t - A, B - A> / |B - A|^2 over the box in colour, A from the rest frame before the window (`rest`)
    and B from its last frame; returns (Channel or None, residual p90)."""
    x, y, w, h = box
    before = rest[y:y + h, x:x + w].astype(np.float32)
    after = images[-1][y:y + h, x:x + w].astype(np.float32)
    delta = after - before
    energy = float((delta * delta).sum())
    if energy <= 0:
        return None, 1.0
    delta_rms = np.sqrt(energy / delta.size) + 1e-6
    alphas, residuals = [], []
    for image in images:
        offset = image[y:y + h, x:x + w].astype(np.float32) - before
        alpha = float((offset * delta).sum() / energy)
        alphas.append(alpha)
        residuals.append(float(np.sqrt(((offset - alpha * delta) ** 2).mean())) / delta_rms)
    alphas = np.array(alphas)
    return Channel("color", alphas, np.ones_like(alphas), 0.0, 1.0), float(np.percentile(residuals, 90))


def energy_channel(images, box, threshold):
    """Normalised cumulative change energy inside the box (the activity-bbox fallback's progress)."""
    x, y, w, h = box
    steps = [0.0]
    for previous, image in zip(images, images[1:]):
        diff = pixels.channel_max(cv2.absdiff(image[y:y + h, x:x + w], previous[y:y + h, x:x + w]))
        steps.append(float(diff[diff > threshold].sum()))
    cumulative = np.cumsum(steps)
    progress = cumulative / cumulative[-1] if cumulative[-1] > 0 else cumulative
    return Channel("energy", progress, np.ones_like(progress), 0.0, 1.0)


@dataclass
class ScrollMeasure:
    shifts: np.ndarray          # (n, 2) per-frame shift, frame k relative to k-1 (row 0 is zero)
    responses: np.ndarray       # phase-correlation response per step (row 0 is 1)
    positions: np.ndarray       # integrated displacement

    @property
    def travel(self):
        return self.positions[-1]

    @property
    def reliable(self):
        return self.responses >= SCROLL_MIN_RESPONSE

    def is_moving(self):
        """Steps that shifted by at least half a pixel."""
        return np.linalg.norm(self.shifts, axis=1) >= SCROLL_MOVING_PX

    def unreliable_frac(self, moving):
        return float((~self.reliable[moving]).mean()) if moving.any() else 0.0


def unbiased_dft_side(n):
    """Largest side <= n whose DFT size is even: cv2.phaseCorrelate reads a +0.5 px shift between identical frames
    when the padded size is odd (measured: 1124 and 1125 px pad to 1125 and read +0.500)."""
    side = n
    while side > 2 and cv2.getOptimalDFTSize(side) % 2:
        side -= 1
    return side


def measure_scroll(greys, box):
    """Phase correlation of consecutive frames inside the box (Hanning window); unreliable steps (response < 0.3)
    take the mean of their reliable neighbours before integration."""
    x, y, w, h = box
    side_w, side_h = unbiased_dft_side(w), unbiased_dft_side(h)
    x, y, w, h = x + (w - side_w) // 2, y + (h - side_h) // 2, side_w, side_h
    window = cv2.createHanningWindow((w, h), cv2.CV_64F)
    shifts, responses = [(0.0, 0.0)], [1.0]
    previous = greys[0][y:y + h, x:x + w].astype(np.float64)
    for grey in greys[1:]:
        current = grey[y:y + h, x:x + w].astype(np.float64)
        (dx, dy), response = cv2.phaseCorrelate(previous, current, window)
        shifts.append((dx, dy))
        responses.append(response)
        previous = current
    shifts, responses = np.array(shifts), np.array(responses)
    reliable = responses >= SCROLL_MIN_RESPONSE
    filled = shifts.copy()
    good_steps = np.nonzero(reliable)[0]
    for k in np.nonzero(~reliable)[0]:
        before, after = good_steps[good_steps < k], good_steps[good_steps > k]
        neighbours = [shifts[i] for i in (before[-1:].tolist() + after[:1].tolist()) if i > 0]
        filled[k] = np.mean(neighbours, axis=0) if neighbours else 0.0
    return ScrollMeasure(shifts, responses, np.cumsum(filled, axis=0))


def scroll_channel(measure):
    """Integrated displacement along the travel direction, fixed amplitude (both rest states are observed)."""
    travel = measure.travel
    length = float(np.hypot(*travel))
    axis = travel / (length + 1e-9)
    values = measure.positions @ axis
    weights = np.where(measure.reliable, 1.0, 0.0)
    weights[0] = 1.0
    return Channel("scroll", values, weights, 0.0, float(values[-1]), is_px=True), axis
