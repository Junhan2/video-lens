"""Content survey (spec 7.5): one 160 px decode gives per-frame change features, still runs, 0.5 s cell winners
and visual events; cuts, peaks, keyframes (with the adaptive peak rule), same_as, the keyframe budget and the OCR
plan are computed afterwards on the arrays. Ported from D2 proto3/4/5 and D3 cutrule.py.

At 160 px a changed title on a shared slide template moves under 0.1 % of the pixels, below every threshold, so
state changes the thumbnails cannot confirm are checked on DETAIL_W frames from the frame cache (DetailFrames).
"""
import heapq
import math
import shutil
from collections import OrderedDict, deque

import cv2
import numpy as np

from . import decode, ocr, pixels, sheets_content
from .context import FRAME_MATCH_S, content_time
from .errors import EXIT_EMPTY_RANGE, VlError

SURVEY_VERSION = 2              # bump when the cached arrays change meaning
THUMB_W = 160
BLUR_KSIZE = (5, 5)
BLUR_SIGMA = 1.2
STILL_FRAC = 0.005              # still: frac < 0.005 and mad < act_mad
ACT_MAD_BASE = 0.4              # act_mad = 0.4 + 2 x p10(mad)
ACT_MAD_GAIN = 2.0
MAD_QUANTILE = 0.10
DECISION_LAG_S = 1.0
SETTLED_HOLD_S = 0.2
CUT_WINDOW_S = 0.5
CUT_BASE_MIN = 0.02
PEAK_GAIN = 2.5
PEAK_FLOOR = 0.1
PEAK_MEDIAN_S = 5.0
PEAK_LOCAL_S = 0.5
FLASH_FRAC = 0.2
FLASH_RATIO = 3.0
FLASH_PREV_MIN = 0.01
EVENT_CUT_E2E = 0.30
EVENT_UPDATE_E2E = 0.04
TRANSIENT_GAP_S = 0.6
TRANSIENT_REVERT = 0.01
TRANSIENT_FIRST_KINDS = ("cut", "transition", "update")
BUSY_SHARE = 0.3
KEY_MOTION_GAP_S = 1.0
HELD_STATE_S = 2.0              # a keyframe held this long is a screen state (a slide): only the hard cap drops it
SHOT_REASONS = sheets_content.SHOT_REASONS
DHASH_MAX_DIST = 4
KEYFRAME_WIDTH = 1280
KEYFRAME_JPEG_QUALITY = 95
DETAIL_W = 640                  # talk10 same-template slides: 0.9 to 1.4 % changed here, 0.08 % at 160 px; repeats 0 %
DETAIL_MIN_FRAC = 0.002         # at DETAIL_W: two frames show different screen states
DETAIL_STEP_FRAC = 2e-4         # 3 changed px at 160 px: a frame where a still screen may have changed state
DETAIL_CACHE_FRAMES = 64        # DETAIL_W images kept in memory
ONSET_KINDS = ("cut", "transition", "transient", "update")
PROGRESS_EVERY = 30
DEFAULT_FRAME_S = 1 / 30


def analyze_content(run):
    """Stage entry (INTERFACES 8): the analysis.json `content` section. Writes keyframes/, sheets/kf_NN.jpg."""
    params = run.params["content"]
    with run.timer("survey"):
        arrays = survey_arrays(run)
        detail = DetailFrames(run, arrays["busy_frac"] < BUSY_SHARE)
        found = classify(arrays, params, run.range.end_s, detail)
        keyframes = keyframe_records(run, found["keyframes"])
        exact_times = [k["t"] for k in found["keyframes"]]
        export_keyframes(run, keyframes, exact_times)
    text, ocr_info = {"persistent": [], "events": []}, None
    if params["ocr"]:
        with run.timer("ocr"):
            times = arrays["times"][found["ocr_plan"]]
            text, ocr_info, ids_by_time = ocr.analyze_text(run, times, exact_times)
        for keyframe, t in zip(keyframes, exact_times):
            keyframe["text_ids"] = ids_by_time.get(ocr.time_key(t), [])
        date_text_by_keyframes(text["events"], keyframes, times)
    sheets_content.write_keyframe_sheets(run, keyframes)
    add_visual_onsets(run, arrays, found)
    return {"shots": shot_records(found["shots"], keyframes), "events": event_records(found["events"]),
            "keyframes": keyframes, "text": text, "ocr": ocr_info}


def export_width(run):
    """Keyframe frames are decoded once at the OCR width when OCR runs (OCR reuses them), else at 1280."""
    return ocr.OCR_MAX_WIDTH if run.params["content"]["ocr"] else KEYFRAME_WIDTH


class DetailFrames:
    """DETAIL_W frames of chosen times, taken from the frame cache at the export width and blurred like the survey
    thumbnails, for the comparisons 160 px cannot make. Keyframe times are exported anyway, so checking a
    keyframe costs no extra decode."""

    def __init__(self, run, keep):
        self.run = run
        self.pix = run.params["content"]["pix"]
        self.keep = keep
        self.keep_detail = None
        self.images = OrderedDict()

    def fetch(self, times):
        """Decodes the missing times in parallel (cache files), so later comparisons only read files."""
        return decode.cached_frames(self.run, sorted(set(times)), width=export_width(self.run))

    def image(self, t):
        if t in self.images:
            self.images.move_to_end(t)
            return self.images[t]
        path = self.fetch([t])[0]
        frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
        height = max(2, round(DETAIL_W * frame.shape[0] / frame.shape[1]))
        small = cv2.GaussianBlur(cv2.resize(frame, (DETAIL_W, height), interpolation=cv2.INTER_AREA), BLUR_KSIZE, BLUR_SIGMA)
        self.images[t] = small
        if len(self.images) > DETAIL_CACHE_FRAMES:
            self.images.popitem(last=False)
        return small

    def change(self, a, b):
        """Share of changed non-busy pixels between the frames at times a and b, at DETAIL_W."""
        first, second = self.image(a), self.image(b)
        if self.keep_detail is None or self.keep_detail.shape != first.shape[:2]:
            size = (first.shape[1], first.shape[0])
            self.keep_detail = cv2.resize(self.keep.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST).astype(bool)
        return changed_frac(first, second, self.pix, self.keep_detail)


# ---------------------------------------------------------------- survey decode (cached)

def survey_arrays(run):
    params = run.params["content"]
    key = {"version": SURVEY_VERSION, "range": [run.range.start_s, run.range.end_s], "pix": params["pix"],
           "cell_s": params["cell_s"], "fast": params["fast"], "thumb_w": THUMB_W}
    entry = run.cache.entry("survey", key, "npz")
    if entry.is_hit:
        return entry.load_npz()
    arrays = scan(run, params["pix"], params["cell_s"])
    entry.save_npz(**arrays)
    return arrays


def scan(run, pix, cell_s):
    """One streaming decode at 160 px (skip_loop_filter; -skip_frame noref with --fast)."""
    width, height = run.display_size
    thumb_h = max(2, round(THUMB_W * height / width / 2) * 2)
    stream = decode.stream_run(run, f"scale={THUMB_W}:{thumb_h}:flags=area", pix_fmt="rgb24", fast=True,
                               noref=run.params["content"]["fast"])
    frame_s = run.frame_interval_s or DEFAULT_FRAME_S
    scanner = Scanner(pix, cell_s, lag_frames=max(1, round(DECISION_LAG_S / frame_s)))
    total = run.frames_in_range()
    for frame in stream:
        scanner.add(frame.t, cv2.GaussianBlur(frame.image, BLUR_KSIZE, BLUR_SIGMA))
        if frame.index % PROGRESS_EVERY == 0:
            run.progress("survey", frame.index, max(total, frame.index + 1))
    run.progress("survey", len(stream.times), len(stream.times))
    return scanner.finish(frame_s)


class RunningQuantile:
    """Lower q-quantile of a growing stream: the smallest floor(q (n-1)) + 1 values sit in a max-heap."""

    def __init__(self, q):
        self.q = q
        self.low = []       # negated
        self.high = []

    def add(self, value):
        if self.low and value > -self.low[0]:
            heapq.heappush(self.high, value)
        else:
            heapq.heappush(self.low, -value)
        target = int(self.q * (len(self.low) + len(self.high) - 1)) + 1
        while len(self.low) > target:
            heapq.heappush(self.high, -heapq.heappop(self.low))
        while len(self.low) < target and self.high:
            heapq.heappush(self.low, -heapq.heappop(self.high))

    @property
    def value(self):
        return -self.low[0] if self.low else 0.0


class Scanner:
    """Streaming survey state. Stillness needs act_mad = 0.4 + 2 p10(mad), and p10 over the whole range is unknown
    while decoding, so each frame is decided DECISION_LAG_S after it arrives, with p10 over every mad seen so far.
    Deciding in the stream is what lets memory stay at about one thumbnail per cell: event boundary thumbnails and
    cell winners are taken as runs open and close.

    Still runs follow D2 proto5: a run of still frames i..j starts at the frame before it (s = i - 1), because that
    frame already shows the settled state; every frame s..j holds `hold = t[j + 1] - t[s]`. Cell winner = best
    (hold >= 0.2 s, hold, -mad, -index).
    """

    def __init__(self, pix, cell_s, lag_frames):
        self.pix = pix
        self.cell_s = cell_s
        self.lag = lag_frames
        self.busy = None
        self.times, self.frac, self.mad = [], [], []
        self.mad_p10 = RunningQuantile(MAD_QUANTILE)
        self.prev_thumb = None
        self.pending = deque()          # (index, thumb) not decided yet
        self.last_decided = None        # (index, thumb)
        self.waiting_active = None      # (index, thumb): active frame whose hold depends on the next decision
        self.span = None                # open still run: {"start": s, "cells": {cell: (mad, index, thumb)}}
        self.cells = {}                 # cell -> (key, index, hold, thumb, span start time)
        self.event_open = None          # (first active index, pre-state thumb)
        self.last_event_pre = None
        self.events = []                # (a, b, e2e, reverts_previous)

    def add(self, t, thumb):
        index = len(self.times)
        if self.prev_thumb is None:
            self.busy = np.zeros(thumb.shape[:2], np.int32)
            frac = mad = 0.0
        else:
            mask, diff = changed_mask(thumb, self.prev_thumb, self.pix)
            self.busy += mask
            frac, mad = float(np.count_nonzero(mask)) / mask.size, float(diff.mean())
            self.mad_p10.add(mad)
        self.times.append(t)
        self.frac.append(frac)
        self.mad.append(mad)
        self.prev_thumb = thumb
        self.pending.append((index, thumb))
        if len(self.pending) > self.lag:
            self.decide(*self.pending.popleft())

    def decide(self, index, thumb):
        act_mad = ACT_MAD_BASE + ACT_MAD_GAIN * self.mad_p10.value
        if self.frac[index] < STILL_FRAC and self.mad[index] < act_mad:
            self.on_still(index, thumb)
        else:
            self.on_active(index, thumb)
        self.last_decided = (index, thumb)

    def on_still(self, index, thumb):
        if self.event_open:
            self.close_event(*self.last_decided)
        if self.span is None:
            start = self.waiting_active or (index, thumb)
            self.span = {"start": start[0], "cells": {}}
            self.add_to_span(*start)
            self.waiting_active = None
        if self.span["start"] != index:
            self.add_to_span(index, thumb)

    def on_active(self, index, thumb):
        if self.span is not None:
            self.close_span(self.times[index])
        elif self.waiting_active:
            self.offer_active(*self.waiting_active)
        self.waiting_active = (index, thumb)
        if self.event_open is None:
            self.event_open = (index, self.last_decided[1])

    def close_event(self, last_index, post_thumb):
        first_index, pre_thumb = self.event_open
        e2e = changed_frac(pre_thumb, post_thumb, self.pix)
        reverts = changed_frac(self.last_event_pre, post_thumb, self.pix) if self.last_event_pre is not None else math.nan
        self.events.append((first_index, last_index, e2e, reverts))
        self.last_event_pre = pre_thumb
        self.event_open = None

    def add_to_span(self, index, thumb):
        cell = int(self.times[index] // self.cell_s)
        slot = self.span["cells"].get(cell)
        if slot is None or self.mad[index] < slot[0]:
            self.span["cells"][cell] = (self.mad[index], index, thumb)

    def close_span(self, end_t):
        start_t = self.times[self.span["start"]]
        for _, index, thumb in self.span["cells"].values():
            self.offer(index, thumb, end_t - start_t, start_t)
        self.span = None

    def offer_active(self, index, thumb):
        self.offer(index, thumb, 0.0, self.times[index])

    def offer(self, index, thumb, hold, span_start):
        cell = int(self.times[index] // self.cell_s)
        key = (hold >= SETTLED_HOLD_S, hold, -self.mad[index], -index)
        best = self.cells.get(cell)
        if best is None or key > best[0]:
            self.cells[cell] = (key, index, hold, thumb, span_start)

    def finish(self, frame_s):
        """Decides the frames still pending (with the final p10), closes open runs and returns the cached arrays."""
        while self.pending:
            self.decide(*self.pending.popleft())
        if not self.times:
            raise VlError(EXIT_EMPTY_RANGE, "the content survey decoded no frame in this range",
                          "Widen --start/--end, or pass --fast off (it skips non-reference frames)")
        times = np.array(self.times, np.float64)
        frame_s = float(np.median(np.diff(times))) if len(times) > 1 else frame_s
        if self.event_open:
            self.close_event(*self.last_decided)
        if self.span is not None:
            self.close_span(times[-1] + frame_s)
        if self.waiting_active:
            self.offer_active(*self.waiting_active)
        winners = [self.cells[cell] for cell in sorted(self.cells)]
        events = np.array(self.events, np.float64).reshape(-1, 4)
        return {"times": times, "frac": np.array(self.frac, np.float32), "mad": np.array(self.mad, np.float32),
                "busy_frac": (self.busy / len(times)).astype(np.float32),
                "cell_index": np.array([w[1] for w in winners], np.int64),
                "cell_hold": np.array([w[2] for w in winners], np.float64),
                "cell_thumbs": np.stack([w[3] for w in winners]),
                "cell_span_s": np.array([w[4] for w in winners], np.float64),
                "event_first": events[:, 0].astype(np.int64), "event_last": events[:, 1].astype(np.int64),
                "event_e2e": events[:, 2], "event_reverts": events[:, 3],
                "frame_s": np.float64(frame_s)}


def changed_mask(a, b, pix):
    """Pixels whose largest per-channel |difference| exceeds pix, plus the per-channel |difference| image."""
    diff = cv2.absdiff(a, b)
    return pixels.channel_max(diff) > pix, diff


def changed_frac(a, b, pix, keep=None):
    """Share of changed pixels, counted only where `keep` is True when it has any True pixel."""
    changed = changed_mask(a, b, pix)[0]
    if keep is not None and keep.any():
        return float(changed[keep].mean())
    return float(changed.mean())


# ---------------------------------------------------------------- classification on the arrays

def classify(arrays, params, range_end_s, detail):
    """Cuts, peaks, flash peaks, events, keyframes and the OCR plan (spec 7.5), post-hoc on the arrays; `detail`
    (DetailFrames) confirms state changes and repeats the thumbnails cannot."""
    times, frac, mad = arrays["times"], arrays["frac"], arrays["mad"]
    keep = arrays["busy_frac"] < BUSY_SHARE
    cuts = find_cuts(times, frac, params["cut_frac"], params["cut_ratio"])
    peaks = find_peaks(times, mad)
    keyframes = pick_keyframes(arrays, times[cuts], times[peaks], keep, params)
    keyframes = add_detail_keyframes(keyframes, arrays, change_times(times, frac, peaks), detail, params, range_end_s)
    thumbs, pix = arrays["cell_thumbs"], params["pix"]
    keyframes = apply_budget(keyframes, thumbs, keep, pix, params["max_keyframes"], is_passing_state)
    keyframes = apply_budget(keyframes, thumbs, keep, pix, params["keyframe_cap"], is_not_shot_start)
    state_changes = np.unique(np.r_[times[peaks], times[cuts], [k["change_s"] for k in keyframes if k["change_s"] is not None]])
    set_state_holds(keyframes, arrays, state_changes)
    detail.fetch([k["t"] for k in keyframes])        # the keyframe exports, decoded in parallel
    mark_repeats(keyframes, thumbs, keep, params, detail)
    return {"flashes": find_flash_peaks(frac),
            "events": classify_events(arrays),
            "shots": shot_spans(times, cuts, float(arrays["frame_s"]), range_end_s),
            "keyframes": keyframes,
            "ocr_plan": ocr_plan(arrays, keyframes, keep, params)}


def find_cuts(times, frac, cut_frac, cut_ratio):
    """frac_i >= cut_frac and >= cut_ratio x max(median frac over +-0.5 s without i, 0.02). Frame 0 has no diff."""
    cuts = []
    for i in np.flatnonzero(frac >= cut_frac):
        if i == 0:
            continue
        lo = max(1, int(np.searchsorted(times, times[i] - CUT_WINDOW_S, "left")))
        hi = int(np.searchsorted(times, times[i] + CUT_WINDOW_S, "right"))
        neighbours = np.concatenate([frac[lo:i], frac[i + 1:hi]])
        base = float(np.median(neighbours)) if neighbours.size else 0.0
        if frac[i] >= cut_ratio * max(base, CUT_BASE_MIN):
            cuts.append(int(i))
    return np.array(cuts, np.int64)


def find_peaks(times, mad):
    """D3 adaptive peak on per-frame mad: mad_i >= 2.5 x median(mad over +-5 s) + 0.1 and the maximum within +-0.5 s."""
    local_lo = np.searchsorted(times, times - PEAK_LOCAL_S, "left")
    local_hi = np.searchsorted(times, times + PEAK_LOCAL_S, "right")
    peaks = []
    for i in range(1, len(mad)):
        if mad[i] < PEAK_FLOOR or mad[i] < mad[local_lo[i]:local_hi[i]].max():
            continue
        lo = max(1, int(np.searchsorted(times, times[i] - PEAK_MEDIAN_S, "left")))
        hi = int(np.searchsorted(times, times[i] + PEAK_MEDIAN_S, "right"))
        if mad[i] >= PEAK_GAIN * float(np.median(mad[lo:hi])) + PEAK_FLOOR:
            peaks.append(i)
    return np.array(peaks, np.int64)


def find_flash_peaks(frac):
    """Sync's content onsets (spec 7.7): frac_i >= 0.2 and >= 3 x max(frac_(i-1), 0.01)."""
    previous = np.maximum(np.r_[np.inf, frac[:-1]], FLASH_PREV_MIN)
    return np.flatnonzero((frac >= FLASH_FRAC) & (frac >= FLASH_RATIO * previous))


def classify_events(arrays):
    """Kinds from e2e (change between the frame before a run and its last frame), then D2's transient merge: an
    update/cut/transition followed within 0.6 s by an event whose end state is this event's start state."""
    times, frac, frame_s = arrays["times"], arrays["frac"], float(arrays["frame_s"])
    raw = []
    for first, last, e2e, reverts in zip(arrays["event_first"], arrays["event_last"], arrays["event_e2e"], arrays["event_reverts"]):
        end_s = times[last + 1] if last + 1 < len(times) else times[last] + frame_s
        kind = ("cut" if first == last and e2e >= EVENT_CUT_E2E else "transition" if e2e >= EVENT_CUT_E2E
                else "update" if e2e >= EVENT_UPDATE_E2E else "activity")
        raw.append({"start_s": float(times[first]), "end_s": float(end_s), "kind": kind, "e2e": float(e2e),
                    "peak": float(frac[first:last + 1].max()), "reverts": float(reverts)})
    merged, k = [], 0
    while k < len(raw):
        event = raw[k]
        after = raw[k + 1] if k + 1 < len(raw) else None
        if (after and event["kind"] in TRANSIENT_FIRST_KINDS and after["start_s"] - event["end_s"] <= TRANSIENT_GAP_S
                and after["reverts"] < TRANSIENT_REVERT):
            merged.append(dict(event, kind="transient", end_s=after["end_s"], peak=max(event["peak"], after["peak"])))
            k += 2
            continue
        merged.append(event)
        k += 1
    return merged


def shot_spans(times, cuts, frame_s, range_end_s):
    starts = [float(times[0])] + [float(times[i]) for i in cuts]
    ends = starts[1:] + [min(float(times[-1]) + frame_s, range_end_s)]
    return list(zip(starts, ends))


def pick_keyframes(arrays, cut_times, peak_times, keep, params):
    """Walks the cell winners (spec 7.5 Keyframes): first | shot | change | peak."""
    times, thumbs = arrays["times"], arrays["cell_thumbs"]
    keyframes, last_thumb = [], None
    for cell, (index, hold) in enumerate(zip(arrays["cell_index"], arrays["cell_hold"])):
        t, is_settled = float(times[index]), hold >= SETTLED_HOLD_S
        shot = int(np.searchsorted(cut_times, t, "right"))
        if not keyframes:
            reason, change = "first", 1.0
        else:
            last = keyframes[-1]
            change = changed_frac(thumbs[cell], last_thumb, params["pix"], keep)
            has_peak = np.searchsorted(peak_times, t, "right") > np.searchsorted(peak_times, last["t"], "right")
            if shot != last["shot"]:
                reason = "shot"
            elif change >= params["key_frac"] and (is_settled or t - last["t"] >= KEY_MOTION_GAP_S):
                reason = "change"
            elif is_settled and has_peak and change >= params["key_low"]:
                reason = "peak"
            else:
                continue
        keyframes.append(keyframe_item(cell, index, t, shot, is_settled, hold, change, reason))
        last_thumb = thumbs[cell]
    return keyframes


def keyframe_item(cell, index, t, shot, is_settled, hold, change, reason, change_s=None):
    """`span_hold_s` (the still span, which the budget protects) and `hold_s` (the keyframe's own state, reported)
    start equal; set_state_holds cuts `hold_s` where the next state begins. `change_s`: detail keyframes' change."""
    return {"cell": cell, "index": int(index), "t": t, "shot": shot, "settled": bool(is_settled), "span_hold_s": float(hold),
            "hold_s": float(hold), "shown_s": t, "change": float(change), "reason": reason, "same_as": None,
            "change_s": change_s}


def change_times(times, frac, peaks):
    """Frames where a still screen may change state: mad peaks, and frames changing >= 3 thumbnail px that are the
    largest change within +-0.5 s (a slide swap on a shared template moves 12 px at 160 px, too few for any rule)."""
    lo = np.searchsorted(times, times - PEAK_LOCAL_S, "left")
    hi = np.searchsorted(times, times + PEAK_LOCAL_S, "right")
    steps = [i for i in np.flatnonzero(frac >= DETAIL_STEP_FRAC) if i and frac[i] >= frac[lo[i]:hi[i]].max()]
    return np.unique(np.concatenate([times[peaks], times[np.array(steps, np.int64)]]))


def detail_candidates(arrays, keyframes, changes, range_end_s, limit):
    """(cell, change time) for each state change no keyframe follows: the first cell winner after a change whose
    state lasts >= HELD_STATE_S until the next change, inside a still span of that length. At most `limit`, the
    longest held first."""
    times, cell_hold = arrays["times"], arrays["cell_hold"]
    winner_t = times[arrays["cell_index"]]
    key_times = np.array(sorted(k["t"] for k in keyframes))
    found = []
    for start, end in zip(changes, np.r_[changes[1:], range_end_s]):
        is_covered = np.searchsorted(key_times, end, "left") > np.searchsorted(key_times, start, "right")
        if end - start < HELD_STATE_S or is_covered:
            continue
        cells = np.flatnonzero((winner_t > start) & (winner_t < end) & (cell_hold >= HELD_STATE_S))
        if cells.size:
            found.append((int(cells[0]), float(start), float(end - start)))
    kept = sorted(found, key=lambda item: -item[2])[:limit]
    return sorted((cell, start) for cell, start, _ in kept)


def add_detail_keyframes(keyframes, arrays, changes, detail, params, range_end_s):
    """Adds a keyframe (reason `detail`) where a held state differs at DETAIL_W from the keyframe before it while
    its thumbnail changed less than --key-low (a larger thumbnail change was the 160 px rules' call to make); its
    `change` is measured at DETAIL_W."""
    candidates = detail_candidates(arrays, keyframes, changes, range_end_s, params["keyframe_cap"])
    if not candidates:
        return keyframes
    times, cell_index, thumbs = arrays["times"], arrays["cell_index"], arrays["cell_thumbs"]
    key_times = [k["t"] for k in keyframes]
    references = [keyframes[max(0, np.searchsorted(key_times, start, "right") - 1)]["t"] for _, start in candidates]
    detail.fetch(references + [float(times[cell_index[cell]]) for cell, _ in candidates])
    start_of = dict(candidates)
    merged = sorted([(k["t"], k) for k in keyframes] + [(float(times[cell_index[c]]), c) for c, _ in candidates],
                    key=lambda item: item[0])
    result = []
    for t, item in merged:
        if isinstance(item, dict):
            result.append(item)
            continue
        if not result:
            continue
        reference = result[-1]
        if changed_frac(thumbs[item], thumbs[reference["cell"]], params["pix"], detail.keep) >= params["key_low"]:
            continue
        change = detail.change(reference["t"], t)
        if change >= DETAIL_MIN_FRAC:
            result.append(keyframe_item(item, cell_index[item], t, reference["shot"], True, arrays["cell_hold"][item],
                                        change, "detail", start_of[item]))
    return result


def set_state_holds(keyframes, arrays, changes):
    """hold_s and shown_s of each settled keyframe: its still span, cut where the next keyframe's state begins (the
    last state change before it: a mad peak, a cut or a detail change). Slides swapped without an active frame
    share one still span."""
    begins = [-math.inf] + [last_change(changes, a["t"], b["t"]) for a, b in zip(keyframes, keyframes[1:])] + [math.inf]
    for number, keyframe in enumerate(keyframes):
        if not keyframe["settled"]:
            continue
        span_start = float(arrays["cell_span_s"][keyframe["cell"]])
        start = max(span_start, begins[number])
        end = min(span_start + float(arrays["cell_hold"][keyframe["cell"]]), begins[number + 1])
        keyframe["shown_s"] = min(start, keyframe["t"])
        keyframe["hold_s"] = max(0.0, end - start)


def last_change(changes, after, until):
    """The last change time in (after, until], else `until`."""
    position = int(np.searchsorted(changes, until, "right"))
    return float(changes[position - 1]) if position and changes[position - 1] > after else until


def is_not_shot_start(keyframe):
    return keyframe["reason"] not in SHOT_REASONS


def is_passing_state(keyframe):
    """Neither a shot start nor inside a still span held >= 2 s: what the soft budget may drop."""
    return is_not_shot_start(keyframe) and keyframe["span_hold_s"] < HELD_STATE_S


def apply_budget(keyframes, thumbs, keep, pix, budget, can_drop):
    """Over budget, repeatedly drops the `can_drop` keyframe with the smallest change to its predecessor; the
    next keyframe's change is then recomputed against its new predecessor (lazy heap, stale entries skipped)."""
    droppable = sum(map(can_drop, keyframes))
    excess = min(len(keyframes) - budget, droppable)
    if excess <= 0:
        return keyframes
    prev = list(range(-1, len(keyframes) - 1))
    nxt = list(range(1, len(keyframes) + 1))
    version = [0] * len(keyframes)
    heap = [(k["change"], i, 0) for i, k in enumerate(keyframes) if can_drop(k)]
    heapq.heapify(heap)
    dropped = set()
    while len(dropped) < excess:
        _, i, stamp = heapq.heappop(heap)
        if i in dropped or stamp != version[i]:
            continue
        dropped.add(i)
        before, after = prev[i], nxt[i]
        if after < len(keyframes):
            prev[after] = before
            keyframes[after]["change"] = changed_frac(thumbs[keyframes[after]["cell"]], thumbs[keyframes[before]["cell"]], pix, keep)
            version[after] += 1
            if can_drop(keyframes[after]):
                heapq.heappush(heap, (keyframes[after]["change"], after, version[after]))
        if before >= 0:
            nxt[before] = after
    return [k for i, k in enumerate(keyframes) if i not in dropped]


def dhash(thumb):
    """64-bit difference hash: signs of horizontal gradients on a 9x8 grey thumbnail."""
    grey = cv2.resize(cv2.cvtColor(thumb, cv2.COLOR_RGB2GRAY), (9, 8), interpolation=cv2.INTER_AREA).astype(np.int16)
    bits = (grey[:, 1:] > grey[:, :-1]).flatten()
    return int("".join("1" if bit else "0" for bit in bits), 2)


def mark_repeats(keyframes, thumbs, keep, params, detail):
    """same_as = the earliest earlier original within dHash distance 4 whose pixels also match: change < key_low on
    the thumbnails (talk10m slides share one layout and identical 9x8 hashes) and < DETAIL_MIN_FRAC at DETAIL_W
    (slides that differ only in a title word change 0.1 % of the thumbnail pixels)."""
    hashes = [dhash(thumbs[k["cell"]]) for k in keyframes]
    for i, keyframe in enumerate(keyframes):
        for j in range(i):
            if keyframes[j]["same_as"] is not None or bin(hashes[i] ^ hashes[j]).count("1") > DHASH_MAX_DIST:
                continue
            if changed_frac(thumbs[keyframe["cell"]], thumbs[keyframes[j]["cell"]], params["pix"], keep) >= params["key_low"]:
                continue
            if detail.change(keyframes[j]["t"], keyframe["t"]) < DETAIL_MIN_FRAC:
                keyframe["same_as"] = j
                break


def ocr_plan(arrays, keyframes, keep, params):
    """Frame indices to OCR: a cell winner that changed >= ocr_frac (non-busy pixels) since the last OCR'd one, plus
    every keyframe; above --ocr-max the non-keyframe samples are thinned uniformly."""
    thumbs, cell_index = arrays["cell_thumbs"], arrays["cell_index"]
    chosen, last = [], None
    for cell in range(len(cell_index)):
        if last is None or changed_frac(thumbs[cell], last, params["pix"], keep) >= params["ocr_frac"]:
            chosen.append(int(cell_index[cell]))
            last = thumbs[cell]
    key_indices = {k["index"] for k in keyframes}
    others = sorted(set(chosen) - key_indices)
    room = max(0, params["ocr_max"] - len(key_indices))
    if len(others) > room:
        picks = np.unique(np.round(np.linspace(0, len(others) - 1, room)).astype(int)) if room else []
        others = [others[p] for p in picks]
    return np.array(sorted(key_indices | set(others)), np.int64)


# ---------------------------------------------------------------- records, exports, onsets

def keyframe_records(run, found_keyframes):
    """analysis.json keyframes; ids K001... after the budget, same_as as an id, frame_index in the whole file."""
    records = []
    for number, k in enumerate(found_keyframes, 1):
        records.append({"id": f"K{number:03d}", "t": content_time(k["t"]), "shown_s": content_time(k["shown_s"]),
                        "frame_index": int(np.searchsorted(run.packet_times, k["t"] - FRAME_MATCH_S)),
                        "shot": f"S{k['shot'] + 1:02d}", "settled": k["settled"], "hold_s": content_time(k["hold_s"]),
                        "change_frac": round(k["change"], 4), "reason": k["reason"],
                        "same_as": None if k["same_as"] is None else f"K{k['same_as'] + 1:03d}",
                        "file": f"keyframes/K{number:03d}.jpg", "sheet": None, "cell": None, "text_ids": []})
    return records


def export_keyframes(run, keyframes, exact_times):
    """Keyframes at most 1280 px wide, from the frame cache at the export width (a rerun reads instead of
    decoding). A wider cached frame is shrunk here, so OCR can reuse it without a second decode."""
    for keyframe, source in zip(keyframes, decode.cached_frames(run, exact_times, width=export_width(run))):
        image = cv2.imread(str(source), cv2.IMREAD_COLOR)
        if image.shape[1] <= KEYFRAME_WIDTH:
            shutil.copyfile(source, run.out_path(keyframe["file"]))
            continue
        size = (KEYFRAME_WIDTH, max(2, round(KEYFRAME_WIDTH * image.shape[0] / image.shape[1] / 2) * 2))
        small = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(run.out_path(keyframe["file"])), small, [cv2.IMWRITE_JPEG_QUALITY, KEYFRAME_JPEG_QUALITY])


def date_text_by_keyframes(events, keyframes, sample_times):
    """Text first read at a keyframe sample appeared when that keyframe's screen state began (shown_s): the survey
    dates state changes to the frame, the OCR sample comes up to one cell later. Only when no other OCR sample lies
    between the two times, since such a sample did not see the text. Likewise, text gone at a keyframe sample was
    replaced when that keyframe's state began, if that is after the last sample that saw it."""
    shown_at = {k["t"]: k["shown_s"] for k in keyframes}
    samples = np.array(sorted(content_time(t) for t in sample_times))
    for event in events:
        shown = shown_at.get(event["first_s"])
        position = int(np.searchsorted(samples, event["first_s"], "left"))
        previous = samples[position - 1] if position else -math.inf
        if shown is not None and previous < shown < event["first_s"]:
            event["first_s"] = shown
        replaced = shown_at.get(event["end_s"])
        if replaced is not None and event["last_s"] < replaced < event["end_s"]:
            event["end_s"] = replaced


def shot_records(spans, keyframes):
    shots = []
    for number, (start, end) in enumerate(spans, 1):
        shot_id = f"S{number:02d}"
        shots.append({"id": shot_id, "start_s": content_time(start), "end_s": content_time(end),
                      "keyframes": [k["id"] for k in keyframes if k["shot"] == shot_id]})
    return shots


def event_records(events):
    return [{"id": f"V{number:03d}", "kind": e["kind"], "start_s": content_time(e["start_s"]),
             "end_s": content_time(e["end_s"]), "duration_ms": round((e["end_s"] - e["start_s"]) * 1000),
             "e2e_frac": round(e["e2e"], 4), "peak_frac": round(e["peak"], 4)} for number, e in enumerate(events, 1)]


def add_visual_onsets(run, arrays, found):
    """Sync's content onsets: starts of cut/transition/transient/update events and flash peaks (one per time)."""
    onsets = {e["start_s"]: e["kind"] for e in found["events"] if e["kind"] in ONSET_KINDS}
    for index in found["flashes"]:
        onsets.setdefault(float(arrays["times"][index]), "flash")
    run.visual_onsets.extend(sorted(onsets.items()))
