"""Audio features on the ffmpeg timeline (spec 7.3): activity segments, onsets, loudness, 1 s levels.

One ffmpeg pass decodes the range to 16 kHz mono float32 and measures EBU R128 loudness on the original
stream. Sample k of the PCM used here sits at `run.range.start_s + k / 16000`: when the audio stream starts
after the range start, the decode is padded at the front with silence (core rule, INTERFACES section 7).
"""
import io
import re
import subprocess
import wave

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from . import decode
from .cache import params_hash, write_atomic
from .context import content_time

SAMPLE_RATE = 16000
FEATURES_VERSION = 1                # bump when a feature below changes; invalidates cached audio sections
HOP = 160                           # 10 ms
WINDOW_HOPS = 3                     # RMS window 30 ms
FLOOR_PERCENTILE = 10
ACTIVITY_ABOVE_FLOOR_DB = 12.0
ACTIVITY_MIN_DB = -60.0
MERGE_GAP_S = 0.25
MIN_SEGMENT_S = 0.03
BLIP_MAX_S = 0.15
ONSET_FFT = 512
ONSET_HOP = 80                      # 5 ms
ONSET_MEDIAN_HALF_S = 0.5
ONSET_MAD_K = 4.0
MAD_TO_SIGMA = 1.4826
ONSET_LOCAL_MAX_S = 0.05
ONSET_MIN_FLUX = 1.0
ONSET_DEDUPE_S = 0.03
REFINE_BEFORE_S = 0.05
REFINE_AFTER_S = 0.04
REFINE_FLOOR_S = 0.03
REFINE_WINDOW_S = 0.002
FLUX_CHUNK_FRAMES = 16384           # bounds STFT memory to ~70 MB whatever the length
WAV_CHUNK_SAMPLES = 1 << 20
LOUDNESS_PATTERNS = {"integrated_lufs": re.compile(r"I:\s+(-?[\d.]+|-?inf|nan) LUFS"),
                     "lra_lu": re.compile(r"LRA:\s+(-?[\d.]+|-?inf|nan) LU\b"),
                     "true_peak_dbfs": re.compile(r"Peak:\s+(-?[\d.]+|-?inf|nan) dBFS")}


def analyze_audio(run):
    """The analysis.json `audio` section for run.range; None (with a warning) when the stream cannot be decoded."""
    entry = run.cache.entry("audio", audio_params(run), "json")
    if entry.is_hit:
        return entry.load_json()
    decoded = decode_range(run)
    if decoded is None:
        return None
    pcm, loudness = decoded
    write_wav(asr_wav_path(run), pcm)
    section = describe(pcm, loudness, run.range.start_s, run.range.duration_s)
    entry.save_json(section)
    return section


def asr_wav(run):
    """16 kHz mono wav of run.range (sample 0 at range start) for ASR; decoded again if the cached one is gone.

    analyze_audio rewrites it on every audio cache miss (also under --refresh), so an existing file is current.
    """
    path = asr_wav_path(run)
    if path.is_file():
        return path
    decoded = decode_range(run)
    if decoded is None:
        return None
    write_wav(path, decoded[0])
    return path


def audio_params(run):
    return {"range": [run.range.start_s, run.range.end_s], "stream": run.probe["audio_stream"]["index"],
            "rate": SAMPLE_RATE, "features": FEATURES_VERSION}


def asr_wav_path(run):
    return run.cache.dir / f"audio16k.{params_hash(audio_params(run))}.wav"


def decode_range(run):
    """(PCM float32 starting at run.range.start_s, loudness dict) from one ffmpeg pass, or None after a warning."""
    stream = run.probe["audio_stream"]
    seek_s = 0.0 if run.range.is_full else run.range.start_s
    seek = [] if run.range.is_full else ["-ss", f"{run.range.start_s:.6f}", "-t", f"{run.range.duration_s:.6f}"]
    source = f"0:{stream['index']}"
    command = [decode.FFMPEG, "-nostdin", "-hide_banner", "-nostats", "-v", "info", *seek, "-i", str(run.input_path),
               "-map", source, "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1",
               "-map", source, "-af", "ebur128=peak=true", "-f", "null", "-"]
    try:
        result = subprocess.run(command, capture_output=True)
    except FileNotFoundError:
        raise decode.ffmpeg_missing() from None
    log = result.stderr.decode("utf-8", "replace")
    if result.returncode != 0:
        lines = log.strip().splitlines()
        run.warn(f"audio decode failed ({lines[-1] if lines else f'exit {result.returncode}'}); audio features skipped")
        return None
    pcm = np.frombuffer(result.stdout, np.float32)
    lead = round((max(seek_s, stream["start_time_s"] or 0.0) - seek_s) * SAMPLE_RATE)
    if lead > 0:
        pcm = np.concatenate([np.zeros(lead, np.float32), pcm])
    return pcm[:round(run.range.duration_s * SAMPLE_RATE)], parse_loudness(log)


def parse_loudness(log):
    """ebur128 summary values (the last match is the summary); -inf and nan (silence) become None."""
    loudness = {}
    for key, pattern in LOUDNESS_PATTERNS.items():
        found = pattern.findall(log)
        value = float(found[-1]) if found else None
        loudness[key] = value if value is not None and np.isfinite(value) else None
    return loudness


def write_wav(path, pcm):
    """16-bit PCM wav, converted in chunks so a 2 h range never holds a second full-size float copy."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        for first in range(0, len(pcm), WAV_CHUNK_SAMPLES):
            chunk = np.clip(pcm[first:first + WAV_CHUNK_SAMPLES], -1.0, 1.0)
            out.writeframes((chunk * 32767).astype("<i2").tobytes())
    write_atomic(path, buffer.getbuffer())


def describe(pcm, loudness, start_s, duration_s):
    hop_energy = block_energy(pcm, HOP)
    window_db = rms_db(sliding_sum(hop_energy, WINDOW_HOPS), WINDOW_HOPS * HOP)
    if window_db.size == 0:
        return {"loudness": loudness, "noise_floor_db": None, "activity_ratio": 0.0, "level_db_1s": [],
                "segments": [], "onsets": []}
    floor_db = float(np.percentile(window_db, FLOOR_PERCENTILE))
    threshold_db = max(floor_db + ACTIVITY_ABOVE_FLOOR_DB, ACTIVITY_MIN_DB)
    segments = activity_segments(window_db > threshold_db, rms_db(hop_energy, HOP) > threshold_db, hop_energy, start_s)
    active_s = sum(s["end_s"] - s["start_s"] for s in segments)
    return {
        "loudness": loudness,
        "noise_floor_db": round(floor_db, 1),
        "activity_ratio": round(active_s / duration_s, 3) if duration_s > 0 else 0.0,
        "level_db_1s": level_db_1s(hop_energy),
        "segments": segments,
        "onsets": detect_onsets(pcm, start_s),
    }


def block_energy(pcm, size):
    """Sum of squares of each whole block of `size` samples (row-wise dot products, no squared copy)."""
    blocks = len(pcm) // size
    rows = pcm[:blocks * size].reshape(blocks, size)
    return np.einsum("ij,ij->i", rows, rows).astype(np.float64)


def sliding_sum(values, width):
    if len(values) < width:
        return np.zeros(0)
    return sliding_window_view(values, width).sum(axis=1)


def rms_db(energy, samples):
    return 10 * np.log10(energy / samples + 1e-12)


def activity_segments(is_window_active, is_hop_active, hop_energy, start_s):
    """Runs of active 30 ms windows, merged across gaps < 250 ms and kept when >= 30 ms (spec 7.3). Each edge
    then moves to the first / last active 10 ms hop inside its boundary window: a window reaches over up to
    20 ms of silence, which made a 100 ms beep read 150 ms. < 150 ms is a blip."""
    edges = np.flatnonzero(np.diff(np.concatenate([[0], is_window_active.astype(np.int8), [0]])))
    spans = []
    for first, stop in zip(edges[::2], edges[1::2]):
        end_hop = stop - 1 + WINDOW_HOPS            # the last active window covers WINDOW_HOPS hops
        if spans and (first - spans[-1][1]) * HOP / SAMPLE_RATE < MERGE_GAP_S:
            spans[-1][1] = end_hop
        else:
            spans.append([first, end_hop])
    segments = []
    for window_first, window_end in spans:
        if (window_end - window_first) * HOP / SAMPLE_RATE < MIN_SEGMENT_S:
            continue
        # a window's mean exceeds the threshold only if one of its hops does, so both searches always hit
        first = window_first + int(np.argmax(is_hop_active[window_first:window_first + WINDOW_HOPS]))
        end_hop = window_end - int(np.argmax(is_hop_active[window_end - WINDOW_HOPS:window_end][::-1]))
        length_s = (end_hop - first) * HOP / SAMPLE_RATE
        energy = hop_energy[first:end_hop].sum() / ((end_hop - first) * HOP)
        segments.append({"start_s": content_time(start_s + first * HOP / SAMPLE_RATE),
                         "end_s": content_time(start_s + end_hop * HOP / SAMPLE_RATE),
                         "kind": "blip" if length_s < BLIP_MAX_S else "sound",
                         "mean_db": round(float(10 * np.log10(energy + 1e-12)), 1)})
    return segments


def level_db_1s(hop_energy):
    """RMS dBFS of each second of the range; the last, partial second counts the samples it has."""
    per_second = SAMPLE_RATE // HOP
    levels = []
    for first in range(0, len(hop_energy), per_second):
        chunk = hop_energy[first:first + per_second]
        levels.append(round(float(rms_db(chunk.sum(), len(chunk) * HOP)), 1))
    return levels


def spectral_flux(pcm):
    """Positive log-magnitude flux per 5 ms frame (Hann 512), computed in chunks to bound memory."""
    count = 1 + (len(pcm) - ONSET_FFT) // ONSET_HOP
    if count < 2:
        return np.zeros(max(count, 0))
    frames = sliding_window_view(pcm, ONSET_FFT)[::ONSET_HOP]
    window = np.hanning(ONSET_FFT).astype(np.float32)
    flux = np.zeros(count)
    previous = None
    for first in range(0, count, FLUX_CHUNK_FRAMES):
        magnitude = np.log1p(100 * np.abs(np.fft.rfft(frames[first:first + FLUX_CHUNK_FRAMES] * window, axis=1)))
        stacked = magnitude if previous is None else np.vstack([previous, magnitude])
        rises = np.maximum(0, np.diff(stacked, axis=0)).sum(axis=1)
        offset = 1 if previous is None else 0
        flux[first + offset:first + offset + len(rises)] = rises
        previous = magnitude[-1:]
    return flux


def detect_onsets(pcm, start_s):
    """Flux peaks above median + 4 x 1.4826 x MAD (±0.5 s), local maxima within ±50 ms, flux > 1;
    each refined to its energy midpoint crossing, then deduplicated within 30 ms."""
    flux = spectral_flux(pcm)
    count = len(flux)
    if count < 3:
        return []
    half = int(ONSET_MEDIAN_HALF_S * SAMPLE_RATE / ONSET_HOP)
    reach = int(ONSET_LOCAL_MAX_S * SAMPLE_RATE / ONSET_HOP)
    local_max = sliding_window_view(np.pad(flux, reach, constant_values=-np.inf), 2 * reach + 1).max(axis=1)
    candidates = np.flatnonzero((flux >= local_max) & (flux > ONSET_MIN_FLUX))
    onsets = []
    for index in candidates[(candidates >= 1) & (candidates <= count - 2)]:
        around = flux[max(0, index - half):min(count, index + half)]
        median = np.median(around)
        mad = np.median(np.abs(around - median)) + 1e-6
        if flux[index] > median + ONSET_MAD_K * MAD_TO_SIGMA * mad:
            frame_center_s = (index * ONSET_HOP + ONSET_FFT / 2) / SAMPLE_RATE
            onsets.append((refine_onset(pcm, frame_center_s), float(flux[index])))
    return [{"t": round(start_s + t, 4), "strength": round(strength, 1)} for t, strength in dedupe_onsets(onsets)]


def refine_onset(pcm, guess_s):
    """Where the 2 ms energy (dB) first crosses halfway between the 30 ms pre-onset floor and the local peak."""
    first = max(0, int((guess_s - REFINE_BEFORE_S) * SAMPLE_RATE))
    segment = pcm[first:int((guess_s + REFINE_AFTER_S) * SAMPLE_RATE)].astype(np.float64)
    width = int(SAMPLE_RATE * REFINE_WINDOW_S)
    if len(segment) <= width:
        return guess_s
    energy_db = 10 * np.log10(np.convolve(segment ** 2, np.ones(width) / width, mode="same") + 1e-12)
    # Digital silence would put the floor at the log epsilon (-120 dB) and the midpoint inside codec pre-echo
    # (AAC: -60 to -80 dB, 1-4 ms early); -60 dBFS is the silence level of the activity rule as well.
    floor = max(float(np.median(energy_db[:int(REFINE_FLOOR_S * SAMPLE_RATE)])), ACTIVITY_MIN_DB)
    peak = energy_db.max()
    if peak <= floor:
        return guess_s
    return (first + int(np.argmax(energy_db > floor + 0.5 * (peak - floor)))) / SAMPLE_RATE


def dedupe_onsets(onsets):
    """Onsets closer than 30 ms merge: the earliest time is kept, with the strongest strength."""
    kept = []
    for t, strength in sorted(onsets):
        if kept and t - kept[-1][0] < ONSET_DEDUPE_S:
            kept[-1] = (kept[-1][0], max(kept[-1][1], strength))
            continue
        kept.append((t, strength))
    return kept
