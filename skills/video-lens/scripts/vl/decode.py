"""ffmpeg decoding (spec 5.1, 5.2): rawvideo pipes timed by showinfo pts, window select, single-frame export.

Every frame time is `showinfo pts_time + S`, where S is the input `-ss` of that decode (0 without one),
so all decodes share one timeline whose 0 is `format.start_time`.
"""
import re
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .errors import EXIT_DEPENDENCY, EXIT_INPUT, VlError

FFMPEG = "ffmpeg"
EXPORT_WORKERS = 8
EXPORT_LEAD_S = 0.0005        # `-ss (t - 0.0005)` lands exactly on the frame whose pts is t, also on VFR
WINDOW_PAD_S = 0.0001         # select windows are inclusive and widened by 0.1 ms against float rounding
BYTES_PER_PIXEL = {"gray": 1, "rgb24": 3, "bgr24": 3}
SHOWINFO_PTS = re.compile(rb"pts_time:\s*(-?[0-9.]+(?:e-?[0-9]+)?)")
SHOWINFO_SIZE = re.compile(rb" s:(\d+)x(\d+)")
STDERR_TAIL_LINES = 6
ERROR_LINE = re.compile(r"error|invalid|fail|not found", re.IGNORECASE)
LOG_PREFIX = re.compile(r"^(\[[^\]]*\]\s*)+")


@dataclass(frozen=True)
class Frame:
    index: int              # position within this stream, 0-based
    t: float                # seconds on the ffmpeg timeline
    image: np.ndarray       # H x W x 3 (bgr24/rgb24) or H x W (gray); read-only, copy before editing in place
    window: int | None      # index into `windows` when the stream was opened with windows


@dataclass(frozen=True)
class ExportJob:
    t: float                # exact frame time from a decode of this file
    out: Path               # .png, .jpg or .jpeg
    width: int | None = None                        # downscale to this width; never upscales
    roi: tuple[int, int, int, int] | None = None    # x, y, w, h in display px, applied before scaling


class ShowinfoReader(threading.Thread):
    """Drains ffmpeg stderr so the pipe never blocks; collects showinfo pts and the output frame size."""

    def __init__(self, pipe):
        super().__init__(daemon=True)
        self.pipe = pipe
        self.pts = []
        self.size = None
        self.tail = deque(maxlen=STDERR_TAIL_LINES)
        self.first_error = None
        self.is_done = False
        self.changed = threading.Condition()

    def run(self):
        for line in iter(self.pipe.readline, b""):
            match = SHOWINFO_PTS.search(line)
            if match is None:
                if b"showinfo" not in line:
                    self.keep_message(LOG_PREFIX.sub("", line.decode("utf-8", "replace").strip()))
                continue
            with self.changed:
                if self.size is None:
                    size = SHOWINFO_SIZE.search(line)
                    self.size = (int(size.group(1)), int(size.group(2)))
                self.pts.append(float(match.group(1)))
                self.changed.notify_all()
        with self.changed:
            self.is_done = True
            self.changed.notify_all()

    def keep_message(self, text):
        self.tail.append(text)
        if self.first_error is None and ERROR_LINE.search(text):
            self.first_error = text

    def wait_size(self):
        with self.changed:
            self.changed.wait_for(lambda: self.size is not None or self.is_done)
            return self.size

    def pts_at(self, index):
        """Frame i is used only once len(pts) > i (spec 5.2)."""
        with self.changed:
            self.changed.wait_for(lambda: len(self.pts) > index or self.is_done)
            if len(self.pts) <= index:
                raise VlError(EXIT_INPUT, f"ffmpeg wrote frame {index} without a showinfo time", "Report this file")
            return self.pts[index]

    def last_error(self):
        """ffmpeg's first error-looking line names the cause; its last line is usually `Conversion failed!`."""
        return self.first_error or (self.tail[-1] if self.tail else "no ffmpeg message")


class FrameStream:
    """Iterates the frames of one ffmpeg rawvideo decode.

    vf       filters placed after the optional select and before `format=<pix_fmt>,showinfo`
    start_s  input `-ss` (frames with t >= start_s); end_s: input `-t` (frames with t < end_s)
    windows  [(a, b), ...] absolute seconds; adds `select='between(t,a,b)+...'` as the first filter
    fast     `-skip_loop_filter all -flags2 fast` (content survey only; never for motion)
    noref    `-skip_frame noref` (content survey with --fast only)
    """

    def __init__(self, path, vf="", *, pix_fmt="bgr24", start_s=None, end_s=None, windows=None,
                 fast=False, noref=False, stream_index=None, on_complete=None):
        if pix_fmt not in BYTES_PER_PIXEL:
            raise ValueError(f"pix_fmt must be one of {sorted(BYTES_PER_PIXEL)}")
        self.path = Path(path)
        self.vf = vf
        self.pix_fmt = pix_fmt
        self.start_s = start_s or 0.0
        self.end_s = end_s
        self.windows = list(windows) if windows else None
        self.is_fast = fast
        self.is_noref = noref
        self.stream_index = stream_index
        self.on_complete = on_complete
        self.times = []
        self.size = None
        self.seconds = 0.0

    @property
    def is_full_range(self):
        """Every frame of the file decoded: the frame-count cross-check of spec 5.1 applies."""
        return not self.start_s and self.end_s is None and not self.windows and not self.is_noref

    def command(self):
        cmd = [FFMPEG, "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info"]
        if self.is_fast:
            cmd += ["-skip_loop_filter", "all", "-flags2", "fast"]
        if self.is_noref:
            cmd += ["-skip_frame", "noref"]
        if self.start_s:
            cmd += ["-ss", f"{self.start_s:.6f}"]
        if self.end_s is not None:
            cmd += ["-t", f"{self.end_s - self.start_s:.6f}"]
        stream = f"0:{self.stream_index}" if self.stream_index is not None else "0:v:0"
        filters = [select_filter(self.windows, self.start_s)] if self.windows else []
        filters += [self.vf] if self.vf else []
        filters += [f"format={self.pix_fmt}", "showinfo=checksum=0"]
        return cmd + ["-i", str(self.path), "-map", stream, "-vf", ",".join(filters),
                      "-fps_mode", "passthrough", "-f", "rawvideo", "-"]

    def __iter__(self):
        started = time.monotonic()
        proc = start_process(self.command())
        reader = ShowinfoReader(proc.stderr)
        reader.start()
        try:
            yield from self.read_frames(proc, reader)
        except BaseException:
            proc.kill()     # consumer stopped early (GeneratorExit) or the read failed
            raise
        finally:
            proc.wait()
            reader.join()
            proc.stdout.close()
            proc.stderr.close()
            self.seconds = time.monotonic() - started
        if proc.returncode != 0:
            raise VlError(EXIT_INPUT, f"ffmpeg decode of {self.path.name} failed: {reader.last_error()}",
                          "Check that the file plays; re-encode it with ffmpeg if it is damaged")
        if self.on_complete:
            self.on_complete(self)

    def read_frames(self, proc, reader):
        self.size = reader.wait_size()
        if self.size is None:
            return
        width, height = self.size
        depth = BYTES_PER_PIXEL[self.pix_fmt]
        shape = (height, width) if depth == 1 else (height, width, depth)
        frame_bytes = width * height * depth
        index = 0
        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                return
            t = reader.pts_at(index) + self.start_s
            self.times.append(t)
            yield Frame(index, t, np.frombuffer(raw, np.uint8).reshape(shape), self.window_of(t))
            index += 1

    def window_of(self, t):
        if not self.windows:
            return None
        for number, (a, b) in enumerate(self.windows):
            if a - WINDOW_PAD_S <= t <= b + WINDOW_PAD_S:
                return number
        return None


def select_filter(windows, offset_s):
    """select's `t` is the decode's own time, which starts at the input -ss; windows are shifted onto it."""
    terms = [f"between(t,{a - offset_s - WINDOW_PAD_S:.6f},{b - offset_s + WINDOW_PAD_S:.6f})" for a, b in windows]
    return "select='" + "+".join(terms) + "'"


def stream_run(run, vf="", **options):
    """FrameStream over the run's input, defaulting to the run's --start/--end range.

    A full-file decode (no range, windows or noref) cross-checks its frame count against the container.
    """
    if "start_s" not in options and "end_s" not in options and not run.range.is_full:
        options["start_s"], options["end_s"] = run.range.start_s, run.range.end_s
    options.setdefault("stream_index", run.probe["video"]["index"])
    stream = FrameStream(run.input_path, vf, **options)
    stream.on_complete = lambda done: run.check_frame_count(len(done.times)) if done.is_full_range else None
    return stream


def ffmpeg_missing():
    return VlError(EXIT_DEPENDENCY, "ffmpeg not found", "Install ffmpeg (brew install ffmpeg)")


def start_process(cmd):
    try:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise ffmpeg_missing() from None


def run_ffmpeg(args, what):
    """One-shot `ffmpeg -nostdin -v error ARGS`; returns stdout bytes, raises VlError with ffmpeg's last line."""
    try:
        result = subprocess.run([FFMPEG, "-nostdin", "-v", "error", *args], capture_output=True)
    except FileNotFoundError:
        raise ffmpeg_missing() from None
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise VlError(EXIT_INPUT, f"{what} failed: {message[-1] if message else 'exit %d' % result.returncode}",
                      "Check that the file plays; re-encode it with ffmpeg if it is damaged")
    return result.stdout


def single_frame_args(path, t, width=None, roi=None, stream_index=None):
    filters = []
    if roi:
        x, y, w, h = roi
        filters.append(f"crop={w}:{h}:{x}:{y}")
    if width:
        filters.append(f"scale='min({width},iw)':-2:flags=area")
    args = ["-ss", f"{max(0.0, t - EXPORT_LEAD_S):.6f}", "-i", str(path),
            "-map", f"0:{stream_index}" if stream_index is not None else "0:v:0", "-frames:v", "1"]
    return args + (["-vf", ",".join(filters)] if filters else [])


def export_frame(path, job, stream_index=None):
    job.out.parent.mkdir(parents=True, exist_ok=True)
    quality = ["-q:v", "2"] if job.out.suffix.lower() in (".jpg", ".jpeg") else []
    run_ffmpeg(single_frame_args(path, job.t, job.width, job.roi, stream_index) + quality + ["-update", "1", "-y", str(job.out)],
               f"frame export at {job.t:.4f} s")
    if not job.out.is_file() or job.out.stat().st_size == 0:
        raise VlError(EXIT_INPUT, f"no frame at {job.t:.4f} s in {Path(path).name}", "Pick a time inside the video")
    return job.out


def export_frames(path, jobs, stream_index=None, workers=EXPORT_WORKERS):
    """Exports each job's frame with its own `ffmpeg -ss` call, 8 in parallel; returns the written paths in order."""
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(lambda job: export_frame(path, job, stream_index), jobs))


def read_frame(path, t, width=None, roi=None, stream_index=None):
    data = run_ffmpeg(single_frame_args(path, t, width, roi, stream_index) + ["-f", "image2pipe", "-c:v", "png", "-"],
                      f"frame read at {t:.4f} s")
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data else None
    if image is None:
        raise VlError(EXIT_INPUT, f"no frame at {t:.4f} s in {Path(path).name}", "Pick a time inside the video")
    return image


def read_frames(path, times, width=None, roi=None, stream_index=None, workers=EXPORT_WORKERS):
    """BGR arrays of the frames at exact times, decoded in parallel, without touching the disk."""
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(lambda t: read_frame(path, t, width, roi, stream_index), times))


def cached_frames(run, times, width=None):
    """JPEG exports in the cache as `frames/<t_ms>_<w>.jpg`; only missing ones are decoded (with --refresh, each
    file once per run)."""
    label = width or run.probe["video"]["display_width"]
    folder = run.cache.frames_dir
    jobs = [ExportJob(t, folder / f"{t * 1000:.3f}_{label}.jpg", width) for t in times]
    is_stale = lambda path: run.cache.is_refresh and path not in run.cache.fresh_frames
    missing = list({job.out: job for job in jobs if not job.out.is_file() or is_stale(job.out)}.values())
    export_frames(run.input_path, missing, run.probe["video"]["index"])
    run.cache.fresh_frames.update(job.out for job in missing)
    return [job.out for job in jobs]
