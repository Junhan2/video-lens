"""The Run object every stage receives: input, probe, cache, OUT, params, analysis.json under construction."""
import functools
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from . import SCHEMA, TOOL_VERSION
from .cache import Cache, download_url, file_key, json_default, write_atomic
from .errors import EXIT_BAD_ARGS, EXIT_EMPTY_RANGE, EXIT_INPUT, VlError
from .probe import probe, rebind_path

URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)
TIME_PATTERN = re.compile(r"^(?:(\d+):)?(\d+):(\d+(?:\.\d*)?)$")
PROBE_FORMAT = 1                    # bump when the probe dict changes shape; invalidates cached probes
MOTION_DECIMALS = 4
CONTENT_DECIMALS = 3
STATUS_INTERVAL_S = 0.5
FRAME_SNAP_S = 0.0005               # the frame on screen at t: last frame <= t + 0.5 ms
FRAME_MATCH_S = 5e-5                # a content time (3 decimals) names the frame within 0.05 ms of it
AUTO_MOTION_MAX_S = 120.0
AUTO_ACTIVITY_MAX = 0.2
CONTENT_MODES = ("content", "both")
MOTION_MODES = ("motion", "both")
KEYFRAMES_BASE = 12                 # auto keyframe budget: 12 + 2 per minute, at most 150 ...
KEYFRAMES_PER_MIN = 2
KEYFRAME_BUDGET_MAX = 150
KEYFRAME_CAP_PER_MIN = 12           # ... hard cap for held slides: 12 + 12 per minute, at most 600
KEYFRAME_CAP_MAX = 600
LONG_CELL_MIN = 30                  # ranges over 30 min use 1 s survey cells
LONG_CELL_S, CELL_S = 1.0, 0.5
FAST_AUTO_MIN = 20                  # --fast auto: on above 20 min
WHISPER_MODEL_ENV = "VIDEO_LENS_WHISPER_MODEL"
OUT_OWNER_FILE = ".vl-input.json"   # which video an OUT folder belongs to
WHISPER_MODEL_DIR = Path.home() / ".local" / "share" / "whisper"
TIMING_KEYS = ("probe", "audio", "speech", "survey", "ocr", "sync", "motion_detect", "motion_fit", "views", "total")


@dataclass(frozen=True)
class Range:
    start_s: float
    end_s: float
    is_full: bool       # the whole file: decodes run without -ss/-t

    @property
    def duration_s(self):
        return self.end_s - self.start_s

    def contains(self, t):
        return self.start_s <= t < self.end_s


class Run:
    """One analyze (or view) invocation. Stages read `probe`, `params`, `range`; they return their analysis.json
    section and may append to `warnings`, `views` and `visual_onsets`."""

    def __init__(self, *, args, input_path, url, probe_info, packet_times, cache, out, rng, analysis):
        self.args = args
        self.input_path = Path(input_path)
        self.url = url
        self.probe = probe_info
        self.packet_times = packet_times
        self.cache = cache
        self.out = Path(out)
        self.range = rng
        self.analysis = analysis
        self.params = analysis["params"]
        self.warnings = analysis["warnings"]
        self.views = analysis["views"]
        self.timing = analysis["timing_s"]
        self.visual_onsets = []                 # (t, kind) from content and motion; read by sync
        self.started = time.time()
        self.stage_started = {}
        self.last_status_write = 0.0

    @classmethod
    def open_for_analyze(cls, args):
        started = time.monotonic()
        input_path, url = resolve_input(args.input)
        cache = Cache(file_key(input_path), is_refresh=args.refresh)
        info, packet_times = load_or_probe(cache, input_path)
        rng = resolve_range(args.start, args.end, info, packet_times)
        out = Path(args.out).expanduser().resolve() if args.out else Path(tempfile.gettempdir()) / "video-lens" / cache.key
        if out.exists() and not out.is_dir():
            raise VlError(EXIT_BAD_ARGS, f"--out {out} is a file", "Pass a folder path")
        out.mkdir(parents=True, exist_ok=True)
        claim_out(out, cache.key, input_path)
        params = resolve_params(args, info, rng)
        analysis = new_analysis(info, url, cache.key, rng, params)
        run = cls(args=args, input_path=input_path, url=url, probe_info=info, packet_times=packet_times,
                  cache=cache, out=out, rng=rng, analysis=analysis)
        run.timing["probe"] = round(time.monotonic() - started, 3)
        return run

    @classmethod
    def open_from_out(cls, out, args):
        """For zoom/frame/text/rows: loads OUT/analysis.json; never re-runs analysis."""
        out = Path(out).expanduser().resolve()
        try:
            analysis = json.loads((out / "analysis.json").read_text())
        except FileNotFoundError:
            raise VlError(EXIT_INPUT, f"no analysis.json in {out}", "Run vl.py analyze with --out there first") from None
        input_path = Path(analysis["input"]["path"])
        if not input_path.is_file():
            raise VlError(EXIT_INPUT, f"the analysed input is gone: {input_path}", "Run vl.py analyze again")
        if file_key(input_path) != analysis["input"]["key"]:
            raise VlError(EXIT_INPUT, f"{input_path.name} changed since it was analysed", "Run vl.py analyze again")
        cache = Cache(analysis["input"]["key"])
        info, packet_times = load_or_probe(cache, input_path)
        rng = Range(analysis["range"]["start_s"], analysis["range"]["end_s"], analysis["range"]["is_full"])
        return cls(args=args, input_path=input_path, url=analysis["input"].get("url"), probe_info=info,
                   packet_times=packet_times, cache=cache, out=out, rng=rng, analysis=analysis)

    @property
    def frame_interval_s(self):
        median_ms = self.probe["video"]["median_dt_ms"]
        return median_ms / 1000 if median_ms else None

    @property
    def display_size(self):
        return self.probe["video"]["display_width"], self.probe["video"]["display_height"]

    def set_mode(self, mode, reason):
        self.analysis["mode"] = mode
        self.analysis["mode_reason"] = reason
        requested = self.args.ocr
        self.params["content"]["ocr"] = mode in CONTENT_MODES if requested == "auto" else requested == "on"

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    @contextmanager
    def timer(self, key):
        """Adds the block's wall time to analysis.json timing_s[key]."""
        started = time.monotonic()
        try:
            yield
        finally:
            self.timing[key] = round(self.timing.get(key, 0.0) + time.monotonic() - started, 3)

    def progress(self, stage, done, total):
        """OUT/status.json (throttled to 2 writes/s); a stderr line too with --progress."""
        now = time.time()
        stage_start = self.stage_started.setdefault(stage, now)
        if done < total and now - self.last_status_write < STATUS_INTERVAL_S:
            return
        self.last_status_write = now
        eta = round((now - stage_start) / done * (total - done), 1) if done else None
        status = {"stage": stage, "done": done, "total": total, "eta_s": eta,
                  "started_at": datetime.fromtimestamp(self.started).astimezone().isoformat(timespec="seconds")}
        write_atomic(self.out / "status.json", json.dumps(status).encode())
        if getattr(self.args, "progress", False):
            print(f"video-lens: {stage} {done}/{total}" + (f" eta {eta:.0f} s" if eta is not None else ""), file=sys.stderr)

    def check_frame_count(self, decoded):
        """Full-range decodes only (spec 5.1): showinfo frames vs container packets."""
        expected = self.probe["video"]["frames"]
        is_match = decoded == expected
        self.analysis["video"]["pts_count_check"] = "ok" if is_match else "mismatch"
        if not is_match:
            self.warn(f"pts_count_mismatch: decoded {decoded} frames, the container lists {expected}; times use showinfo pts")

    def frame_index(self, t):
        """Index of the frame on screen at t: the last frame whose time is <= t (0.5 ms tolerance); -1 before the first."""
        return int(np.searchsorted(self.packet_times, t + FRAME_SNAP_S, side="right")) - 1

    def frames_in_range(self):
        return int(np.count_nonzero((self.packet_times >= self.range.start_s) & (self.packet_times < self.range.end_s)))

    def snap_time(self, t):
        """The frame on screen at t: the last frame whose time is <= t (0.5 ms tolerance)."""
        index = self.frame_index(t)
        if index < 0 or t > self.probe["duration_s"]:
            raise VlError(EXIT_EMPTY_RANGE, f"no frame at {t:.4f} s (video spans {self.packet_times[0]:.4f} to "
                          f"{self.probe['duration_s']:.4f} s)", "Pick a time inside the video")
        return float(self.packet_times[index])

    def out_path(self, rel):
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def rel(self, path):
        return Path(path).resolve().relative_to(self.out).as_posix()

    def finish_timing(self):
        self.timing["total"] = round(time.time() - self.started, 3)

    def record_cache(self):
        """analysis.json `cache`: which stage files were reused (hits) or computed (misses) so far."""
        self.analysis["cache"] = {"dir": str(self.cache.dir), "hits": list(self.cache.hits), "misses": list(self.cache.misses)}

    def write_analysis(self):
        self.record_cache()
        text = json.dumps(self.analysis, ensure_ascii=False, indent=1, default=json_default) + "\n"
        write_atomic(self.out / "analysis.json", text.encode())
        return text


def resolve_input(text):
    """A URL is downloaded with yt-dlp into the cache; a path is made absolute (symlinks kept)."""
    if URL_PATTERN.match(text):
        return download_url(text), text
    path = Path(os.path.abspath(os.path.expanduser(text)))
    if not path.is_file():
        raise VlError(EXIT_INPUT, f"input not found: {path}", "Check the path")
    return path, None


def claim_out(out, key, input_path):
    """Refuse an OUT that belongs to another video, so parallel sessions sharing one path never mix results.

    The claim is created with O_EXCL: of two runs racing for an empty folder, exactly one owns it.
    """
    owner = out / OUT_OWNER_FILE
    record = json.dumps({"key": key, "input": str(input_path)}).encode()
    owner_key, owner_input = read_out_owner(owner) if owner.exists() else analysed_input(out)
    if owner_key not in (key, None):
        raise_foreign_out(out, owner_input)
    if owner.exists():
        write_atomic(owner, record)
        return
    try:
        fd = os.open(owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        owner_key, owner_input = read_out_owner(owner)
        if owner_key not in (key, None):
            raise_foreign_out(out, owner_input)
        return
    with os.fdopen(fd, "wb") as fh:
        fh.write(record)


def raise_foreign_out(out, owner_input):
    raise VlError(EXIT_BAD_ARGS, f"--out {out} holds the analysis of another video ({Path(owner_input or '?').name})",
                  "Pass a folder of its own, or omit --out (the default folder is unique to each video)")


def read_out_owner(owner):
    try:
        claim = json.loads(owner.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    return claim.get("key"), claim.get("input")


def analysed_input(out):
    """(key, path) of an analysis.json written before OUT claims existed; (None, None) when there is none."""
    try:
        recorded = json.loads((out / "analysis.json").read_text())["input"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None, None
    return recorded.get("key"), recorded.get("path")


@functools.cache
def tool_versions():
    try:
        first_line = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.split("\n", 1)[0]
    except FileNotFoundError:
        first_line = "missing"
    return {"tool": TOOL_VERSION, "probe_format": PROBE_FORMAT, "ffmpeg": first_line.split(" ")[2] if first_line.count(" ") >= 2 else first_line,
            "cv2": cv2.__version__, "numpy": np.__version__}


def load_or_probe(cache, input_path):
    versions = tool_versions()
    cached = cache.load_probe(versions)
    if cached:
        info, packet_times = cached
        return rebind_path(info, input_path), packet_times
    info, packet_times = probe(input_path)
    cache.save_probe(info, packet_times, versions)
    return info, packet_times


def resolve_range(start, end, info, packet_times):
    """[start, end) on the ffmpeg timeline; end is clamped to the duration. No frame inside -> exit 4."""
    duration = info["duration_s"]
    start_s = start or 0.0
    end_s = min(end, duration) if end is not None else duration
    if start_s >= duration:
        raise VlError(EXIT_EMPTY_RANGE, f"--start {start_s:g} s is at or after the end of the video ({duration:.3f} s)",
                      "Pick a start inside the video")
    if end_s <= start_s:
        raise VlError(EXIT_EMPTY_RANGE, f"empty range {start_s:g}-{end_s:g} s", "Make --end larger than --start")
    has_frame = np.any((packet_times >= start_s) & (packet_times < end_s))
    if not has_frame:
        raise VlError(EXIT_EMPTY_RANGE, f"no video frame between {start_s:g} and {end_s:g} s", "Widen the range")
    return Range(round(start_s, 6), round(end_s, 6), start_s <= 0 and end_s >= duration)


def resolve_params(args, info, rng):
    """analysis.json `params` with every `auto` resolved, except content.ocr which needs the mode (Run.set_mode)."""
    minutes = rng.duration_s / 60
    explicit_keyframes = None if args.max_keyframes == "auto" else int(args.max_keyframes)
    return {
        "roi": validated_roi(args.roi, info),
        "content": {
            "pix": args.pix, "cut_frac": args.cut_frac, "cut_ratio": args.cut_ratio, "key_frac": args.key_frac,
            "key_low": args.key_low, "ocr_frac": args.ocr_frac,
            "cell_s": (LONG_CELL_S if minutes > LONG_CELL_MIN else CELL_S) if args.cell == "auto" else float(args.cell),
            "fast": minutes > FAST_AUTO_MIN if args.fast == "auto" else args.fast == "on",
            "ocr": None, "ocr_langs": args.ocr_langs.split(","), "ocr_max": args.ocr_max, "jobs": args.jobs,
            "max_keyframes": explicit_keyframes or min(KEYFRAME_BUDGET_MAX, KEYFRAMES_BASE + int(KEYFRAMES_PER_MIN * minutes)),
            "keyframe_cap": explicit_keyframes or min(KEYFRAME_CAP_MAX, KEYFRAMES_BASE + int(KEYFRAME_CAP_PER_MIN * minutes)),
            "timeline_chars": args.timeline_chars,
        },
        "motion": {
            "det_side": args.det_side, "work_side": args.work_side, "pix_threshold": args.pix_threshold,
            "gap_ms": args.gap_ms, "noise_maxd": args.noise_maxd, "joint": not args.no_joint, "dpr": args.dpr,
            "motion_mode": args.motion_mode, "max_elements": args.max_elements, "micro": args.micro,
            "sheets": args.sheets, "events": args.events, "easings": str(Path(args.easings).resolve()) if args.easings else None,
        },
        "speech": {"asr": args.asr, "lang": args.lang, "whisper_model": resolve_whisper_model(args.whisper_model)},
        "report": {"budget_chars": args.budget_chars},
    }


def validated_roi(roi, info):
    if roi is None:
        return None
    x, y, w, h = roi
    width, height = info["video"]["display_width"], info["video"]["display_height"]
    if x + w > width or y + h > height:
        raise VlError(EXIT_BAD_ARGS, f"--roi {x},{y},{w},{h} lies outside the {width}x{height} frame",
                      "Give x,y,w,h in display pixels of the source")
    return [x, y, w, h]


def resolve_whisper_model(flag):
    """--whisper-model, else $VIDEO_LENS_WHISPER_MODEL, else the largest real ggml model in ~/.local/share/whisper."""
    chosen = flag or os.environ.get(WHISPER_MODEL_ENV)
    if chosen:
        return str(Path(chosen).expanduser().resolve())
    if not WHISPER_MODEL_DIR.is_dir():
        return None
    models = [p for p in WHISPER_MODEL_DIR.glob("ggml-*.bin") if "for-tests" not in p.name]
    return str(max(models, key=lambda p: p.stat().st_size)) if models else None


def new_analysis(info, url, key, rng, params):
    return {
        "schema": SCHEMA,
        "tool_version": TOOL_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "input": {"path": info["path"], "url": url, "key": key, "size_bytes": info["size_bytes"]},
        "video": dict(info["video"]),
        "audio_stream": info["audio_stream"],
        "subtitle_streams": info["subtitle_streams"],
        "mode": None, "mode_reason": None,
        "range": {"start_s": rng.start_s, "end_s": rng.end_s, "is_full": rng.is_full},
        "params": params,
        "timing_s": dict.fromkeys(TIMING_KEYS, 0.0),
        "cache": {},
        "audio": None, "speech": None, "sync": None, "content": None, "motion": None,
        "views": [],
        "suggested_next": [],
        "warnings": [],
    }


def parse_time(text):
    """Seconds (`12.5`) or `[hh:]mm:ss[.s]` (`1:02.5`, `1:00:02`); negative times are rejected."""
    text = str(text).strip()
    match = TIME_PATTERN.match(text)
    if match:
        hours, minutes, seconds = match.groups()
        value = int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)
    else:
        value = float(text)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"time must be >= 0: {text}")
    return value


def clock(t, decimals=1):
    """03:12.4 below one hour, 1:03:12.4 above."""
    minutes, seconds = divmod(round(max(0.0, t), decimals), 60)
    hours, minutes = divmod(int(minutes), 60)
    width = 3 + decimals if decimals else 2
    body = f"{minutes:02d}:{seconds:0{width}.{decimals}f}"
    return f"{hours}:{body}" if hours else body


def motion_time(t):
    return round(float(t), MOTION_DECIMALS)


def content_time(t):
    return round(float(t), CONTENT_DECIMALS)
