"""Easing fit (spec 7.8.5), numpy only.

A channel is one measured series of an element: position along its motion axis, scale, angle, opacity or colour
blend. Every channel of an element shares one model,

    value(t) = anchor + b * (E((t - t0) / D) - e_anchor)

where the anchor is a rest state that was observed (the value at the reference rest frame; both rest states for
opacity and colour), E is a CSS cubic-bezier, and b is the amplitude: fixed when both rest states are known,
otherwise closed-form weighted least squares capped at 1.5 x the observed range. Named curves are tried first
on a (t0, D) grid, the best three are refined, a free 6-parameter fit runs, and the named curve is kept unless
the free curve is clearly better, the two disagree in shape and start, or named curves of different shapes tie.

A custom curve's flat start trades against its start time (a y1 < 0 dip absorbed 49 ms of rest), so the free
fit keeps y inside [0, 1] unless the data under- or overshoots, and its start is probed a few frames either way:
the starts and durations that fit as well become its ranges.
"""
import functools
import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from . import REFERENCE_DIR
from .errors import EXIT_BAD_ARGS, VlError

NAMED_TABLE = REFERENCE_DIR / "easings.json"
DENSE_X = np.linspace(0.0, 1.0, 2049)       # curves are tabulated here once; lookups interpolate
DENSE_LAST = len(DENSE_X) - 1
NEWTON_STEPS = 8
BISECTION_STEPS = 20
CONVERGED = 1e-7
MOVING_PROGRESS = 0.02          # a sample moves when its progress is this far from both rest states
MOVING_GAP = 2                  # samples; back curves cross their end value inside the motion
RELIABLE_WEIGHT = 0.05
T0_POINTS, D_POINTS, RANGE_D_POINTS, RANGE_T0_POINTS = 31, 61, 131, 41
D_SPAN = (0.6, 1.8)             # x the moving span D0
NAMED_REFINED = 3
FREE_SEEDS = 3
SEED_SPREAD = 0.1               # free-fit seeds differ this much (max |dy|): one family's seeds find only that family
NAMED_REFINE_ITERS = 200
NAMED_REFINE_STEP = 0.3         # x frame interval
FREE_ITERS = 800
FREE_STEP = 0.04
FREE_SKIP_RMSE = 0.002
FLAT_END_SPAN = 0.15            # a free y2 this close to 1 is tried on it (with_flat_end)
CHOOSE_RATIO, CHOOSE_MARGIN = 1.25, 0.004
NEAR_TIE_DIST, NEAR_TIE_RATIO = 0.05, 1.25
TIE_SCREEN_RATIO, TIE_REFINED_MAX = 1.5, 8     # grid rows refined when looking for named curves that tie in RMSE
TELL_APART_MARGIN = 0.0005      # a channel alone prefers a curve when the other's RMSE exceeds 1.25 x its own + this
RANGE_RATIO, RANGE_MARGIN = 1.25, 0.002
AMPLITUDE_CAP = 1.5             # without it the amplitude of an unobserved start ran off to -449,933 px
PENALTY = 9.0
STALL_PASSES = 3
STALL_PX = (0.2, 1.0)           # measured step below, predicted step above: px channels
STALL_PROGRESS = (0.005, 0.02)  # the same in progress units: every other channel
LINEAR_STOPS = 21
OVERSHOOT_PROGRESS = 1.02
BEZIER_X_RANGE = (0.0, 1.0)
BEZIER_Y_RANGE = (-1.0, 2.0)
UNDERSHOOT_PROGRESS = -0.02
PROBE_FRAMES = (-3, -2, -1, 1, 2, 3)
PROBE_STEP = 0.05                # x the fitted duration, each way, until one fits worse or leaves the search box
PROBE_ITERS = 400


def bezier_coefficients(p1, p2):
    c = 3 * p1
    b = 3 * (p2 - p1) - c
    return 1 - c - b, b, c


def bezier_y(x, x1, y1, x2, y2):
    """y at progress x of cubic-bezier(x1, y1, x2, y2): 8 Newton steps on x(s), then bisection where needed."""
    x = np.clip(np.asarray(x, float), 0, 1)
    ax, bx, cx = bezier_coefficients(np.asarray(x1, float), np.asarray(x2, float))
    ay, by, cy = bezier_coefficients(np.asarray(y1, float), np.asarray(y2, float))
    s = x + 0 * ax
    for _ in range(NEWTON_STEPS):
        fx = ((ax * s + bx) * s + cx) * s - x
        slope = (3 * ax * s + 2 * bx) * s + cx
        usable = np.abs(slope) > 1e-6
        s = np.clip(np.where(usable, s - fx / np.where(usable, slope, 1), s), 0, 1)
    fx = ((ax * s + bx) * s + cx) * s - x
    if np.max(np.abs(fx), initial=0.0) > CONVERGED:
        low, high = np.zeros_like(s), np.ones_like(s)
        for _ in range(BISECTION_STEPS):
            fx = ((ax * s + bx) * s + cx) * s - x
            high = np.where(fx > 0, s, high)
            low = np.where(fx <= 0, s, low)
            s = np.where(np.abs(fx) > CONVERGED, (low + high) / 2, s)
    return ((ay * s + by) * s + cy) * s


@dataclass(frozen=True)
class Curve:
    name: str                       # "custom" for a free fit
    bezier: tuple
    aliases: tuple = ()
    table: np.ndarray = field(default=None, repr=False, compare=False)

    def at(self, x):
        """Linear interpolation of the table; the grid is uniform, so no search (np.interp was half the fit time)."""
        position = np.clip(np.asarray(x, float), 0.0, 1.0) * DENSE_LAST
        index = np.minimum(position.astype(np.int64), DENSE_LAST - 1)
        low = self.table[index]
        return low + (position - index) * (self.table[index + 1] - low)

    def x_at_half(self):
        """First x where the curve reaches 0.5 (back curves cross it once on the way up)."""
        index = int(np.argmax(self.table >= 0.5))
        if index == 0:
            return 0.0
        y0, y1 = self.table[index - 1], self.table[index]
        return float(DENSE_X[index - 1] + (0.5 - y0) / (y1 - y0 + 1e-12) * (DENSE_X[index] - DENSE_X[index - 1]))


def make_curve(name, bezier, aliases=()):
    bezier = tuple(float(v) for v in bezier)
    return Curve(name, bezier, tuple(aliases), bezier_y(DENSE_X, *bezier))


def curve_distance(a, b):
    """max |dy| over x in [0, 1]: the spec's measure of how alike two curves are."""
    return float(np.max(np.abs(a.table - b.table)))


@functools.cache
def named_curves(path=None):
    """The named table (reference/easings.json, or --easings FILE in the same format)."""
    source = Path(path) if path else NAMED_TABLE
    try:
        rows = json.loads(source.read_text())["curves"]
        curves = tuple(make_curve(row["name"], row["bezier"], row.get("aliases", ())) for row in rows)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise VlError(EXIT_BAD_ARGS, f"cannot read the easing table {source}: {error}",
                      "Pass a JSON file with {\"curves\": [{\"name\", \"bezier\": [x1, y1, x2, y2]}]}") from None
    bad = [c.name for c in curves if len(c.bezier) != 4 or not all(0 <= v <= 1 for v in c.bezier[::2])]
    if bad or not curves:
        raise VlError(EXIT_BAD_ARGS, f"easing table {source}: x1 and x2 must lie in [0, 1] ({', '.join(bad) or 'no curves'})",
                      "Fix the table")
    return curves


def table_digest(path):
    """Content hash of the easing table, so an edited table invalidates cached fits."""
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:12]
    except OSError:
        return "missing"


@dataclass(frozen=True)
class Channel:
    """One measured series. `start`/`end` are the observed rest values (None when not observed; one is always set).

    An element that crosses a clip line (the frame edge, a clipping container) is out of view at its unobserved rest
    state: `hidden` marks the samples where it was wholly beyond the line and `hidden_travel` is the displacement
    from the observed rest value `hidden_from` that puts it there. The fit then keeps the model out of view at those
    samples and its amplitude at least that long."""
    name: str
    values: np.ndarray
    weights: np.ndarray
    start: float | None
    end: float | None
    is_px: bool = False             # stall thresholds in px (translate, scroll) instead of progress units
    hidden: np.ndarray | None = None
    hidden_from: float | None = None
    hidden_travel: float | None = None

    @property
    def is_fixed(self):
        return self.start is not None and self.end is not None

    def at_hidden_edge(self):
        """The reading where the unobserved rest state lies just beyond the clip line: the least travel it allows."""
        if self.start is None:
            return replace(self, start=self.hidden_from + self.hidden_travel)
        return replace(self, end=self.hidden_from + self.hidden_travel)

    @property
    def anchor(self):
        """(anchor value, easing value at the anchor): the start state when observed, else the end state."""
        return (self.start, 0.0) if self.start is not None else (self.end, 1.0)

    @property
    def reliable(self):
        return self.weights > RELIABLE_WEIGHT

    @property
    def observed_range(self):
        values = self.values[self.reliable]
        return float(np.ptp(values)) if values.size else 0.0

    @property
    def accepted_range(self):
        """Range over every sample the tracker accepted, faint ones included: a row sliding in while it fades showed
        37 of its 64 px at weights above RELIABLE_WEIGHT and 51 px in all."""
        values = self.values[self.weights > 0]
        return float(np.ptp(values)) if values.size else 0.0

    def without(self, mask):
        return replace(self, weights=np.where(mask, 0.0, self.weights))

    def rough_progress(self):
        """Progress before any fit: an unobserved rest state is replaced by the reliable value farthest from the other."""
        if self.is_fixed:
            return (self.values - self.start) / (self.end - self.start + 1e-12)
        anchor, e_anchor = self.anchor
        reliable = self.values[self.reliable]
        if not reliable.size:
            return np.zeros_like(self.values)
        extreme = reliable[np.argmax(np.abs(reliable - anchor))]
        progress = (self.values - anchor) / (extreme - anchor + 1e-12)
        return progress if e_anchor == 0.0 else 1 - progress


def channel_error(channel, easing_values):
    """Normalised weighted MSE and amplitude b for every row of easing_values (rows = candidate curve/timing).
    Hidden samples weigh 1 each and count only the shortfall of the model's travel from the hidden travel."""
    hidden_count = int(channel.hidden.sum()) if channel.hidden is not None else 0
    total = channel.weights.sum() + hidden_count + 1e-12
    weights = channel.weights / total
    anchor, e_anchor = channel.anchor
    shifted = easing_values - e_anchor
    if channel.is_fixed:
        amplitude = np.full(easing_values.shape[:-1], channel.end - channel.start)
    else:
        amplitude = (weights * shifted * (channel.values - anchor)).sum(-1) / ((weights * shifted * shifted).sum(-1) + 1e-12)
        if channel.hidden_travel is not None:
            amplitude = at_least(amplitude, channel.hidden_travel * (1 - 2 * e_anchor))
    predicted = anchor + amplitude[..., None] * shifted
    residual = predicted - channel.values
    squared = weights * residual * residual
    if hidden_count:
        travel = channel.hidden_travel
        short = np.maximum(0.0, abs(travel) - (predicted - channel.hidden_from) * np.sign(travel))
        squared = np.where(channel.hidden, short * short / total, squared)
    mse = squared.sum(-1) / (amplitude * amplitude + 1e-12)
    if not channel.is_fixed:
        reference = max(channel.accepted_range, abs(channel.hidden_travel or 0.0), 1e-9)
        mse = np.where(np.abs(amplitude) > AMPLITUDE_CAP * reference, PENALTY ** 2, mse)
    return mse, amplitude


def at_least(amplitude, minimum):
    """Amplitudes that fall short of `minimum` in its direction become `minimum` (an element hidden beyond a clip line
    at its unobserved rest state travelled at least that far)."""
    return np.where(amplitude * np.sign(minimum) < abs(minimum), minimum, amplitude)


def joint_score(channels, easing_values):
    """sqrt(mean over channels of the amplitude-normalised weighted MSE)."""
    return np.sqrt(sum(channel_error(c, easing_values)[0] for c in channels) / len(channels))


def nelder_mead(f, x0, step, iters, tol=1e-9, upper=None):
    """Minimises f from x0 (prototype idiom); returns (best point, best value). `step` is the first simplex step, one
    for every coordinate or one per coordinate. A first step that would cross `upper` goes the other way: a seed on its
    bound (easeOutQuart's y1 = 1) otherwise starts with a vertex f penalises and the simplex never leaves the seed."""
    n = len(x0)
    x0 = np.array(x0, float)
    sizes = np.broadcast_to(np.asarray(step, float), (n,))
    steps = [sizes[i] if upper is None or x0[i] + sizes[i] <= upper[i] else -sizes[i] for i in range(n)]
    points = [x0] + [x0 + steps[i] * np.eye(n)[i] for i in range(n)]
    values = [f(p) for p in points]
    for _ in range(iters):
        order = np.argsort(values)
        points, values = [points[i] for i in order], [values[i] for i in order]
        if values[-1] - values[0] < tol:
            break
        centre = np.mean(points[:-1], axis=0)
        reflected = centre + (centre - points[-1])
        f_reflected = f(reflected)
        if f_reflected < values[0]:
            expanded = centre + 2 * (centre - points[-1])
            f_expanded = f(expanded)
            points[-1], values[-1] = (expanded, f_expanded) if f_expanded < f_reflected else (reflected, f_reflected)
        elif f_reflected < values[-2]:
            points[-1], values[-1] = reflected, f_reflected
        else:
            contracted = centre + 0.5 * (points[-1] - centre)
            f_contracted = f(contracted)
            if f_contracted < values[-1]:
                points[-1], values[-1] = contracted, f_contracted
            else:
                points = [points[0] + 0.5 * (p - points[0]) for p in points]
                values = [f(p) for p in points]
    best = int(np.argmin(values))
    return points[best], float(values[best])


@dataclass
class Fit:
    curve: Curve
    start_s: float
    duration_s: float
    rmse: float
    amplitudes: list            # b per channel, in the channel's units
    channel_rmse: list
    free: dict | None           # the free 6-parameter fit, None when skipped
    near_ties: list
    duration_range_s: tuple
    ranked: list                # [(rmse, name)] best named curves after refinement
    stalled: np.ndarray = None  # bool per sample, set by fit_with_stalls
    start_range_s: tuple = None # custom curves: starts that fit as well (probed), else None
    rival: dict | None = None   # the named curve a disagreeing free fit displaced: {name, rmse, start_s}
    shape_tie: bool = False     # named curves of different shapes tie: the free fit is reported, near_ties lists them

    @property
    def source(self):
        return "custom" if self.curve.name == "custom" else "named"

    @property
    def t50_s(self):
        return self.start_s + self.curve.x_at_half() * self.duration_s

    def easing(self, t):
        return self.curve.at(np.clip((np.asarray(t, float) - self.start_s) / self.duration_s, 0, 1))

    def predict(self, t, channel, index):
        anchor, e_anchor = channel.anchor
        return anchor + self.amplitudes[index] * (self.easing(t) - e_anchor)

    def progress(self, channel, index):
        """Measured progress of one channel under this fit: 0 at the start state, 1 at the end state."""
        anchor, e_anchor = channel.anchor
        start_value = anchor - self.amplitudes[index] * e_anchor
        return (channel.values - start_value) / (self.amplitudes[index] + 1e-12)


@dataclass(frozen=True)
class Search:
    t: np.ndarray
    channels: tuple
    t0_bounds: tuple
    d_bounds: tuple
    d0: float
    dt: float
    y_bounds: tuple = BEZIER_Y_RANGE        # free-fit control point y range

    @property
    def is_pinned(self):
        return self.t0_bounds[0] == self.t0_bounds[1] and self.d_bounds[0] == self.d_bounds[1]

    def contains(self, t0, duration):
        return self.t0_bounds[0] <= t0 <= self.t0_bounds[1] and self.d_bounds[0] <= duration <= self.d_bounds[1]

    def score_at(self, curve, t0, duration):
        if not self.contains(t0, duration):
            return PENALTY
        x = np.clip((self.t - t0) / duration, 0, 1)
        return float(joint_score(self.channels, curve.at(x)[None])[0])

    @property
    def free_upper(self):
        """Upper bounds of (x1, y1, x2, y2, t0, D) in the free fit."""
        return [BEZIER_X_RANGE[1], self.y_bounds[1], BEZIER_X_RANGE[1], self.y_bounds[1], self.t0_bounds[1], self.d_bounds[1]]

    def score_free(self, z):
        x1, y1, x2, y2, t0, duration = z
        (x_low, x_high), (y_low, y_high) = BEZIER_X_RANGE, self.y_bounds
        is_inside = x_low <= x1 <= x_high and x_low <= x2 <= x_high and y_low <= y1 <= y_high and y_low <= y2 <= y_high
        if not is_inside or not self.contains(t0, duration):
            return PENALTY
        easing_values = bezier_y(np.clip((self.t - t0) / duration, 0, 1), x1, y1, x2, y2)
        return float(joint_score(self.channels, easing_values[None])[0])


def main_run(mask, reliable):
    """Indices of the longest run of `mask` among reliable samples (gaps of up to MOVING_GAP reliable samples
    joined): a lone noisy rest sample must not stretch the search window, and samples left out of the fit
    (weight 0, e.g. stalled frames) must not split the motion."""
    positions = np.nonzero(reliable)[0]
    inside = np.nonzero(mask[positions])[0]
    if inside.size == 0:
        return inside
    runs = np.split(inside, np.nonzero(np.diff(inside) > MOVING_GAP + 1)[0] + 1)
    return positions[max(runs, key=len)]


def shown_run(channel, first, final):
    """(first, final) of the moving samples, widened for a channel with hidden samples to every sample between them: an
    element crossing a clip line moves wherever it shows, tracked or not. The first 28 and 72 px of a shade held only its
    featureless grab handle, and without them easeOutQuint's 400 ms lay beyond the search box's longest duration."""
    if channel.hidden is None:
        return first, final
    hidden = np.flatnonzero(channel.hidden)
    before, after = hidden[hidden < first], hidden[hidden > final]
    return (before[-1] + 1 if before.size else first), (after[0] - 1 if after.size else final)


def moving_span(t, channels, dt):
    """(t_first, D0): first moving sample and the span of moving samples, its start capped at one frame."""
    firsts, lows, highs = [], [], []
    last = len(t) - 1
    for channel in channels:
        progress = channel.rough_progress()
        moving = main_run((np.abs(progress) > MOVING_PROGRESS) & (np.abs(1 - progress) > MOVING_PROGRESS), channel.reliable)
        if not moving.size:
            continue
        first, final = shown_run(channel, moving[0], moving[-1])
        firsts.append(t[first])
        lows.append(max(t[max(first - 1, 0)], t[first] - dt))
        highs.append(t[min(final + 1, last)])
    if not firsts:
        return None
    return min(firsts), max(max(highs) - min(lows), 2 * dt)


def build_search(t, channels, dt, timing=None):
    """The (t0, D) search box; `timing=(t0, D)` pins it (the fitter-alone KAT M0 knows the true timing)."""
    y_bounds = free_y_bounds(channels)
    if timing is not None:
        t0, duration = timing
        return Search(t, tuple(channels), (t0, t0), (duration, duration), duration, dt, y_bounds)
    span = moving_span(t, channels, dt)
    if span is None:
        return None
    t_first, d0 = span
    return Search(t, tuple(channels), (t_first - 0.5 * d0, t_first + 0.5 * dt), (D_SPAN[0] * d0, D_SPAN[1] * d0), d0,
                  dt, y_bounds)


def free_y_bounds(channels):
    """[0, 1] for the control points' y unless some channel's reliable progress goes below 0 (anticipation) or
    above 1 (overshoot): a dip or bump the data never shows only lets the curve trade shape against t0 and D."""
    progress = np.concatenate([c.rough_progress()[c.reliable] for c in channels])
    low = BEZIER_Y_RANGE[0] if np.any(progress < UNDERSHOOT_PROGRESS) else 0.0
    high = BEZIER_Y_RANGE[1] if np.any(progress > OVERSHOOT_PROGRESS) else 1.0
    return low, high


def grid_rank(search, curves):
    """Every named curve over the (t0, D) grid, vectorised: [(rmse, curve, t0, D)] best first."""
    t0_grid = np.linspace(*search.t0_bounds, T0_POINTS)
    d_grid = np.linspace(*search.d_bounds, D_POINTS)
    g0, gd = (a.ravel() for a in np.meshgrid(t0_grid, d_grid, indexing="ij"))
    x = np.clip((search.t[None] - g0[:, None]) / gd[:, None], 0, 1)
    ranked = []
    for curve in curves:
        scores = joint_score(search.channels, curve.at(x))
        best = int(np.argmin(scores))
        ranked.append((float(scores[best]), curve, float(g0[best]), float(gd[best])))
    return sorted(ranked, key=lambda row: row[0])


def refine(search, row):
    """2-parameter Nelder-Mead on (t0, D) for one named curve, bounded to the grid."""
    rmse, curve, t0, duration = row
    if search.is_pinned:
        return row
    point, value = nelder_mead(lambda z: search.score_at(curve, z[0], z[1]), [t0, duration],
                               NAMED_REFINE_STEP * search.dt, NAMED_REFINE_ITERS)
    return (value, curve, float(point[0]), float(point[1])) if value < rmse else row


def free_seeds(refined, others):
    """The best named row, then the next best rows whose curves differ from every seed so far by more than
    SEED_SPREAD: the free fit is local, and three ease-out seeds with y1 = 1 never reached a custom curve with y1 =
    0.1 that md3-standard's seed found."""
    seeds = []
    for row in refined + others:
        if all(curve_distance(row[1], seed[1]) > SEED_SPREAD for seed in seeds):
            seeds.append(row)
        if len(seeds) == FREE_SEEDS:
            break
    return seeds


def free_fit(search, seeds):
    """6-parameter Nelder-Mead (x1, y1, x2, y2, t0, D) from the best named rows."""
    if search.is_pinned:
        t0, duration = search.t0_bounds[0], search.d_bounds[0]
        results = [nelder_mead(lambda z: search.score_free([*z, t0, duration]), list(curve.bezier), FREE_STEP, FREE_ITERS,
                               upper=search.free_upper[:4]) for _, curve, _, _ in seeds]
        results = [(np.array([*point, t0, duration]), value) for point, value in results]
    else:
        results = [nelder_mead(search.score_free, [*curve.bezier, t0, duration], FREE_STEP, FREE_ITERS,
                               upper=search.free_upper) for _, curve, t0, duration in seeds]
    point, value = min(results, key=lambda r: r[1])
    x1, y1, x2, y2, t0, duration = (float(v) for v in point)
    return with_flat_end(search, {"cubic_bezier": [x1, y1, x2, y2], "rmse": value, "start_s": t0, "duration_s": duration})


def with_flat_end(search, free):
    """The free fit refitted with its end control point on the end level (y2 = 1) when y2 lies within FLAT_END_SPAN of 1
    and the refit fits within CHOOSE_RATIO of it; else the free fit. A long tail creeping below a pixel trades y2
    against the duration: a Chrome drawer under cubic-bezier(0.32, 0.72, 0, 1) fitted every duration from 430 to 450 ms
    within 3 % of the best RMSE, each shorter one ending on a slope nobody sees (y2 0.99, x2 0.01), and a badge's and a
    chip's overshoot settled 3.3 and 3.9 % long on y2 0.97. CSS curves put y2 on the end level unless they accelerate
    into the end, far from it."""
    x1, y1, x2, y2 = free["cubic_bezier"]
    if y2 == 1.0 or abs(1 - y2) > FLAT_END_SPAN:
        return free
    t0, duration = free["start_s"], free["duration_s"]
    upper = search.free_upper[:3] + search.free_upper[4:]
    if search.is_pinned:
        point, value = nelder_mead(lambda z: search.score_free([*z, 1.0, t0, duration]), [x1, y1, x2], FREE_STEP,
                                   FREE_ITERS, upper=upper[:3])
        point = [*point, t0, duration]
    else:
        point, value = nelder_mead(lambda z: search.score_free([*z[:3], 1.0, *z[3:]]), [x1, y1, x2, t0, duration],
                                   FREE_STEP, FREE_ITERS, upper=upper)
    if value > CHOOSE_RATIO * free["rmse"]:
        return free
    x1, y1, x2, t0, duration = (float(v) for v in point)
    return {"cubic_bezier": [x1, y1, x2, 1.0], "rmse": value, "start_s": t0, "duration_s": duration}


def duration_range(search, curve, best_t0, best_d, best_rmse):
    """Every D whose RMSE, with t0 profiled, is within 1.25 x best + 0.002. t0 is searched +-1 frame around the
    start that keeps the fitted t50 in place, which is where a stretched curve fits best."""
    d_grid = search.d0 * np.linspace(*D_SPAN, RANGE_D_POINTS)
    centres = best_t0 + curve.x_at_half() * (best_d - d_grid)
    offsets = np.linspace(-search.dt, search.dt, RANGE_T0_POINTS)
    g0 = (centres[:, None] + offsets[None]).ravel()
    gd = np.repeat(d_grid, RANGE_T0_POINTS)
    scores = joint_score(search.channels, curve.at(np.clip((search.t[None] - g0[:, None]) / gd[:, None], 0, 1)))
    accepted = gd[scores <= RANGE_RATIO * best_rmse + RANGE_MARGIN]
    if not accepted.size:
        return None
    return float(accepted.min()), float(accepted.max())


def spanned(span, values):
    """(low, high) of a span extended to hold every value."""
    return min(span[0], *values), max(span[1], *values)


def widened(span, duration):
    """The accepted D range always holds the fitted D (the grid may step over it when the RMSE is tiny)."""
    if span is None:
        return duration, duration
    return min(span[0], duration), max(span[1], duration)


def near_ties(search, chosen, chosen_rmse, rows):
    """Named curves within 0.05 of the chosen curve whose own refined RMSE is within 1.25 x the chosen one."""
    ties = []
    for row in rows:
        curve = row[1]
        if curve.name == chosen.name:
            continue
        distance = curve_distance(curve, chosen)
        if distance > NEAR_TIE_DIST:
            continue
        rmse = refine(search, row)[0]
        if rmse <= NEAR_TIE_RATIO * chosen_rmse + 1e-9:
            ties.append({"name": curve.name, "curve_dist": round(distance, 3), "rmse": round(rmse, 4)})
    return sorted(ties, key=lambda tie: tie["rmse"])


def shape_ties(search, best, rows):
    """Named rows whose refined RMSE is within NEAR_TIE_RATIO of the best named row, when at least one of them lies
    more than NEAR_TIE_DIST from it; else []. Then the recording cannot name the curve: four ease-in curves up to
    0.077 apart fitted a 180 ms easeInQuad exit within 1.25 x, and the named-first rule picked one of them."""
    best_rmse, best_curve = best[0], best[1]
    screened = [row for row in rows if row[1].name != best_curve.name and row[0] <= TIE_SCREEN_RATIO * best_rmse]
    refined = [refine(search, row) for row in screened[:TIE_REFINED_MAX]]
    tied = sorted((row for row in refined if row[0] <= NEAR_TIE_RATIO * best_rmse + 1e-9
                   and not is_told_apart(search, best_curve, row[1])), key=lambda row: row[0])
    return tied if any(curve_distance(row[1], best_curve) > NEAR_TIE_DIST for row in tied) else []


def channel_rmse(search, channel, curve):
    """RMSE of one named curve on one channel alone, its timing refined for that channel; None when it does not move."""
    alone = build_search(search.t, [channel], search.dt)
    return None if alone is None else refine(alone, grid_rank(alone, [curve])[0])[0]


def is_told_apart(search, best_curve, curve):
    """Fitted channel by channel, each on its own timing, some channel clearly prefers the best curve and none clearly
    prefers the other. A precise translate channel told md3-standard from easeOutExpo 13 x apart where a gain-biased
    opacity channel blurred them into a joint tie; channels that prefer different curves leave them tied."""
    if len(search.channels) < 2:
        return False
    prefers_best = prefers_other = False
    for channel in search.channels:
        best, other = channel_rmse(search, channel, best_curve), channel_rmse(search, channel, curve)
        if best is None or other is None:
            continue
        prefers_best |= other > NEAR_TIE_RATIO * best + TELL_APART_MARGIN
        prefers_other |= best > NEAR_TIE_RATIO * other + TELL_APART_MARGIN
    return prefers_best and not prefers_other


def tie_list(chosen, rows):
    """near_ties entries for a shape tie: every tied named curve, its distance to the reported curve and its RMSE."""
    return [{"name": row[1].name, "curve_dist": round(curve_distance(row[1], chosen), 3), "rmse": round(row[0], 4)}
            for row in rows]


def is_close_enough(best_rmse, free):
    return best_rmse <= max(CHOOSE_RATIO * free["rmse"], free["rmse"] + CHOOSE_MARGIN)


def is_margin_win(best_rmse, free):
    """The named curve is kept only through the absolute margin, not the ratio (free RMSE near 0.004)."""
    return is_close_enough(best_rmse, free) and best_rmse > CHOOSE_RATIO * free["rmse"]


def agrees(curve, t0, free, dt):
    """A named curve may stand in for a clearly better free fit only when the two describe the same motion: shapes
    within NEAR_TIE_DIST, or starts within half a frame. md2-standard is 0.15 from a custom stagger curve and
    started 22 to 26 ms late on it."""
    return (curve_distance(curve, make_curve("custom", free["cubic_bezier"])) <= NEAR_TIE_DIST
            or abs(t0 - free["start_s"]) <= 0.5 * dt)


def named_wins(best_rmse, curve, t0, free, dt):
    if not is_close_enough(best_rmse, free):
        return False
    return not is_margin_win(best_rmse, free) or agrees(curve, t0, free, dt)


def probe_start(search, free, centre, duration, limit):
    """(start range, duration range): free curves refitted with t0 pinned 1 to 3 frames either side of `centre`;
    every pinned start whose RMSE is <= limit fits as well as the chosen curve."""
    starts, durations = [centre], [duration]
    for frames in PROBE_FRAMES:
        pinned = centre + frames * search.dt
        upper = search.free_upper
        point, value = nelder_mead(lambda z: search.score_free([*z[:4], pinned, z[4]]),
                                   [*free["cubic_bezier"], free["duration_s"]], FREE_STEP, PROBE_ITERS,
                                   upper=upper[:4] + upper[5:])
        if value <= limit:
            starts.append(pinned)
            durations.append(float(point[4]))
    return (min(starts), max(starts)), (min(durations), max(durations))


def probe_duration(search, free, start, duration, limit):
    """(low, high): free curves refitted with D pinned PROBE_STEP apart either way of `duration`, stepping out until one
    fits worse than `limit` or leaves the search box. A custom curve's flat tail trades against its duration: a 500 ms
    curve whose last 20 ms move under 0.1 px fitted 481 ms, and stretching that one shape to 500 ms fits clearly
    worse. A drawer closing under a long tail (hidden from 3 px, tracked down to 51 px) fitted 263 ms for 400 and still
    fitted at +30 %, where a fixed list of stretches ended the range."""
    low = high = duration
    upper = search.free_upper[:5]
    d_low, d_high = search.d_bounds
    for sign in (-1, 1):
        # each step starts from the last accepted curve: from the fitted one, a 10 % stretch stuck at 0.0049 where
        # the 15 % one reached 0.0019
        seed = [*free["cubic_bezier"], start]
        pinned = duration * (1 + sign * PROBE_STEP)
        while d_low <= pinned <= d_high:
            seed, value = nelder_mead(lambda z: search.score_free([*z, pinned]), seed, FREE_STEP, PROBE_ITERS, upper=upper)
            if value > limit:
                break
            low, high = min(low, pinned), max(high, pinned)
            pinned += sign * PROBE_STEP * duration
    return low, high


def seed_rows(search, seeds):
    """Extra free-fit seeds (curve, t0, D) as ranked rows, their timing clamped into the search box."""
    return [(PENALTY, curve, float(np.clip(t0, *search.t0_bounds)), float(np.clip(duration, *search.d_bounds)))
            for curve, t0, duration in seeds]


def fit_channels(t, channels, dt, curves, *, with_free=True, timing=None, seeds=()):
    """Named-first fit of one or more channels sharing (t0, D, curve). None when nothing moves. `seeds` (curve, t0, D)
    also start the free fit."""
    search = build_search(np.asarray(t, float), channels, dt, timing)
    if search is None:
        return None
    ranked = grid_rank(search, curves)
    refined = sorted([refine(search, row) for row in ranked[:NAMED_REFINED]], key=lambda row: row[0])
    best_rmse, best_curve, best_t0, best_d = refined[0]
    tied = shape_ties(search, refined[0], refined[1:] + ranked[NAMED_REFINED:]) if with_free else []
    free = None
    if with_free and (best_rmse >= FREE_SKIP_RMSE or tied or seeds):
        free = free_fit(search, free_seeds(refined, ranked[NAMED_REFINED:]) + seed_rows(search, seeds))
    rival, start_range, probed_durations = None, None, None
    is_named_first = free is None or named_wins(best_rmse, best_curve, best_t0, free, dt)
    # named curves of different shapes tie only where named-first would pick one of them: a free curve that fits
    # clearly better (0.0005 against 0.016 to 0.021) is the reading, not the names
    is_tie = bool(tied) and free is not None and is_named_first
    if is_named_first and not is_tie:
        chosen, rmse, t0, duration = best_curve, best_rmse, best_t0, best_d
        if free is not None and is_margin_win(best_rmse, free) and not search.is_pinned:
            # named only through the margin: free curves at other starts may fit better than it does
            start_range, probed_durations = probe_start(search, free, t0, duration, best_rmse)
    else:
        chosen = make_curve("custom", free["cubic_bezier"])
        rmse, t0, duration = free["rmse"], free["start_s"], free["duration_s"]
        limit = RANGE_RATIO * rmse + RANGE_MARGIN
        if (is_close_enough(best_rmse, free) or best_rmse <= limit) and not is_tie:
            rival = {"name": best_curve.name, "rmse": round(best_rmse, 4), "start_s": best_t0, "duration_s": best_d,
                     "is_in_range": best_rmse <= limit}
        if not search.is_pinned:
            start_range, probed_durations = probe_start(search, free, t0, duration, limit)
            probed_durations = spanned(probed_durations, probe_duration(search, free, t0, duration, limit))
    easing_values = chosen.at(np.clip((search.t - t0) / duration, 0, 1))[None]
    errors = [channel_error(c, easing_values) for c in channels]
    others = refined + ranked[NAMED_REFINED:]
    duration_span = widened(duration_range(search, chosen, t0, duration, rmse), duration)
    if probed_durations:
        duration_span = (min(duration_span[0], probed_durations[0]), max(duration_span[1], probed_durations[1]))
    if is_tie:
        # every tied named curve is an equally good reading, with its own start and duration
        readings = [refined[0]] + tied
        start_range = spanned(start_range or (t0, t0), [row[2] for row in readings])
        duration_span = spanned(duration_span, [row[3] for row in readings])
    if rival is not None and rival["is_in_range"]:
        # a named curve within the range limit is as good a reading as the free one, by the rule the ranges use: a
        # badge's codec-noisy overshoot fitted a custom curve 396 ms long where its easeOutBack, 352 ms, fitted within it
        start_range = spanned(start_range or (t0, t0), [rival["start_s"]])
        duration_span = spanned(duration_span, [rival["duration_s"]])
    return Fit(curve=chosen, start_s=t0, duration_s=duration, rmse=rmse,
               amplitudes=[float(amplitude[0]) for _, amplitude in errors],
               channel_rmse=[float(np.sqrt(mse[0])) for mse, _ in errors],
               free=free, near_ties=tie_list(chosen, [refined[0]] + tied) if is_tie else near_ties(search, chosen, rmse, others),
               duration_range_s=duration_span, ranked=[(round(r[0], 4), r[1].name) for r in refined],
               start_range_s=start_range, rival=rival, shape_tie=is_tie)


def fit_start(t, channels, curve, duration, guess, dt):
    """The start that fits the channels best with curve and duration fixed: a grid of +-2 frames around the guess
    (0.1 frame apart), then Nelder-Mead on the start alone."""
    t = np.asarray(t, float)
    grid = guess + np.linspace(-2 * dt, 2 * dt, 41)
    scores = joint_score(channels, curve.at(np.clip((t[None] - grid[:, None]) / duration, 0, 1)))
    score = lambda z: float(joint_score(channels, curve.at(np.clip((t - z[0]) / duration, 0, 1))[None])[0])
    point, _ = nelder_mead(score, [float(grid[int(np.argmin(scores))])], NAMED_REFINE_STEP * dt, NAMED_REFINE_ITERS)
    return float(point[0])


def spread_to(fit, t, channels):
    """This fit's timing and curve applied to every channel, each with its closed-form amplitude (--no-joint: the
    primary channel's own fit describes the element)."""
    errors = [channel_error(channel, fit.easing(t)[None]) for channel in channels]
    return replace(fit, amplitudes=[float(amplitude[0]) for _, amplitude in errors],
                   channel_rmse=[float(np.sqrt(mse[0])) for mse, _ in errors])


def stalled_samples(t, channels, fit):
    """Samples that did not move although the fit says they should: every channel expecting a step shows none
    (px channels only, when the element has one)."""
    frozen_somewhere = np.zeros(len(t), bool)
    moved_somewhere = np.zeros(len(t), bool)
    has_px = any(c.is_px for c in channels)
    for index, channel in enumerate(channels):
        if has_px and not channel.is_px:
            continue                # px channels are the precise ones; opacity gain noise must not decide a stall
        amplitude = abs(fit.amplitudes[index])
        low, high = STALL_PX if channel.is_px else (STALL_PROGRESS[0] * amplitude, STALL_PROGRESS[1] * amplitude)
        pair = channel.reliable[1:] & channel.reliable[:-1]
        measured = np.abs(np.diff(channel.values))
        predicted = np.abs(np.diff(fit.predict(t, channel, index)))
        expects = pair & (predicted > high)
        frozen_somewhere[1:] |= expects & (measured < low)
        moved_somewhere[1:] |= expects & (measured >= low)
    return frozen_somewhere & ~moved_somewhere


def fit_with_stalls(t, channels, dt, curves, *, with_free=True, seeds=()):
    """Stall-aware refit (up to 3 passes): stalled samples get weight 0 and the fit reruns."""
    t = np.asarray(t, float)
    stalled = np.zeros(len(t), bool)
    fit = fit_channels(t, channels, dt, curves, with_free=with_free, seeds=seeds)
    for _ in range(STALL_PASSES):
        if fit is None:
            return None
        found = stalled_samples(t, channels, fit) & ~stalled
        if not found.any():
            break
        stalled |= found
        refit = fit_channels(t, [c.without(stalled) for c in channels], dt, curves, with_free=with_free, seeds=seeds)
        if refit is None:
            break
        fit = refit
    fit.stalled = stalled
    return fit


def css_linear(t, progress, weights, start_s, duration_s):
    """CSS linear() with 21 stops taken from the measured progress over [start, start + duration]: the stops sit on
    measured samples, placed greedily where the curve bends most. Returns (text, stop positions, stop values,
    RMSE of that piecewise curve against the samples inside the span)."""
    reliable = weights > RELIABLE_WEIGHT
    x = (np.asarray(t, float)[reliable] - start_s) / duration_s
    measured = np.asarray(progress, float)[reliable]
    inside = (x > 0) & (x < 1)
    order = np.argsort(x[inside])
    points_x = np.concatenate([[0.0], x[inside][order], [1.0]])
    points_y = np.concatenate([[0.0], measured[inside][order], [1.0]])
    chosen = greedy_stops(points_x, points_y, LINEAR_STOPS)
    stops_x, stops_y = points_x[chosen], points_y[chosen]
    rmse = float(np.sqrt(np.mean((np.interp(points_x, stops_x, stops_y) - points_y) ** 2)))
    parts = [css_number(v) if i in (0, len(chosen) - 1) else f"{css_number(v)} {css_number(s * 100, 1)}%"
             for i, (s, v) in enumerate(zip(stops_x, stops_y))]
    return "linear(" + ", ".join(parts) + ")", stops_x, stops_y, rmse


def greedy_stops(points_x, points_y, count):
    """Indices of `count` points (first and last included) chosen by repeatedly adding the point farthest from
    the piecewise-linear curve through the points chosen so far."""
    chosen = [0, len(points_x) - 1]
    while len(chosen) < min(count, len(points_x)):
        ordered = sorted(chosen)
        deviation = np.abs(np.interp(points_x, points_x[ordered], points_y[ordered]) - points_y)
        deviation[ordered] = -1
        chosen.append(int(np.argmax(deviation)))
    return sorted(chosen)


def extrapolated_start(t, progress, reliable, earliest):
    """Where measured progress leaves 0: a cubic through the first four moving samples (a quadratic through three
    when the cubic has no root there), solved for 0 between `earliest` and the first moving sample."""
    moving = main_run(np.asarray(progress) > MOVING_PROGRESS, reliable)
    t = np.asarray(t, float)
    for degree in (3, 2):
        if moving.size < degree + 1:
            continue
        first = moving[:degree + 1]
        roots = np.roots(np.polyfit(t[first] - t[first[0]], np.asarray(progress, float)[first], degree))
        inside = [float(r.real) + t[first[0]] for r in roots
                  if abs(r.imag) < 1e-12 and earliest - 1e-9 <= float(r.real) + t[first[0]] <= t[first[0]] + 1e-9]
        if inside:
            return max(inside)
    return None


def css_number(value, decimals=3):
    """Shortest CSS number: 0.25 not 0.250, 0 not -0.000, 400 stays 400."""
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def overshoots(progress, weights):
    reliable = weights > RELIABLE_WEIGHT
    return bool(np.any(np.asarray(progress)[reliable] > OVERSHOOT_PROGRESS))
