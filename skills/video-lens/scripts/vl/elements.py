"""Elements of one motion event (spec 7.8.3, 7.8.4, 7.8.5 derived values, 7.8.8): rest-frame segmentation,
exits, the scroll / activity-bbox fallback, channels, fits and the element records of analysis.json.

Everything here works on one event window at work scale; `WorkGeometry` maps work pixels back to source pixels.
"""
from dataclasses import dataclass, field, replace

import cv2
import numpy as np

from . import easing, pixels, track
from .track import ClipMeasure, ScrollMeasure, Track
from .context import motion_time
from .scrim import Scrim, find_scrim

FG_THRESHOLD = 5
CROP_MARGIN = 0.10
# a 30 px badge got a 4 px margin: its padded template, scaled 1.1 at the overshoot, sampled black beyond the crop and
# read as a fade to 0.37
CROP_MIN_MARGIN_PX = 24
STILL_RING_MIN = 0.1                # share of the border ring that must stay still to define the background alone
MIN_ELEMENT_AREA = 150
MIN_ELEMENT_AREA_FRAC = 1e-4
MIN_ACTIVE_SHARE = 0.15
MIN_MOVING_SHARE = 0.05             # px of a component that are not static content (a flickering nav icon: 1 to 2 %)
STATIC_REST_DIFF = 16               # the same pixel in both rest frames: codec noise at rest reached 11 on an icon edge
STATIC_WINDOW_PX = 5
CREEP_SHARE = 0.5
NEST_SLACK_PX = 2
ANCHOR_STATIC_SHARE = 0.9           # content the element sits on is this much the same in both rest frames outside its box
# an element this small that covers content (at least this share of its box) where it is missing is tracked composited
# over that frame; larger ones hold enough structure of their own for a masked ECC template
COMPOSITE_MAX_AREA = 160 * 160
COVERED_MIN_SHARE = 0.02
ABSENT_SHOWN_SHARE = 0.05           # ... and anything else showing in its box there is codec noise
STANDS_OUT_SHARE = 0.5
GROWN_VISIBLE_PX = 2                # an element grown from scale 0 is not seen while it spans less than this
BOX_COMPONENT_MIN_PX = 3            # single encoder-noise pixels do not widen an activity box
FALLBACK_COVER = 0.8                # one element covering this much of the activity box may be a whole region
RIGID_TRACKED = 0.8                 # ... it stays an element only if it tracks as one rigid thing
SCROLL_MIN_TRAVEL_PX = 1.0
CONTENT_SCROLL_MIN_PX = 3.0         # content travel inside an element whose outline stays put (carousel, list)
OUTLINE_SHIFT_SHARE = 0.25          # ... the outline's own shift is at most this share of that travel
SAME_OUTLINE_IOU = 0.9             # a rotated or rescaled element changes its box more than this
VISIBLE_STEP_PX = 0.25              # a px channel counts as changed beyond this
VISIBLE_PROGRESS = 0.02
VISIBLE_OPACITY = 0.05
HIGH_TRACKED, HIGH_RMSE, HIGH_RANGE = 0.8, 0.01, 0.10
LOW_SCROLL_UNRELIABLE = 0.2
TIMING_DIFFERS_FRAMES, TIMING_DIFFERS_DURATION = 1.0, 0.10
# a channel splits off when the shared fit misses it by more than 1.5 x its own fit plus the whole RMSE a high-confidence
# fit may have: a 5 % scale change fading out got 3 x lower RMSE (0.0031 vs 0.0092) from its own free curve and amplitude
SPLIT_RATIO, SPLIT_MARGIN = 1.5, HIGH_RMSE
INSTANT_INTERVALS = 2               # a change done within this many frame intervals has no measurable easing
INSTANT_BULK = 0.1                  # ... or whose 10 % to 90 % rise takes one interval
LINEAR_TIMING_NOTE = "overshooting motion no cubic-bezier follows: timing and easing from the measured linear() curve"
START_UNKNOWN_NOTE = ("start state not observed (enters from outside the frame or ROI): travel, duration and easing "
                      "come from the visible part only; do not quote the easing")
GROWN_NOTE = ("grew from nothing: missing from the other rest frame and opaque whenever seen, so it scaled from 0, not "
              "faded; it {state} at scale 0 (assumed, the natural reading of scale(0))")
CHANNEL_ORDER = ("translate", "scale", "rotate", "opacity", "color", "scroll", "energy")
AT_EDGE, BEYOND_EDGE, SEEN = "at-edge", "beyond-edge", "seen"
CLIPPED_READING = "clipped by the edge, not scaled or faded: position read from the part that stays visible"
BACKDROP_NOTE = ("backdrop (scrim): an overlay of {colour} at {alpha:.0f} % over the page {action} (measured: the page "
                 "behind x {gain:.2f} between the rest frames, {share:.0f} % of its structure following that one map); "
                 "its opacity follows this row's timing and curve, the box is the work crop")
NEAR_TIE_RATIO_TEXT = f"{round((easing.NEAR_TIE_RATIO - 1) * 100)} % RMSE"
OCCLUSION_SLACK_PX = 2              # an occluder's extent is grown by this much (its edge's codec ringing)
# changes of covered content outside the occluder's extent are codec noise: one row of a text line (4.5 % of its box, 19
# levels) 26 px from a drawer's edge in a VFR re-encode; content changing on its own moves most of its pixels
OCCLUDED_OUTSIDE_SHARE = 0.05
# under a scrim the page is re-encoded in every frame: text lines a sheet was yet to cover changed by up to 25 levels
SCRIM_REST_DIFF = 32
# the at-edge reading stands unless the free one fits at least twice as well: a free custom curve absorbed 48 px of
# invented hidden travel at 1.5 x lower RMSE, while a real 80 px hidden start fitted 6.7 x better
EDGE_READING_RATIO, EDGE_READING_MARGIN = 2.0, 0.0002


@dataclass(frozen=True)
class WorkGeometry:
    """Work px = (source px - offset) * scale, per axis."""
    offset: tuple
    scale: tuple

    def box_to_source(self, box):
        x, y, w, h = box
        (ox, oy), (sx, sy) = self.offset, self.scale
        x0, y0 = ox + x / sx, oy + y / sy
        return [int(round(x0)), int(round(y0)), int(round(w / sx)), int(round(h / sy))]

    def vector_to_source(self, vector):
        return np.array([vector[0] / self.scale[0], vector[1] / self.scale[1]])


@dataclass
class Window:
    """One event's frames at work scale: index 0 is the rest frame before, the last is the rest frame after."""
    times: np.ndarray
    images: list
    greys: list
    activity: np.ndarray
    activity_box: tuple
    geometry: WorkGeometry
    scrim: Scrim | None = None      # a backdrop dimming the page behind the motion; activity is taken beyond it
    rest: np.ndarray | None = None  # the frame a colour change is measured from (detect.lead_start), at or before 0


@dataclass(frozen=True)
class Settings:
    dt: float
    curves: tuple
    is_joint: bool
    motion_mode: str
    pix_threshold: int
    max_elements: int


@dataclass
class Element:
    origin: str                     # "end": found in the rest frame after; "start": in the rest frame before (exit)
    box: tuple                      # work px at its reference rest frame
    track: Track | None
    channels: list
    types: list
    fit: easing.Fit
    appears: bool = False
    disappears: bool = False
    axis: np.ndarray | None = None
    own_fits: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    colours: tuple | None = None    # colour elements: median box colour (hex) in the rest frames before and after
    scroll: ScrollMeasure | None = None   # scroll elements: the phase-correlation measurement
    clip: ClipMeasure | None = None        # elements entering or leaving view across a clip line
    clip_reading: str | None = None       # AT_EDGE, BEYOND_EDGE or SEEN: where the hidden rest state was taken to be
    grown: bool = False                   # grew from (or shrank to) scale 0: that rest state is assumed, not seen
    is_backdrop: bool = False             # the scrim's overlay, measured over the whole page, not tracked


@dataclass
class Candidate:
    """A rest-frame element before its fit: ECC-tracked, or measured by phase correlation (content scroll)."""
    origin: str
    box: tuple
    track: Track | None = None
    scroll: ScrollMeasure | None = None
    clip: ClipMeasure | None = None
    extent: tuple | None = None     # RestBox.extent; the box once a clip line is measured against it
    blend: easing.Channel | None = None     # a colour change in place (in_place_blend): its only channel


def make_window(times, images, rest, threshold, geometry):
    """The event's frames at work scale, and the rest frame of its lead-in. When the border ring hardly stayed still
    anywhere, a backdrop dimming the whole page is looked for, and activity is what changed beyond it."""
    greys = [cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32) for image in images]
    activity = changed_pixels(images, threshold)
    # a dim changes the page by a few levels per frame, under the pixel threshold in 23 % of a VFR re-encode's ring, but
    # by 100 between the rest frames
    rest_changed = pixels.channel_max(cv2.absdiff(images[0], images[-1])) > threshold
    scrim = find_scrim(images) if (~border_ring(activity | rest_changed)).mean() < STILL_RING_MIN else None
    window = Window(np.asarray(times, float), images, greys, activity, activity_box(activity), geometry, rest=rest)
    if scrim is None:
        return window
    # a dimming page is re-encoded in every frame: its text edges missed the map by up to 24 levels, 0.3 to 0.8 % of
    # the page by more than 8 and 0.01 % by more than 16, the codec's noise at rest
    activity = changed_pixels(images, max(threshold, STATIC_REST_DIFF), scrim)
    return narrowed(replace(window, activity=activity, activity_box=activity_box(activity), scrim=scrim))


def element_sized_box(activity):
    """Bounding box of the changed components large enough to hold an element (rest_elements' least area), or None."""
    count, _, stats, _ = cv2.connectedComponentsWithStats(activity.astype(np.uint8), connectivity=8)
    kept = stats[1:][stats[1:, 4] >= min_element_area(activity.shape)]
    if not len(kept):
        return None
    x0, y0 = kept[:, 0].min(), kept[:, 1].min()
    return int(x0), int(y0), int((kept[:, 0] + kept[:, 2]).max() - x0), int((kept[:, 1] + kept[:, 3]).max() - y0)


def min_element_area(shape):
    return max(MIN_ELEMENT_AREA, MIN_ELEMENT_AREA_FRAC * shape[0] * shape[1])


def narrowed(window):
    """The window cut to its element-sized activity plus the work crop's margin, the scrim's page box kept. A scrim
    spreads an event's detected change over the whole frame, and the border ring of such a crop ran along a white app
    bar, which then was the background and joined the whole page to the sheet as one element. Codec specks the scrim
    leaves (0.01 % of the page) do not widen the cut."""
    height, width = window.activity.shape
    moving = element_sized_box(window.activity)
    if moving is None:
        return replace(window, scrim=replace(window.scrim, box=(0, 0, width, height)))
    x, y, w, h = moving
    mx, my = (max(CROP_MIN_MARGIN_PX * scale, CROP_MARGIN * size) for scale, size in zip(window.geometry.scale, (w, h)))
    x0, y0 = max(0, int(x - mx)), max(0, int(y - my))
    x1, y1 = min(width, int(np.ceil(x + w + mx))), min(height, int(np.ceil(y + h + my)))
    cut = (slice(y0, y1), slice(x0, x1))
    (ox, oy), (sx, sy) = window.geometry.offset, window.geometry.scale
    return Window(window.times, [image[cut] for image in window.images], [grey[cut] for grey in window.greys],
                  window.activity[cut], shifted(window.activity_box, -x0, -y0), WorkGeometry((ox + x0 / sx, oy + y0 / sy),
                  window.geometry.scale), replace(window.scrim, box=(-x0, -y0, width, height)), window.rest[cut])


def changed_pixels(images, threshold, scrim=None):
    """Pixels that changed between consecutive frames by more than `threshold`, beyond the scrim's share of the frame."""
    activity = np.zeros(images[0].shape[:2], bool)
    for k in range(1, len(images)):
        previous = images[k - 1] if scrim is None else scrim.carry(images[k - 1], k - 1, k)
        activity |= pixels.channel_max(cv2.absdiff(images[k], previous)) > threshold
    return activity


def rest_noise(window):
    """How far one static pixel may differ between two frames of the window (the second under its share of a scrim)."""
    return STATIC_REST_DIFF if window.scrim is None else SCRIM_REST_DIFF


def carried(window, image, k_from, k_to):
    """`image` (a frame, or a crop of one) seen at window frame k_from, as it would look under frame k_to's share of the
    scrim; unchanged without one."""
    return image if window.scrim is None else window.scrim.carry(image, k_from, k_to)


def activity_box(mask):
    """Bounding box of the changed components of at least 3 px; the whole mask's box when none is that large."""
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    kept = stats[1:][stats[1:, 4] >= BOX_COMPONENT_MIN_PX] if count > 1 else stats[:0]
    if not len(kept):
        return cv2.boundingRect(mask.astype(np.uint8))
    x0, y0 = kept[:, 0].min(), kept[:, 1].min()
    x1, y1 = (kept[:, 0] + kept[:, 2]).max(), (kept[:, 1] + kept[:, 3]).max()
    return int(x0), int(y0), int(x1 - x0), int(y1 - y0)


def border_ring(image):
    return np.concatenate([image[0], image[-1], image[:, 0], image[:, -1]])


def background_colour(frame, activity):
    """Per-channel median of the 1 px border ring, over the ring pixels that stayed still during the event when at least
    STILL_RING_MIN of them did. A drawer that slid in over most of the work crop's ring (61 to 64 % of it) made its own
    white the median, and its rows and avatars became 18 to 24 elements."""
    ring = border_ring(frame)
    still = ~border_ring(activity)
    return np.median(ring[still] if still.mean() >= STILL_RING_MIN else ring, axis=0)


def differs(image, colour):
    """Pixels more than FG_THRESHOLD from `colour` in some channel."""
    return np.abs(image.astype(np.int16) - colour.astype(np.int16)).max(axis=2) > FG_THRESHOLD


@dataclass(frozen=True)
class RestView:
    """One rest frame against the other rest frame of the event."""
    shown: np.ndarray           # differs from the background here
    foreground: np.ndarray      # shown, closed: its connected components are the rest frame's parts
    was_shown: np.ndarray       # differs from the background in the other rest frame
    static: np.ndarray          # static content (static_pixels)
    rest_change: np.ndarray     # largest channel difference between the two rest frames
    frame: np.ndarray           # this rest frame
    other: np.ndarray           # the other rest frame (under this frame's share of a scrim)
    background: np.ndarray      # the page colour (background_colour)


def rest_view(frame, other, activity):
    background = background_colour(frame, activity)
    shown, was_shown = differs(frame, background), differs(other, background)
    rest_change = pixels.channel_max(cv2.absdiff(frame, other))
    foreground = cv2.morphologyEx(shown.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    return RestView(shown, foreground, was_shown,
                    static_pixels(shown & was_shown & (rest_change <= STATIC_REST_DIFF), activity), rest_change,
                    frame, other, background)


def static_pixels(same, activity):
    """Pixels of static content: the same non-background pixels in both rest frames (`same`) whose neighbourhood stayed
    mostly still during the event, with the one px fringe around them. A carousel's strip looks the same at both rests
    after a shift of one card pitch, but all of it moved in between; the codec flickers a static neighbour's edge only
    here and there (and a VFR re-encode's first frame took its fringe for background)."""
    still = cv2.blur(activity.astype(np.float32), (STATIC_WINDOW_PX, STATIC_WINDOW_PX)) < 0.5
    return cv2.dilate((same & still).astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)


def template_mask(window, view, box, ref):
    """TemplateMask for tracking the element in `box` of rest frame `ref`, or None. The template's pad adds the
    background around the element, so what shows in the pad and is not the element is left out: another part of the
    frame, or static content joined to it only through the closing's bridge (two columns of a nav icon beside a 22 px
    badge held its overshoot to scale 1.047 across where it grew to 1.098, with 5 deg of rotation). Static content that
    touches it stays: its own edge or border, which a change of colour in place shows alike in both rest frames (left
    out, its outer ring let ECC stretch a button 1 to 3 % while only its fill changed, a 4 px border's too). Content it
    sits on is left out as well, the part it covers at rest included (anchor_pixels). The template holds background
    where a left-out pixel shows at rest what the other rest frame shows there, the content itself; where the element
    covers it, the element's own pixel stays (stubs of an icon's outline kept at a badge's edge let it read 35 deg of
    rotation)."""
    height, width = view.shown.shape
    px, py, pw, ph = track.padded_box(box, (width, height))
    region = (slice(py, py + ph), slice(px, px + pw))
    inside = np.zeros((ph, pw), bool)
    inside[box[1] - py:box[1] - py + box[3], box[0] - px:box[0] - px + box[2]] = True
    _, labels = cv2.connectedComponents(view.foreground[region], connectivity=8)
    own = np.bincount(labels[inside & (labels > 0)]).argmax() if (inside & (labels > 0)).any() else -1
    _, touching = cv2.connectedComponents(view.shown[region].astype(np.uint8), connectivity=8)
    own_touching = np.bincount(touching[inside & (touching > 0)]).argmax() if (inside & (touching > 0)).any() else -1
    other = len(window.images) - 1 if ref == 0 else 0
    at_rest = window.images[ref][region]
    seen = carried(window, window.images[other][region], other, ref)
    left_out = view.shown[region] & ~inside & ((labels != own) | (view.static[region] & (touching != own_touching)))
    left_out |= anchor_pixels(at_rest, seen, inside, view.background)
    if not left_out.any():
        return None
    keep, hold = np.ones((height, width), np.uint8), np.zeros((height, width), bool)
    keep[region][left_out] = 0
    hold[region] = left_out & (~inside | (pixels.channel_max(cv2.absdiff(at_rest, seen)) <= STATIC_REST_DIFF))
    return track.TemplateMask(keep, hold)


def anchor_pixels(at_rest, seen, inside, background):
    """Content the element sits on or beside and does not carry, over its padded box (`at_rest` there at its rest
    frame, `seen` the other rest frame): the parts of `seen` that differ from the page's `background` (content_mask) and
    look the same in both rest frames where they lie outside its box. It stays put while the element scales or moves over
    it: a badge on an icon's corner uncovered the corner while shrinking, and the icon dragged the warp into translate,
    scale and 91 deg of rotation for a pure scale. Its part under the element at rest is left out too; the element's own
    state at the other rest frame differs outside its box, so it stays. The page's background, not the pad's median,
    sets what is content (a tooltip between two text lines had their grey for its pad's median). Stillness around the
    content is not asked (static_pixels): beside a growing badge the codec's activity reached the text line under it."""
    anchors = np.zeros(inside.shape, bool)
    if inside.all():
        return anchors
    same = pixels.channel_max(cv2.absdiff(at_rest, seen)) <= STATIC_REST_DIFF
    count, labels = cv2.connectedComponents(content_mask(seen, background).astype(np.uint8), connectivity=8)
    for label in range(1, count):
        outside = (labels == label) & ~inside
        if outside.any() and same[outside].mean() >= ANCHOR_STATIC_SHARE:
            anchors |= labels == label
    return anchors


def content_mask(image, background):
    """Pixels that differ from the page's background by more than two rest frames of one static pixel may differ: fainter
    ones cannot be told the same as what replaced them (a card fading in, at 6 % at the first rest frame, looked the same
    as the page it left, and was taken for content under its own final box)."""
    return pixels.channel_max(cv2.absdiff(image, np.full_like(image, background.astype(np.uint8)))) > STATIC_REST_DIFF


def rest_track(window, view, box, ref):
    """Walk of the element at `box` of rest frame `ref` (`view` is that frame's RestView): ECC, what shows in the pad
    and the content it sits on left out of the match; a small element missing from the other rest frame that covers
    content there is tracked as composited over that frame instead (track.CompositeTracker)."""
    if is_over_content(view, box):
        other = len(window.images) - 1 if ref == 0 else 0
        behinds = [carried(window, window.images[other], other, k) for k in range(len(window.images))]
        return track.track_composite(window.images, behinds, box, ref)
    return track.track_element(window.greys, box, ref, template_mask(window, view, box, ref))


def is_over_content(view, box):
    """The element at `box` of the rest frame `view` belongs to is small, missing from the other rest frame, and covers
    content that shows there, at least COVERED_MIN_SHARE of its box (other_rest_shares)."""
    if box_area(box) > COMPOSITE_MAX_AREA:
        return False
    content, other = other_rest_shares(view, box)
    return content >= COVERED_MIN_SHARE and other <= ABSENT_SHOWN_SHARE


def other_rest_shares(view, box):
    """(share of `box` that content staying put beyond it fills in the other rest frame (anchor_pixels), share any other
    content fills there); what is left of the box shows the page's background."""
    px, py, pw, ph = track.padded_box(box, view.shown.shape[::-1])
    region = (slice(py, py + ph), slice(px, px + pw))
    inside = np.zeros((ph, pw), bool)
    inside[box[1] - py:box[1] - py + box[3], box[0] - px:box[0] - px + box[2]] = True
    if inside.all():
        return 0.0, 1.0
    anchors = anchor_pixels(view.frame[region], view.other[region], inside, view.background) & inside
    other = content_mask(view.other[region], view.background) & inside & ~anchors
    return float(anchors.sum()) / inside.sum(), float(other.sum()) / inside.sum()


@dataclass(frozen=True)
class RestBox:
    """A rest-frame element. `box` holds its own changes, the part tracked; `extent` all of it but static content, a
    box-shadow and a slow tail's creep included, which places it against a clip line."""
    box: tuple
    extent: tuple


def rest_elements(view, activity, max_elements):
    """RestBoxes of the components of a rest frame that differ from the background and overlap the event's activity
    (spec 7.8.3 steps 1-4). Static content is left out: a static neighbour joins a component when it has the same colour
    (a white top bar beside a white drawer) or lies within the closing's reach with its fringe (a nav icon 3 px from a
    badge). A component that is almost all static content is a neighbour the codec flickers near the motion, unless what
    changed in it between the rest frames stands out from it as a thing of its own (standing_part)."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(view.foreground, connectivity=8)
    active = cv2.dilate(activity.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    min_area = min_element_area(activity.shape)
    found = []
    for label in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[label])
        if area < min_area:
            continue
        region = (slice(y, y + h), slice(x, x + w))
        component, static = labels[region] == label, view.static[region]
        is_whole = is_element_part(component, active[region], static, min_area)
        for part in [component] if is_whole else standing_part(view, region, component, min_area):
            if not is_element_part(part, active[region], static, min_area):
                continue
            extent = activity_box(part & ~static)
            box = own_box(part, activity[region], view.rest_change[region], static) or extent
            whole = shifted(cv2.boundingRect(part.astype(np.uint8)), x, y)
            found.append((int(part.sum()), whole, RestBox(shifted(box, x, y), shifted(extent, x, y))))
    found.sort(key=lambda row: -row[0])
    kept = []
    for area, whole, rest in found:
        # children move with their container: nesting is judged on whole components
        if not any(is_nested(whole, other) for _, other, _ in kept):
            kept.append((area, whole, rest))
    return [rest for _, _, rest in kept[:max_elements]]


def is_element_part(part, active, static, min_area):
    """At least the least element area, mostly within the event's activity, and not almost all static content."""
    size = int(part.sum())
    return (size >= min_area and active[part].mean() >= MIN_ACTIVE_SHARE
            and (part & ~static).sum() >= MIN_MOVING_SHARE * size)


def standing_part(view, region, component, min_area):
    """[what changed in `component` (over `region`) between the rest frames] when a connected piece of it holds at least
    `min_area` px and it stands out from the component around it, else []. A 24 px badge on an avatar that touched two
    text lines was 4.4 % of their component, a 34 px dot on a white card 1.3 % of the card's: both were dropped as static."""
    changed = changed_parts(component, view.rest_change[region])
    _, _, stats, _ = cv2.connectedComponentsWithStats(changed.astype(np.uint8), connectivity=8)
    if not (stats[1:, 4] >= min_area).any() or not stands_out(view.frame[region], changed, component):
        return []
    return [changed]


def stands_out(frame, part, component):
    """Most of the part's pixels differ from the component's pixels around it (their median colour): a dot on a card
    does, the patch of the card it covers in the other rest frame does not."""
    ring = cv2.dilate(part.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool) & component & ~part
    if not ring.any():
        return True
    colour = np.median(frame[ring], axis=0)
    return float(differs(frame, colour)[part].mean()) >= STANDS_OUT_SHARE


def changed_parts(component, rest_change):
    """The component's pixels that differ between the rest frames beyond the foreground threshold, opened so that one-px
    chains of codec noise along a line's edge join nothing."""
    changed = (component & (rest_change > FG_THRESHOLD)).astype(np.uint8)
    return cv2.morphologyEx(changed, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)).astype(bool)


def shifted(box, x, y):
    return box[0] + x, box[1] + y, box[2], box[3]


def own_box(component, activity, rest_change, static):
    """(x, y, w, h) of a component's own changes, or None when they are too few. Its own changes are the pixels that
    changed during the event, plus those whose change between the rest frames is at least CREEP_SHARE of the
    component's typical one (a slow tail creeps below the per-frame threshold), static content left out. The codec's
    ringing around an edge changes its pixels by a small share of the edge (6 to 12 beside a badge that changed by 99):
    a box that held it put that ringing, which does not scale with the element, into its template and read a badge's
    overshoot 1.6 % low, enough for a custom curve 7.6 % long to beat easeOutBack."""
    active = component & activity
    typical = float(np.median(rest_change[active])) if active.any() else 0.0
    own = (active | (component & (rest_change > max(FG_THRESHOLD, CREEP_SHARE * typical)))) & ~static
    return activity_box(own) if own.sum() >= MIN_MOVING_SHARE * component.sum() else None


def is_nested(inner, outer):
    """Children move with their container: a box inside a kept box is dropped."""
    return (inner[0] >= outer[0] - NEST_SLACK_PX and inner[1] >= outer[1] - NEST_SLACK_PX
            and inner[0] + inner[2] <= outer[0] + outer[2] + NEST_SLACK_PX
            and inner[1] + inner[3] <= outer[1] + outer[3] + NEST_SLACK_PX)


def explains(candidate, start):
    """The end element accounts for this start element (a RestBox): tracked back to the rest frame before, it lands on
    it (centre in its half-box), or the start element is the part of it showing inside its clip line; a content-scroll
    element or a colour change in place accounts for the outline it overlaps."""
    if candidate.scroll is not None or candidate.blend is not None:
        return iou(candidate.box, start.box) >= SAME_OUTLINE_IOU
    if candidate.clip is not None and candidate.clip.strip == start.extent:
        return True
    return lands_on(candidate.track, 0, start.box)


def lands_on(tracked, k, box):
    """The tracked centre at window frame k is accepted and lies inside the box."""
    if not tracked.good[k]:
        return False
    x, y, w, h = box
    return abs(tracked.cx[k] - (x + w / 2)) < w / 2 and abs(tracked.cy[k] - (y + h / 2)) < h / 2


def iou(a, b):
    overlap = intersection_area(a, b)
    return overlap / max(1, box_area(a) + box_area(b) - overlap)


def box_centre(box):
    return np.array([box[0] + box[2] / 2, box[1] + box[3] / 2])


def content_scroll(window, box, starts):
    """The phase-correlation measurement of an element whose content moved inside an outline that stayed (a carousel
    strip, a scrolled list), else None: a start element has the same box (IoU >= 0.9), the content moved >= 3 px with a
    median response >= 0.3, and the outline's centre moved at most a quarter of that. A rigid move shifts the outline
    as far as the content, so it never qualifies."""
    counterpart = max(starts, key=lambda other: iou(box, other), default=None)
    if counterpart is None or iou(box, counterpart) < SAME_OUTLINE_IOU:
        return None
    measure = track.measure_scroll(window.greys, box)
    travel = float(np.hypot(*measure.travel))
    if travel < CONTENT_SCROLL_MIN_PX or not is_scroll_like(measure):
        return None
    outline_shift = float(np.hypot(*(box_centre(box) - box_centre(counterpart))))
    return measure if outline_shift <= OUTLINE_SHIFT_SHARE * travel else None


def box_area(box):
    return box[2] * box[3]


def element_channels(origin, box, tracked, window, threshold):
    """(channels, types, axis, appears, disappears, grown) of one tracked element (spec 7.8.4). An element missing from
    the other rest frame fades in or out, unless it is opaque whenever seen and smaller toward that side: then it grew
    from (or shrank to) scale 0, and the gain step where the walk lost it is no fade."""
    geometry, axis, _ = track.geometry_channels(tracked, origin)
    appears = origin == "end" and track.is_absent(tracked, "end")
    disappears = origin == "start" and track.is_absent(tracked, "start")
    channels = list(geometry)
    types = [c.name for c in geometry]
    grown = None
    if appears or disappears:
        grown = from_zero_scale(channels, tracked, origin) if track.is_opaque_when_seen(tracked) else None
        if grown is None:
            channels.append(track.opacity_channel(tracked, origin))
            types.append("fade")
    if channels:
        return grown or channels, types, axis, appears, disappears, grown is not None
    blend, residual = track.blend_channel(window.images, window.rest, box)
    if blend is not None and residual < track.COLOR_RESIDUAL_P90:
        return [blend], ["color"], None, False, False, False
    return [track.energy_channel(window.images, box, threshold)], ["other"], None, False, False, False


def from_zero_scale(channels, tracked, origin):
    """The channels with the scale channel's unobserved rest state at scale 0, when the element was smaller than at
    its rest frame where it was last seen toward the other one; None otherwise. The frames beyond that one where it was
    missing keep the model below GROWN_VISIBLE_PX across, as hidden samples keep a slide behind its clip line: without
    them a slow-starting curve that began 38 ms early fitted a badge's first frames as well as the true one."""
    scale = next((c for c in channels if c.name == "scale"), None)
    if scale is None or scale.is_fixed:
        return None
    seen = np.flatnonzero(tracked.good)
    nearest_absent = seen[0] if origin == "end" else seen[-1]
    reference = scale.values[tracked.ref]
    if scale.values[nearest_absent] >= reference:
        return None
    zero = replace(scale, start=0.0) if origin == "end" else replace(scale, end=0.0)
    frames = np.arange(len(tracked.good))
    absent = (frames < seen[0] if origin == "end" else frames > seen[-1]) & (tracked.gain < track.LOST_GAIN)
    if absent.any():
        floor = GROWN_VISIBLE_PX / max(min(tracked.size), 1) * reference
        zero = replace(zero, hidden=absent, hidden_from=reference, hidden_travel=floor - reference)
    return [zero if c is scale else c for c in channels]


def median_hex(image, box):
    x, y, w, h = box
    b, g, r = (int(round(v)) for v in np.median(image[y:y + h, x:x + w].reshape(-1, 3), axis=0))
    return f"#{r:02x}{g:02x}{b:02x}"


def fit_element(channels, window, settings, seeds=()):
    """Joint fit of all channels (default), or the primary channel's own fit with --no-joint; plus per-channel fits.
    The per-channel fits get the free curve too: a named-only fit of one channel of a custom-curve animation lands on
    another start and duration by construction, which read as channels with their own timing. `seeds` (curve, t0, D)
    also start the joint free fit."""
    t = window.times
    own = []
    if len(channels) >= 2:
        own = [easing.fit_with_stalls(t, [c], settings.dt, settings.curves) for c in channels]
    if settings.is_joint or len(channels) == 1:
        return easing.fit_with_stalls(t, channels, settings.dt, settings.curves, seeds=seeds), own
    primary = next((fit for fit in own if fit is not None), None)
    return (None if primary is None else easing.spread_to(primary, t, channels)), own


def clipped_channels(candidate):
    """The translate channel of an element that crosses a clip line, read from the translation-only track. Its
    unobserved rest state is out of view: the hidden frames and the least travel that hides it (all but the part that
    showed inside the line) go with the channel; a rest state the walk found (a shadow fringe) is observed as it is.
    No fade: a clip line cuts the element, and measure_clip kept only elements at full contrast where they show."""
    geometry, axis, _ = track.geometry_channels(candidate.track, candidate.origin)
    translate = next((c for c in geometry if c.name == "translate"), None)
    if translate is None:
        return None
    built = ["translate"], axis, candidate.origin == "end", candidate.origin == "start"
    if translate.is_fixed:
        return [translate], *built
    clip = candidate.clip
    hidden_travel = float(clip.clip.hidden_vector(candidate.box, clip.shown_px) @ axis)
    observed = translate.end if translate.start is None else translate.start
    return [replace(translate, hidden=clip.hidden, hidden_from=observed, hidden_travel=hidden_travel)], *built


def fit_clipped(channels, window, settings, clip):
    """(fit, channels, reading) of an element hidden beyond a clip line at one rest state. A part of it that showed
    inside the line there places that rest state (seen). Otherwise it is read as starting (or ending) just beyond the
    line, the least travel its hidden frames allow, unless a longer hidden travel fits clearly better; then the travel
    stays free (at least that long) and the start state counts as not observed. The free travel's fit also seeds the
    at-edge one: where it sits at the least travel it is an at-edge solution, which a drawer closing under a long hidden
    tail reached (0.0001 RMSE) while the at-edge search, seeded by named curves 150 ms short, stopped at 0.009."""
    at_edge_channels = [c.at_hidden_edge() if c.hidden_travel is not None else c for c in channels]
    if clip.shown_px > 0:
        return fit_element(at_edge_channels, window, settings)[0], at_edge_channels, SEEN
    beyond, _ = fit_element(channels, window, settings)
    seeds = () if beyond is None else ((beyond.curve, beyond.start_s, beyond.duration_s),)
    at_edge, _ = fit_element(at_edge_channels, window, settings, seeds)
    if at_edge is not None and (beyond is None or at_edge.rmse <= EDGE_READING_RATIO * beyond.rmse + EDGE_READING_MARGIN):
        return at_edge, at_edge_channels, AT_EDGE
    return beyond, channels, BEYOND_EDGE


def build_clipped(candidate, window, settings):
    built = clipped_channels(candidate)
    if built is None:
        return None
    channels, types, axis, appears, disappears = built
    fit, channels, reading = fit_clipped(channels, window, settings, candidate.clip)
    if fit is None:
        return None
    return Element(candidate.origin, candidate.box, candidate.track, channels, types, fit, appears, disappears, axis,
                   clip=candidate.clip, clip_reading=reading)


def build_element(candidate, window, settings):
    if candidate.scroll is not None:
        return scroll_element(window, settings, candidate.box, candidate.scroll)
    if candidate.clip is not None:
        return build_clipped(candidate, window, settings)
    origin, box, tracked = candidate.origin, candidate.box, candidate.track
    if candidate.blend is not None:
        # measured over its box in every frame; the walk that lost it describes nothing of it (its shear read 0.07)
        channels, types, axis, appears, disappears, grown = [candidate.blend], ["color"], None, False, False, False
        tracked = None
    else:
        channels, types, axis, appears, disappears, grown = element_channels(origin, box, tracked, window,
                                                                             settings.pix_threshold)
    if not settings.is_joint and len(channels) >= 2:
        channels = sorted(channels, key=lambda c: CHANNEL_ORDER.index(c.name))
    fit, own = fit_element(channels, window, settings)
    if fit is None:
        return None
    colours = (median_hex(window.rest, box), median_hex(window.images[-1], box)) if types == ["color"] else None
    return Element(origin, box, tracked, channels, types, fit, appears, disappears, axis, own, colours=colours, grown=grown)


def end_candidate(window, view, rest, starts, settings):
    scroll = content_scroll(window, rest.box, [start.box for start in starts]) if settings.motion_mode == "auto" else None
    if scroll is not None:
        return Candidate("end", rest.box, scroll=scroll, extent=rest.extent)
    return Candidate("end", rest.box, rest_track(window, view, rest.box, len(window.images) - 1), extent=rest.extent)


def in_place_blend(candidate, window, starts):
    """The colour blend channel of an end element that changed its looks in place, else None: a start element has its
    outline (IoU >= SAME_OUTLINE_IOU), its walk lost it before the rest frame before, and the blend of the two rest
    frames explains its box. A ghost button filling (its label from blue on the page to white on blue) crossed over in
    contrast mid-way, and the walk read the fading inner edge of its border as a 4 to 7 % stretch and a fade."""
    if candidate.track is None or candidate.clip is not None or candidate.track.good[0]:
        return None
    if not any(iou(candidate.box, start.box) >= SAME_OUTLINE_IOU for start in starts):
        return None
    blend, residual = track.blend_channel(window.images, window.rest, candidate.box)
    return blend if blend is not None and residual < track.COLOR_RESIDUAL_P90 else None


def with_clip(candidate, window, settings, view):
    """The candidate re-tracked translation-only when it crosses a clip line on its way into or out of view. Its extent
    is measured against the line, a box-shadow included: that is what shows inside it at the hidden rest state. `view`
    is its rest frame's RestView."""
    if candidate.track is None:
        return candidate
    measured = track.measure_clip(window.images, window.greys, window.times, window.activity, candidate.extent,
                                  candidate.track, candidate.origin, settings.pix_threshold,
                                  template_mask(window, view, candidate.extent, candidate.track.ref))
    if measured is None:
        return candidate
    tracked, clip = measured
    return replace(candidate, box=candidate.extent, track=tracked, clip=clip)


def edge_strip(clip, box, boxes):
    """The box among the other rest frame's elements that is the part of a clipped element showing inside its line
    there: it reaches the line, lies inside the element's band across the motion axis and is at most three quarters of
    its size along it. The widest such box, or None."""
    axis, across = clip.axis, 1 - clip.axis
    band = (box[across] - NEST_SLACK_PX, box[across] + box[2 + across] + NEST_SLACK_PX)

    def is_strip(other):
        far_edge = other[axis] + other[2 + axis] if clip.sign > 0 else other[axis]
        return (abs(far_edge - clip.line) <= NEST_SLACK_PX and band[0] <= other[across]
                and other[across] + other[2 + across] <= band[1]
                and other[2 + axis] <= (1 - track.CLIP_MIN_CUT_SHARE) * box[2 + axis])
    return max((other for other in boxes if is_strip(other)), key=lambda other: other[2 + axis], default=None)


def with_strip(candidate, boxes):
    """A clipped candidate with the part of it that showed inside its line at the hidden rest state (a shadow fringe),
    found among that rest frame's element boxes: that part is no element of its own, and its size places the hidden
    rest state when the walk did not reach it."""
    measure = candidate.clip
    strip = None if measure is None else edge_strip(measure.clip, candidate.box, boxes)
    if strip is None:
        return candidate
    shown = measure.shown_px or float(strip[2 + measure.clip.axis])
    return replace(candidate, clip=replace(measure, shown_px=shown, strip=strip))


def segment_elements(window, settings):
    """Entrances and moves from the rest frame after, exits from the rest frame before (spec 7.8.3 step 5). Clip lines
    are measured first: the strip of a clipped element that shows inside its line at the other rest state is part of
    it, not an element of its own."""
    first, last = window.images[0], window.images[-1]
    before = rest_view(first, carried(window, last, -1, 0), window.activity)
    after = rest_view(last, carried(window, first, 0, -1), window.activity)
    starts = rest_elements(before, window.activity, settings.max_elements)
    finals = rest_elements(after, window.activity, settings.max_elements)
    ends = [with_strip(with_clip(end_candidate(window, after, rest, starts, settings), window, settings, after),
                       [start.extent for start in starts]) for rest in finals]
    ends = [replace(candidate, blend=in_place_blend(candidate, window, starts)) for candidate in ends]
    unexplained = [Candidate("start", rest.box, rest_track(window, before, rest.box, 0), extent=rest.extent)
                   for rest in starts if not any(explains(candidate, rest) for candidate in ends)]
    # an entrance already faintly visible in the rest frame before tracks forward onto its end element: not an exit
    exits = [c for c in unexplained if not any(lands_on(c.track, -1, end.box) for end in ends)]
    exits = [with_strip(with_clip(c, window, settings, before), [final.extent for final in finals])
             for c in exits[:max(0, settings.max_elements - len(ends))]]
    strips = {c.clip.strip for c in exits if c.clip is not None}
    found = [c for c in ends if c.extent not in strips] + exits
    return [c for c in found if not is_covered_content(c, found, window, settings.pix_threshold)]


def box_at(tracked, box, k):
    """`box` (x, y, w, h at the track's reference rest frame) at window frame k: moved with the tracked centre and scaled
    about it (rotation left out)."""
    ref, sx, sy = tracked.ref, tracked.sx[k], tracked.sy[k]
    return (tracked.cx[k] + (box[0] - tracked.cx[ref]) * sx, tracked.cy[k] + (box[1] - tracked.cy[ref]) * sy,
            box[2] * sx, box[3] * sy)


def occluder_boxes(candidate):
    """Per window frame, the candidate's extent where its track accepted the frame, else None. Past its tracked frames
    toward the rest frame it is missing from, the element is bounded. One crossing a clip line lies, until it is wholly
    beyond the line, between its nearest tracked box and the line: that box stretched through the line (a drawer closing
    kept covering the page just above the frame edge after the walk had lost its last 40 px). One absent from that rest
    frame without a clip line faded or shrank there within its nearest tracked box (a badge growing over an avatar was
    too small to track in its first frames). A frame the walk rejected between two it accepted holds the element
    between their boxes, within their union: a shade's walk rejected one frame midway, and three text lines it covered
    there read as moving on their own."""
    tracked, extent = candidate.track, candidate.extent or candidate.box
    boxes = [box_at(tracked, extent, k) if tracked.good[k] else None for k in range(len(tracked.good))]
    good = np.flatnonzero(tracked.good)
    for k in range(good[0] + 1, good[-1]) if good.size else ():
        if boxes[k] is None:
            boxes[k] = union_box(boxes[good[good < k][-1]], boxes[good[good > k][0]])
    is_absent = track.is_absent(tracked, candidate.origin)
    if not good.size or (candidate.clip is None and not is_absent):
        return boxes
    beyond = range(good[-1] + 1, len(boxes)) if candidate.origin == "start" else range(0, good[0])
    nearest = boxes[good[-1]] if candidate.origin == "start" else boxes[good[0]]
    absent_at = 0 if candidate.origin == "end" else len(boxes) - 1
    for k in beyond:
        if candidate.clip is None:
            boxes[k] = None if k == absent_at else nearest
        else:
            boxes[k] = None if candidate.clip.hidden[k] else through_line(nearest, candidate.clip.clip.side)
    return boxes


def union_box(a, b):
    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
    return x0, y0, max(a[0] + a[2], b[0] + b[2]) - x0, max(a[1] + a[3], b[1] + b[3]) - y0


def through_line(box, side):
    """The box stretched without end toward `side`."""
    x, y, w, h = box
    far = 1e9
    return {"right": (x, y, far, h), "left": (x - far, y, w + far, h), "bottom": (x, y, w, far),
            "top": (x, y - far, w, h + far)}[side]


def cover_mask(box, region, slack):
    """Boolean mask over `region` (x, y, w, h) of what a float box, grown by `slack` px on every side, covers."""
    x, y, w, h = region
    x0, x1 = (int(np.clip(v - x, 0, w)) for v in (np.floor(box[0] - slack), np.ceil(box[0] + box[2] + slack)))
    y0, y1 = (int(np.clip(v - y, 0, h)) for v in (np.floor(box[1] - slack), np.ceil(box[1] + box[3] + slack)))
    mask = np.zeros((h, w), bool)
    mask[y0:y1, x0:x1] = True
    return mask


def changes_from_rest(candidate, window, threshold):
    """Per window frame: the candidate box's pixels that differ from its reference rest frame (under the frame's share
    of the scrim) by more than `threshold`."""
    x, y, w, h = candidate.box
    ref = candidate.track.ref
    reference = window.images[ref][y:y + h, x:x + w]
    return [pixels.channel_max(cv2.absdiff(image[y:y + h, x:x + w], carried(window, reference, ref, k))) > threshold
            for k, image in enumerate(window.images)]


def is_covered_content(candidate, found, window, threshold):
    """Page content that another element slides over or uncovers, not motion of its own: that element's extent reaches it
    at its other rest frame but not at its own, and every change in its box over the window beyond codec noise lies inside
    that element's extent of the frame. Text lines a sheet covered shrank toward its edge and read as scale to 0, fades and
    colour changes, one row each, with a stagger between them; the codec changed a line 6 px from the sheet's edge by 17."""
    if candidate.track is None:
        return False
    changes = changes_from_rest(candidate, window, max(threshold, rest_noise(window)))
    limit = OCCLUDED_OUTSIDE_SHARE * box_area(candidate.box)
    ref = candidate.track.ref
    other_ref = len(changes) - 1 if ref == 0 else 0
    for other in found:
        if other is candidate or other.track is None:
            continue
        covers = [None if box is None else cover_mask(box, candidate.box, OCCLUSION_SLACK_PX)
                  for box in occluder_boxes(other)]
        if (covers[ref] is not None and covers[ref].any()) or covers[other_ref] is None or not covers[other_ref].any():
            continue
        if all((changed if cover is None else changed & ~cover).sum() <= limit for changed, cover in zip(changes, covers)):
            return True
    return False


def needs_fallback(found, window):
    """0 elements, or one tracked element that covers > 80 % of the activity box and does not track as one rigid
    thing. An element crossing a clip line was already found rigid and opaque (measure_clip), and its frames behind
    the line are not frames it failed to track; a colour change in place is measured over its box, not tracked."""
    if not found:
        return True
    if len(found) > 1 or found[0].scroll is not None or found[0].clip is not None or found[0].blend is not None:
        return False
    covered = intersection_area(found[0].box, window.activity_box) / max(1, box_area(window.activity_box))
    return covered > FALLBACK_COVER and found[0].track.visible_tracked_frac() < RIGID_TRACKED


def intersection_area(a, b):
    w = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    h = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return w * h


def scroll_element(window, settings, box, measure):
    """Scroll / carousel element: the integrated phase correlation inside the box as one translate channel."""
    channel, axis = track.scroll_channel(measure)
    fit = easing.fit_with_stalls(window.times, [channel], settings.dt, settings.curves)
    if fit is None:
        return None
    return Element("end", box, None, [channel], ["translate"], fit, axis=axis, scroll=measure)


def is_scroll_like(measure):
    """Auto mode: the region moves as content (median response >= 0.3 over moving frames, travel >= 1 px)."""
    moving = measure.is_moving()
    if not moving.any() or np.hypot(*measure.travel) < SCROLL_MIN_TRAVEL_PX:
        return False
    return float(np.median(measure.responses[moving])) >= track.SCROLL_MIN_RESPONSE


def activity_element(window, settings):
    channel = track.energy_channel(window.images, window.activity_box, settings.pix_threshold)
    fit = easing.fit_with_stalls(window.times, [channel], settings.dt, settings.curves)
    if fit is None:
        return None
    element = Element("end", window.activity_box, None, [channel], ["other"], fit)
    element.notes.append("segmentation fell back to the activity box: rerun with --roi around one element")
    return element


def region_scroll(window, settings, measure):
    element = scroll_element(window, settings, window.activity_box, measure)
    return "scroll", "activity-bbox", [element] if element else []


def backdrop_element(window, settings):
    """The scrim as an element of its own, or None: an overlay over the whole crop whose opacity follows each frame's
    measured share of the page's dimming (or clearing)."""
    scrim = window.scrim
    if scrim is None:
        return None
    appears = scrim.is_dimming_in
    values = scrim.progress if appears else 1 - scrim.progress
    channel = easing.Channel("opacity", values, np.ones_like(values), *((0.0, 1.0) if appears else (1.0, 0.0)))
    fit = easing.fit_with_stalls(window.times, [channel], settings.dt, settings.curves)
    if fit is None:
        return None
    element = Element("end" if appears else "start", scrim.box, None, [channel], ["fade"], fit, appears, not appears,
                      is_backdrop=True)
    alpha, (b, g, r) = scrim.overlay()
    element.notes.append(BACKDROP_NOTE.format(
        colour=f"#{int(round(r)):02x}{int(round(g)):02x}{int(round(b)):02x}", alpha=alpha * 100,
        action="fades in" if appears else "clears", gain=scrim.gain, share=scrim.share * 100))
    return element


def analyse_window(window, settings):
    """(mode, segmentation, elements) for one event; mode is scroll when an element is measured as a scroll. A scrim
    is listed as an element of its own next to what moved over it."""
    if settings.motion_mode == "scroll":
        return region_scroll(window, settings, track.measure_scroll(window.greys, window.activity_box))
    found = segment_elements(window, settings)
    backdrop = backdrop_element(window, settings)
    if backdrop is not None and not found:
        return "elements", "rest-frame", [backdrop]
    if needs_fallback(found, window):
        measure = track.measure_scroll(window.greys, window.activity_box)
        if settings.motion_mode == "auto" and is_scroll_like(measure):
            return region_scroll(window, settings, measure)
        element = activity_element(window, settings)
        return "activity-bbox", "activity-bbox", [element] if element else []
    built = [e for e in (build_element(candidate, window, settings) for candidate in found) if e is not None]
    built += [backdrop] if backdrop is not None else []
    return ("scroll" if any(e.scroll is not None for e in built) else "elements"), "rest-frame", built


def visible_span(element, t):
    """(start_visible_s, end_visible_s): first sample showing change and the sample where the last change lands."""
    in_between = np.zeros(len(t), bool)
    reliable = np.zeros(len(t), bool)
    for index, channel in enumerate(element.channels):
        progress = element.fit.progress(channel, index)
        amplitude = abs(element.fit.amplitudes[index])
        tolerance = VISIBLE_STEP_PX / max(amplitude, 1e-9) if channel.is_px else VISIBLE_PROGRESS
        in_between |= channel.reliable & (np.abs(progress) > tolerance) & (np.abs(1 - progress) > tolerance)
        reliable |= channel.reliable
    indices = easing.main_run(in_between, reliable)
    if not indices.size:
        return element.fit.start_s, element.fit.start_s + element.fit.duration_s
    return float(t[indices[0]]), float(t[min(indices[-1] + 1, len(t) - 1)])


def primary_index(element):
    return min(range(len(element.channels)), key=lambda i: CHANNEL_ORDER.index(element.channels[i].name))


def curve_max_slope(curve):
    return float(np.max(np.abs(np.gradient(curve.table, easing.DENSE_X))))


def geometry_record(element, window):
    """Fitted and observed geometry in source px (css px are added later from --dpr)."""
    record = {"translate_px": [0.0, 0.0], "translate_observed_px": [0.0, 0.0], "translate_observed": False,
              "scale_from": 1.0, "scale_to": 1.0, "rotate_deg": 0.0, "shear_max": 0.0, "peak_speed_px_s": 0.0}
    fit = element.fit
    for index, channel in enumerate(element.channels):
        amplitude = fit.amplitudes[index]
        if channel.name in ("translate", "scroll"):
            vector = window.geometry.vector_to_source(element.axis * amplitude)
            record["translate_px"] = [round(float(v), 1) + 0.0 for v in vector]
            record["translate_observed"] = channel.is_fixed and element.clip_reading in (None, SEEN)
            record["peak_speed_px_s"] = round(float(np.hypot(*vector)) * curve_max_slope(fit.curve) / fit.duration_s, 1)
            reliable = channel.values[channel.reliable]
            if reliable.size:
                far = reliable[np.argmax(np.abs(reliable - channel.anchor[0]))] - channel.anchor[0]
                observed = far if channel.anchor[1] == 0.0 else -far
                record["translate_observed_px"] = [round(float(v), 1) + 0.0 for v in window.geometry.vector_to_source(element.axis * observed)]
        elif channel.name == "scale":
            anchor, e_anchor = channel.anchor
            start_value = anchor - amplitude * e_anchor
            reference = channel.values[element.track.ref]
            record["scale_from"] = round(float(start_value / reference), 4)
            record["scale_to"] = round(float((start_value + amplitude) / reference), 4)
        elif channel.name == "rotate":
            record["rotate_deg"] = round(float(amplitude), 2)
    if element.track is not None and element.track.good.any():
        record["shear_max"] = round(float(np.max(np.abs(element.track.shear[element.track.good]))), 4)
    return record


def timing_record(element, window, settings):
    fit, t = element.fit, window.times
    start_visible, end_visible = visible_span(element, t)
    inside = (t >= fit.start_s) & (t <= fit.start_s + fit.duration_s)
    stalled = fit.stalled if fit.stalled is not None else np.zeros(len(t), bool)
    start_range = None if fit.start_range_s is None else [motion_time(v) for v in fit.start_range_s]
    # change energy is a proxy of the motion, not its progress: no fit range bounds its duration (a dot's settle and
    # codec changes far from it read 322 ms, range 225 to 354, for 220)
    is_proxy = element.channels[primary_index(element)].name == "energy"
    duration_range = None if is_proxy else [round(v * 1000, 1) for v in fit.duration_range_s]
    return {"start_s": motion_time(fit.start_s), "start_s_range": start_range, "start_visible_s": motion_time(start_visible),
            "end_s": motion_time(fit.start_s + fit.duration_s), "end_visible_s": motion_time(end_visible),
            "duration_ms": round(fit.duration_s * 1000, 1), "duration_visible_ms": round((end_visible - start_visible) * 1000, 1),
            "duration_ms_range": duration_range, "t50_s": motion_time(fit.t50_s),
            "resolution_ms": round(settings.dt * 1000, 2), "stalled_frames_s": [motion_time(v) for v in t[stalled]],
            "dropped_frame_ratio": round(float(stalled.sum() / max(1, inside.sum())), 3),
            "instant": is_instant(element)}


def is_instant(element):
    """The primary channel changes from rest to rest within INSTANT_INTERVALS frame intervals, or from 10 % to 90 %
    within one (encoder drift shifted the rest before a slide swap by 1.6 px). A slide swap measured 1 interval; no
    easing or duration can be measured from it. Secondary channels are left out: ECC reads 1 % scale noise on a
    swapped slide."""
    index = primary_index(element)
    channel = element.channels[index]
    if not channel.is_fixed:
        return False
    progress = element.fit.progress(channel, index)
    amplitude = abs(element.fit.amplitudes[index])
    rest = VISIBLE_STEP_PX / max(amplitude, 1e-9) if channel.is_px else VISIBLE_PROGRESS
    return (change_intervals(progress, channel.reliable, rest) <= INSTANT_INTERVALS
            or change_intervals(progress, channel.reliable, INSTANT_BULK) <= 1)


def change_intervals(progress, reliable, tolerance):
    """Frame intervals from the last reliable sample within `tolerance` of the start state to the first one within
    `tolerance` of the end state after it; infinite when either is missing."""
    at_start = np.flatnonzero(reliable & (np.abs(progress) <= tolerance))
    at_end = np.flatnonzero(reliable & (np.abs(1 - progress) <= tolerance))
    if not at_start.size or not at_end.size or at_end[-1] < at_start[0]:
        return np.inf
    first_end = at_end[at_end > at_start[0]][0]
    return int(first_end - at_start[at_start < first_end][-1])


def easing_record(element, linear):
    fit = element.fit
    record = {"name": fit.curve.name, "aliases": list(fit.curve.aliases),
              "cubic_bezier": [round(v, 3) for v in fit.curve.bezier], "source": fit.source,
              "fit_rmse": round(fit.rmse, 4),
              "free_fit": None if fit.free is None else {"cubic_bezier": [round(v, 3) for v in fit.free["cubic_bezier"]],
                                                         "rmse": round(fit.free["rmse"], 4)},
              "near_ties": fit.near_ties, "shape_tie": fit.shape_tie, "overshoot": linear is not None, "css_linear": None,
              "css_linear_timing": None}
    if linear is not None:
        record["css_linear"] = linear["text"]
        record["css_linear_timing"] = {"start_s": motion_time(linear["start_s"]),
                                       "duration_ms": round(linear["duration_s"] * 1000, 1), "rmse": round(linear["rmse"], 4)}
    return record


def linear_easing(element, window):
    """Overshooting motion: linear() over the measured span. Start where the first moving samples extrapolate to
    0 (the fitted cubic's start when that fails), end where the last change lands. None when there is no overshoot."""
    fit, t = element.fit, window.times
    index = primary_index(element)
    channel = element.channels[index]
    progress = fit.progress(channel, index)
    if not easing.overshoots(progress, channel.weights):
        return None
    dt = window_dt(t)
    start_visible, end_visible = visible_span(element, t)
    start = easing.extrapolated_start(t, progress, channel.reliable, start_visible - dt)
    if start is None:
        start = min(max(fit.start_s, start_visible - dt), start_visible)
    duration = max(end_visible - start, dt)
    text, stops_x, stops_y, rmse = easing.css_linear(t, progress, channel.weights, start, duration)
    x50 = float(np.interp(0.5, np.maximum.accumulate(stops_y), stops_x))
    return {"text": text, "start_s": float(start), "duration_s": float(duration), "rmse": rmse,
            "t50_s": float(start + x50 * duration), "dt": dt}


def use_linear_timing(record, linear):
    """A cubic-bezier cannot follow this motion (overshoot, RMSE >= 0.01): timing and easing come from linear()."""
    start, duration = linear["start_s"], linear["duration_s"]
    record["timing"].update(start_s=motion_time(start), end_s=motion_time(start + duration), start_s_range=None,
                            duration_ms=round(duration * 1000, 1), t50_s=motion_time(linear["t50_s"]),
                            duration_ms_range=[round((duration - linear["dt"]) * 1000, 1), round((duration + linear["dt"]) * 1000, 1)])
    record["easing"].update(name="linear()", aliases=[], source="measured-linear", fit_rmse=round(linear["rmse"], 4))


def window_dt(t):
    steps = np.diff(t)
    return float(np.median(steps)) if steps.size else 0.0


def channels_record(element, window, split):
    """Each channel as a property of the element under its one shared fit; own fits only when a channel split off."""
    records = {}
    for index, channel in enumerate(element.channels):
        amplitude = element.fit.amplitudes[index]
        record = {"rmse": round(element.fit.channel_rmse[index], 4), "amplitude_fixed": channel.is_fixed}
        if channel.name in ("translate", "scroll"):
            record["amplitude_px"] = round(float(np.hypot(*window.geometry.vector_to_source(element.axis * amplitude))), 1)
        elif channel.name in ("opacity", "color", "energy"):
            anchor, e_anchor = channel.anchor
            start_value = anchor - amplitude * e_anchor
            record.update({"from": round(float(start_value), 3), "to": round(float(start_value + amplitude), 3)})
            if channel.name == "color" and element.colours:
                record.update({"from_hex": element.colours[0], "to_hex": element.colours[1]})
        else:
            record["amplitude"] = round(float(amplitude), 4)
        own = element.own_fits[index] if index < len(element.own_fits) else None
        if own is not None and split is not None:
            record["own_fit"] = {"name": own.curve.name, "cubic_bezier": [round(v, 3) for v in own.curve.bezier],
                                 "start_s": motion_time(own.start_s), "duration_ms": round(own.duration_s * 1000, 1),
                                 "rmse": round(own.rmse, 4)}
        records[channel.name] = record
    return records


def split_channel(element, dt):
    """The channel one shared timing clearly misses, or None. Channels share one CSS animation unless one of them,
    under the shared fit, misses its data by more than SPLIT_RATIO x its own fit + SPLIT_MARGIN while its own start
    differs by more than a frame or its own duration by more than 10 %. Returns {channel, shared_rmse, own_rmse}."""
    fit, worst = element.fit, None
    for index, (channel, own) in enumerate(zip(element.channels, element.own_fits)):
        if own is None:
            continue
        is_apart = (abs(own.start_s - fit.start_s) > TIMING_DIFFERS_FRAMES * dt
                    or abs(own.duration_s / fit.duration_s - 1) > TIMING_DIFFERS_DURATION)
        shared = fit.channel_rmse[index]
        excess = shared - (SPLIT_RATIO * own.rmse + SPLIT_MARGIN)
        if is_apart and excess > 0 and (worst is None or excess > worst[0]):
            worst = (excess, {"channel": channel.name, "shared_rmse": round(shared, 4), "own_rmse": round(own.rmse, 4)})
    return None if worst is None else worst[1]


def tracked_fraction(element, t):
    """Accepted share of the window frames in which the fit says the element is visible (a staggered card is not
    expected to be tracked before it appears)."""
    if element.track is not None:
        shown = element.fit.easing(t)
        expected = shown >= VISIBLE_OPACITY if element.appears else shown <= 1 - VISIBLE_OPACITY if element.disappears else None
        good = element.track.good if expected is None or not expected.any() else element.track.good[expected]
        return float(good.mean())
    if element.scroll is not None:
        return float(element.scroll.reliable.mean())
    # a backdrop and a colour change in place are measured over their box in every frame
    return 1.0 if element.is_backdrop or element.types == ["color"] else 0.0


def confidence(element, segmentation, tracked_frac, scroll_unreliable, near_ties, dt, is_instant_change):
    """high / medium / low (spec 7.8.8)."""
    fit = element.fit
    is_start_unknown = not any(c.is_fixed for c in element.channels)
    is_bbox_fallback = segmentation == "activity-bbox" and element.scroll is None
    if (is_bbox_fallback or "other" in element.types or (scroll_unreliable or 0.0) > LOW_SCROLL_UNRELIABLE
            or is_start_unknown or is_instant_change):
        return "low"
    low, high = fit.duration_range_s
    is_range_tight = low >= (1 - HIGH_RANGE) * fit.duration_s and high <= (1 + HIGH_RANGE) * fit.duration_s
    is_start_firm = fit.start_range_s is None or fit.start_range_s[1] - fit.start_range_s[0] <= dt * 1.01
    checks = (tracked_frac >= HIGH_TRACKED, fit.rmse < HIGH_RMSE, is_range_tight, is_start_firm)
    is_weak_name = len(element.channels) == 1 and element.channels[0].is_fixed and bool(near_ties)
    is_start_assumed = element.clip_reading == AT_EDGE or element.grown
    is_unnamed = fit.shape_tie
    return "high" if all(checks) and not (is_weak_name or is_start_assumed or is_unnamed) else "medium"


def clip_record(element, window):
    """Where the element crossed its clip line, when it was out of view and the least travel that hid it (source px):
    the part seen moving plus the hidden part, which follows from its full size at the visible rest state. A rest
    state seen as a strip inside the line gives the travel itself."""
    measure = element.clip
    if measure is None:
        return None
    clip, t, geometry = measure.clip, window.times, window.geometry
    channel = element.channels[0]
    length = lambda value: round(float(np.hypot(*geometry.vector_to_source(element.axis * value))), 1)
    seen, hidden = t[element.track.good], t[measure.hidden]
    before, after = hidden[hidden < seen[0]], hidden[hidden > seen[-1]]
    fitted = length(abs(element.fit.amplitudes[0]))
    return {"edge": clip.side, "container": "frame" if clip.is_frame_edge else "inner",
            "line_src_px": round(geometry.offset[clip.axis] + clip.line / geometry.scale[clip.axis], 1),
            "seen_s": [motion_time(seen[0]), motion_time(seen[-1])],
            "hidden_until_s": motion_time(before[-1]) if element.origin == "end" and before.size else None,
            "hidden_from_s": motion_time(after[0]) if element.origin == "start" and after.size else None,
            "shown_px": round(measure.shown_px / geometry.scale[clip.axis], 1),
            "travel_min_px": fitted if element.clip_reading == SEEN else length(abs(channel.hidden_travel)),
            "visible_travel_px": length(channel.observed_range), "travel_fitted_px": fitted, "reading": element.clip_reading}


def clip_note(record):
    clip = record["clip"]
    where = (f"the {clip['edge']} edge of the frame" if clip["container"] == "frame" else
             f"the {clip['edge']} edge of a clipping container (line at {clip['line_src_px']:g} source px)")
    is_entrance = record["origin"] == "end"
    rest = "started" if is_entrance else "ended"
    if clip["reading"] == SEEN:
        crossing = f"entered from beyond {where}" if is_entrance else f"left past {where}"
        return (f"{crossing}; {CLIPPED_READING}; it {rest} with a {clip['shown_px']:g} source px strip of it (a shadow or "
                f"its edge) still inside that edge, which places that state: travel {clip['travel_fitted_px']:g} source px, "
                "measured")
    if is_entrance:
        bound = f"hidden until {clip['hidden_until_s']:.4f} s, " if clip["hidden_until_s"] is not None else ""
        crossing = f"entered from beyond {where}: {bound}first seen {clip['seen_s'][0]:.4f} s"
    else:
        bound = f", hidden from {clip['hidden_from_s']:.4f} s" if clip["hidden_from_s"] is not None else ""
        crossing = f"left past {where}: last seen {clip['seen_s'][1]:.4f} s{bound}"
    hidden = round(clip["travel_min_px"] - clip["visible_travel_px"], 1)
    text = (f"{crossing}; {CLIPPED_READING}; travel at least {clip['travel_min_px']:g} source px "
            f"({clip['visible_travel_px']:g} seen moving + {hidden:g} hidden, from its full size)")
    if clip["reading"] == AT_EDGE:
        return text + f"; start, duration and easing assume it {rest} just beyond the edge, the least travel the hidden frames allow"
    return text + (f"; it {rest} farther out ({clip['travel_fitted_px']:g} source px fitted; a rest state just beyond the "
                   "edge fits clearly worse): that state was not observed, do not quote the easing")


def split_note(record):
    """Why the element is listed per channel, and each channel's own timing."""
    split = record["split"]
    parts = [f"{name} {own['start_s']:.4f} s {own['duration_ms']:.0f} ms "
             + (own["name"] if own["name"] != "custom" else "cubic-bezier(" + ",".join(f"{v:g}" for v in own["cubic_bezier"]) + ")")
             for name, own in ((n, c["own_fit"]) for n, c in record["channels"].items() if "own_fit" in c)]
    return (f"channels split: one shared timing misses {split['channel']} (rmse {split['shared_rmse']:.4f} shared vs "
            f"{split['own_rmse']:.4f} on its own), so each keeps its own: " + "; ".join(parts))


def element_notes(element, record, dt):
    notes = list(element.notes)
    timing = record["timing"]
    if timing["instant"]:
        notes.append(f"instant: the whole change lands within {INSTANT_INTERVALS} frame intervals (a swap or cut); "
                     "no easing or duration can be measured")
    if record["clip"]:
        notes.append(clip_note(record))
    elif element.grown:
        notes.append(GROWN_NOTE.format(state="started" if element.origin == "end" else "ended"))
    elif not any(c.is_fixed for c in element.channels):
        notes.append(START_UNKNOWN_NOTE)
    # a split element's rows show each channel's own fit: notes on the shared fit's curve and start would contradict them
    is_shared_shown = not record["split"]
    start_range = timing["start_s_range"] if is_shared_shown else None
    if start_range and start_range[1] - start_range[0] > dt * 1.01:
        notes.append(f"start uncertain: curves starting {start_range[0]:.4f} to {start_range[1]:.4f} s fit as well "
                     f"(a flat start trades against the start time); t50 {timing['t50_s']:.4f} s is firmer")
    rival = element.fit.rival if is_shared_shown else None
    if rival:
        held = "; the start and duration ranges hold its reading" if rival["is_in_range"] else ""
        notes.append(f"{rival['name']} fits almost as well (rmse {rival['rmse']:.4f}, {rival['duration_s'] * 1000:.0f} ms) but "
                     f"starts {(rival['start_s'] - element.fit.start_s) * 1000:+.0f} ms apart and differs in shape: reported "
                     f"as custom{held}")
    if record["split"]:
        notes.append(split_note(record))
    if record["timing"]["stalled_frames_s"]:
        notes.append(f"{len(record['timing']['stalled_frames_s'])} stalled frame(s) left out of the fit")
    ties = record["easing"]["near_ties"] if is_shared_shown else []
    if ties and record["easing"]["shape_tie"]:
        notes.append(f"named curves tie: {', '.join(t['name'] for t in ties)} fit within {NEAR_TIE_RATIO_TEXT} of each "
                     f"other although their shapes differ (up to {max(t['curve_dist'] for t in ties):g} from the reported "
                     "curve), so this recording cannot name the curve: the free fit is reported; quote it with these "
                     "names as equally likely")
    elif ties:
        notes.append(f"curve cannot be told apart from {', '.join(t['name'] for t in ties)} in this recording")
    if record["geometry"]["shear_max"] > 0.05:
        notes.append("shear: perspective or 3D transform")
    return notes


def element_record(element, number, event_id, window, settings, segmentation):
    """The analysis.json element (spec 8); css px fields are filled from --dpr after the cache."""
    tracked_frac = tracked_fraction(element, window.times)
    linear = linear_easing(element, window)
    scroll_unreliable = None
    if element.scroll is not None:
        scroll_unreliable = element.scroll.unreliable_frac(element.scroll.is_moving())
    record = {
        "id": f"{event_id}.{number}", "bbox_src": window.geometry.box_to_source(element.box),
        "origin": element.origin, "appears": bool(element.appears), "disappears": bool(element.disappears),
        "types": element.types, "geometry": geometry_record(element, window),
        "timing": timing_record(element, window, settings), "easing": easing_record(element, linear),
        "clip": clip_record(element, window), "tracked_frac": round(tracked_frac, 3),
    }
    split = split_channel(element, settings.dt)
    record.update(channels=channels_record(element, window, split), property_timing_differs=split is not None,
                  split=split)
    record["confidence"] = confidence(element, segmentation, tracked_frac, scroll_unreliable, record["easing"]["near_ties"],
                                      settings.dt, record["timing"]["instant"])
    uses_linear = linear is not None and element.fit.rmse >= HIGH_RMSE
    if uses_linear:
        use_linear_timing(record, linear)
    record["notes"] = element_notes(element, record, settings.dt) + ([LINEAR_TIMING_NOTE] if uses_linear else [])
    return record
