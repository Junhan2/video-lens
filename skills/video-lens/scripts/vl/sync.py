"""Audio-visual sync (spec 7.7): visual onsets (content cuts, flashes, motion starts) paired with strong audio
onsets outside speech; reports the median offset, its IQR and a cross-correlation lag.

Positive offsets mean the audio is later than the picture. The on-screen cause can lie up to one frame
interval before the frame that shows it, so the frame interval is reported next to the offset.
"""
import numpy as np

PAIR_WINDOW_S = 0.25
MIN_PAIRS = 3
XCORR_SIGMA_S = 0.01
XCORR_MAX_LAG_MS = 300
XCORR_REACH_S = XCORR_MAX_LAG_MS / 1000 + 5 * XCORR_SIGMA_S    # pairs farther apart add nothing at any lag
CONVENTION = "positive = audio later"


def analyze_sync(run):
    """The analysis.json `sync` section; `insufficient` below 3 mutual pairs."""
    visual = np.array(sorted({t for t, _ in run.visual_onsets if run.range.contains(t)}))
    audio_times, strengths = strong_audio_onsets(run.analysis["audio"])
    pairs = mutual_pairs(visual, audio_times)
    if len(pairs) < MIN_PAIRS:
        return {"status": "insufficient", "pairs": len(pairs)}
    offsets_ms = np.array([(a - v) * 1000 for v, a in pairs])
    frame_interval = run.frame_interval_s
    return {
        "status": "ok",
        "pairs": len(pairs),
        "offset_ms_median": round(float(np.median(offsets_ms)), 1),
        "offset_ms_iqr": [round(float(q), 1) for q in np.percentile(offsets_ms, [25, 75])],
        "xcorr_lag_ms": xcorr_lag_ms(visual, audio_times, strengths),
        "frame_interval_ms": round(frame_interval * 1000, 1) if frame_interval else None,
        "convention": CONVENTION,
    }


def strong_audio_onsets(audio):
    """Onsets with strength >= the median of all onsets that lie outside activity segments labelled speech."""
    if not audio["onsets"]:
        return np.zeros(0), np.zeros(0)
    times = np.array([onset["t"] for onset in audio["onsets"]])
    strengths = np.array([onset["strength"] for onset in audio["onsets"]])
    speech = sorted((s["start_s"], s["end_s"]) for s in audio["segments"] if s["kind"] == "speech")
    speech_starts = np.array([start for start, _ in speech])
    speech_ends = np.array([end for _, end in speech])
    containing = np.searchsorted(speech_starts, times, side="right") - 1    # activity segments never overlap
    is_in_speech = (containing >= 0) & (times < speech_ends[np.maximum(containing, 0)]) if speech else np.zeros(times.size, bool)
    keep = (strengths >= np.median(strengths)) & ~is_in_speech
    return times[keep], strengths[keep]


def nearest(sorted_times, targets):
    """Index into sorted_times of the element nearest each target (sorted_times must be non-empty)."""
    if len(sorted_times) == 1:
        return np.zeros(len(targets), int)
    right = np.clip(np.searchsorted(sorted_times, targets), 1, len(sorted_times) - 1)
    left = right - 1
    is_left = np.abs(targets - sorted_times[left]) <= np.abs(sorted_times[right] - targets)
    return np.where(is_left, left, right)


def mutual_pairs(visual, audio_times):
    """(visual t, audio t) where each is the other's nearest neighbour and they lie within ±250 ms."""
    if visual.size == 0 or audio_times.size == 0:
        return []
    to_audio = nearest(audio_times, visual)
    to_visual = nearest(visual, audio_times)
    return [(float(visual[i]), float(audio_times[j])) for i, j in enumerate(to_audio)
            if to_visual[j] == i and abs(audio_times[j] - visual[i]) <= PAIR_WINDOW_S]


def xcorr_lag_ms(visual, audio_times, strengths):
    """Lag (1 ms grid, ±300 ms) maximising the correlation of Gaussian trains (sigma 10 ms) at the visual onsets
    (weight 1) and the audio onsets (weight = strength). Two Gaussians of sigma s correlate as a Gaussian of
    variance 2 s^2 in their distance, so only onset pairs are summed instead of a whole-timeline grid."""
    lags_ms = np.arange(-XCORR_MAX_LAG_MS, XCORR_MAX_LAG_MS + 1)
    score = np.zeros(lags_ms.size)
    for v in visual:
        near = np.abs(audio_times - v) <= XCORR_REACH_S
        distance_ms = (audio_times[near] - v) * 1000
        score += (strengths[near][:, None] *
                  np.exp(-((distance_ms[:, None] - lags_ms[None, :]) ** 2) / (4 * (XCORR_SIGMA_S * 1000) ** 2))).sum(axis=0)
    return int(lags_ms[int(np.argmax(score))])
