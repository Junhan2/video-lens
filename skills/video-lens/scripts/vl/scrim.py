"""A backdrop (scrim) that dims or lightens the whole page behind a moving element: one photometric map between the
event's rest frames, last = gain x first + offset per channel, and each frame's share of it. Segmentation compares
frames under that map, so the dimmed page is not motion and a sheet's own colour is not taken for the background.

A vaul-style drawer's 40 % black overlay changed 99.8 % of the work crop's border ring; the ring median then was the
sheet's white, and the sheet split into its avatars and text lines.
"""
from dataclasses import dataclass

import cv2
import numpy as np

SAMPLE_STEP = 3             # px between the sampled pixels the map is fitted on
EDGE_FULL = 32.0            # grey gradient at which a pixel counts fully as the page's structure
# an overlay keeps at least this much of the page: an opaque sheet keeps none of what it covers, so the constant map
# onto its colour, which a flat page fits as well as the dimming, is ruled out
MIN_GAIN = 0.2
MIN_CHANGE = 8.0            # the map moves mid-grey by at least this much in some channel
TOLERANCE = 6.0             # levels a pixel may miss the map by (codec)
MIN_SHARE = 0.3             # share of the page's structure the map must explain
TRIALS = 200
MIN_PAIR_SPREAD = 24.0      # grey levels between the two pixels of a trial, so they fix the gain
PROGRESS_MIN_STEP = 16.0    # pixels the map moves by less than this do not time it


@dataclass(frozen=True)
class Scrim:
    gain: float
    offset: np.ndarray      # per channel (B, G, R)
    progress: np.ndarray    # per window frame: 0 at the first rest frame, 1 at the last
    share: float            # share of the page's structure the map explains
    box: tuple = None       # the page it covers (x, y, w, h), in px of the window it belongs to

    def carry(self, image, k_from, k_to):
        """`image`, seen at window frame k_from, as it would look under frame k_to's share of the map."""
        s_from, s_to = self.progress[k_from], self.progress[k_to]
        page = (image.astype(np.float32) - s_from * self.offset) / (1 + s_from * (self.gain - 1))
        return np.clip(page * (1 + s_to * (self.gain - 1)) + s_to * self.offset + 0.5, 0, 255).astype(np.uint8)

    @property
    def is_dimming_in(self):
        """The overlay appears (the page darkens or pales toward its colour); otherwise it clears."""
        return self.gain < 1

    def overlay(self):
        """(alpha, BGR colour) of the overlay the map implies: page x (1 - alpha) + colour x alpha at its full state."""
        if self.is_dimming_in:
            alpha = 1 - self.gain
            colour = self.offset / alpha
        else:
            alpha = 1 - 1 / self.gain
            colour = -self.offset / (alpha * self.gain)
        return float(alpha), np.clip(colour, 0, 255)


def structure_weights(image):
    """Per pixel, how much of the page's structure (grey gradient) it holds, 0 to 1."""
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gx, gy = cv2.Sobel(grey, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(grey, cv2.CV_32F, 0, 1, ksize=3)
    return np.clip(np.hypot(gx, gy) / (4 * EDGE_FULL), 0, 1)


def map_residual(first, last, gain, offset):
    return np.abs(last - (gain * first + offset)).max(axis=1)


def trial_map(first, last, i, j):
    """(gain, offset) through the pixel pair i, j, or None when the gain or the change is out of bounds."""
    df, dl = first[i] - first[j], last[i] - last[j]
    gain = float((df * dl).sum() / max(float((df * df).sum()), 1e-6))
    if not MIN_GAIN <= gain <= 1 / MIN_GAIN:
        return None
    offset = (last[i] - gain * first[i] + last[j] - gain * first[j]) / 2
    if np.abs((gain - 1) * 128 + offset).max() < MIN_CHANGE:
        return None
    return gain, offset


def refined_map(first, last, weights, inliers):
    """Weighted least squares of one gain and a per-channel offset over the inliers."""
    w = weights[inliers][:, None]
    f, l = first[inliers], last[inliers]
    f_mean, l_mean = (w * f).sum(0) / w.sum(), (w * l).sum(0) / w.sum()
    gain = float((w * (f - f_mean) * (l - l_mean)).sum() / max(float((w * (f - f_mean) ** 2).sum()), 1e-6))
    return gain, l_mean - gain * f_mean


def fit_map(first, last):
    """(gain, offset, share) of the photometric map from the first rest frame to the last that explains the most of
    the page's structure (RANSAC over pixel pairs, a fixed seed), or None."""
    weights = structure_weights(first)[::SAMPLE_STEP, ::SAMPLE_STEP].ravel()
    f = first[::SAMPLE_STEP, ::SAMPLE_STEP].reshape(-1, 3).astype(np.float32)
    l = last[::SAMPLE_STEP, ::SAMPLE_STEP].reshape(-1, 3).astype(np.float32)
    total = float(weights.sum())
    candidates = np.flatnonzero(weights > 0)
    if total <= 0 or candidates.size < 2:
        return None
    rng = np.random.default_rng(0)
    luma = f.mean(axis=1)
    best, best_score = None, 0.0
    for _ in range(TRIALS):
        i, j = rng.choice(candidates, 2, replace=False, p=weights[candidates] / weights[candidates].sum())
        if abs(luma[i] - luma[j]) < MIN_PAIR_SPREAD:
            continue
        trial = trial_map(f, l, i, j)
        if trial is None:
            continue
        score = float(weights[map_residual(f, l, *trial) <= TOLERANCE].sum())
        if score > best_score:
            best, best_score = trial, score
    if best is None:
        return None
    gain, offset = refined_map(f, l, weights, map_residual(f, l, *best) <= TOLERANCE)
    inliers = map_residual(f, l, gain, offset) <= TOLERANCE
    share = float(weights[inliers].sum()) / total
    if share < MIN_SHARE or not MIN_GAIN <= gain <= 1 / MIN_GAIN or np.abs((gain - 1) * 128 + offset).max() < MIN_CHANGE:
        return None
    return gain, offset, share


def frame_progress(images, gain, offset):
    """Per frame, its share of the map: the mean of the middle half of the per-pixel shares over the pixels that follow
    the map at the rest frames and that it moves by at least PROGRESS_MIN_STEP. An element passing over some of them
    stays out of the middle half, and pixels of different levels dither the 8-bit steps a median of one flat page colour
    kept (a 40 % dim moves the page by 99 levels: 1 % steps, and the median read the curve's tail 27 % short)."""
    first = images[0][::SAMPLE_STEP, ::SAMPLE_STEP].reshape(-1, 3).astype(np.float32)
    last = images[-1][::SAMPLE_STEP, ::SAMPLE_STEP].reshape(-1, 3).astype(np.float32)
    step = (gain - 1) * first + offset
    used = (map_residual(first, last, gain, offset) <= TOLERANCE) & (np.abs(step).max(axis=1) >= PROGRESS_MIN_STEP)
    if not used.any():
        return None
    step = step[used]
    progress = []
    for image in images:
        moved = image[::SAMPLE_STEP, ::SAMPLE_STEP].reshape(-1, 3).astype(np.float32)[used] - first[used]
        shares = np.sort((moved * step).sum(axis=1) / (step * step).sum(axis=1))
        progress.append(float(shares[len(shares) // 4:max(len(shares) * 3 // 4, len(shares) // 4 + 1)].mean()))
    # the map is fitted over every level of the page, and the middle half of its shares sat 0.9 % above it at the last
    # rest frame too: the rest frames are 0 and 1 by definition
    return np.array(progress) / progress[-1]


def find_scrim(images):
    """The Scrim of an event window, or None when no one map explains enough of the page."""
    fitted = fit_map(images[0], images[-1])
    if fitted is None:
        return None
    gain, offset, share = fitted
    progress = frame_progress(images, gain, offset)
    return None if progress is None else Scrim(gain, offset, progress, share)
