"""ffprobe: streams, format, sorted packet times, VFR stats, rotation and sidecar subtitles (spec 7.1, 5.1)."""
import json
import os
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np

from .errors import EXIT_DEPENDENCY, EXIT_INPUT, VlError

FFPROBE = "ffprobe"
STREAM_ENTRIES = ("format=duration,start_time,format_name"
                  ":stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,start_time,nb_frames,"
                  "sample_rate,channels:stream_side_data=rotation:stream_tags=language:stream_disposition=attached_pic")
VFR_RATIO = 1.5
SIDECAR_SUFFIXES = (".srt", ".vtt")


def probe(path):
    """Returns (info, packet_times). packet_times are sorted seconds on the ffmpeg timeline (0 = format.start_time)."""
    path = Path(os.path.abspath(path))
    if not path.is_file():
        raise VlError(EXIT_INPUT, f"input not found: {path}", "Check the path")
    info = json.loads(run_ffprobe(["-show_entries", STREAM_ENTRIES, "-of", "json"], path))
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and not is_cover_art(s)), None)
    if video is None:
        raise VlError(EXIT_INPUT, f"no video stream in {path.name}", "Pass a file that contains video")
    fmt = info.get("format", {})
    start = to_float(fmt.get("start_time"), 0.0)
    packet_times = read_packet_times(path, video["index"], start)
    duration = to_float(fmt.get("duration"), None)
    if duration is None:
        duration = estimate_duration(packet_times)
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "duration_s": round(duration, 6),
        "start_time_s": round(start, 6),
        "video": describe_video(video, start, duration, packet_times),
        "audio_stream": describe_audio(streams, start),
        "subtitle_streams": [{"index": s["index"], "lang": s.get("tags", {}).get("language"), "codec": s.get("codec_name")}
                             for s in streams if s.get("codec_type") == "subtitle"],
        "sidecar_subtitles": [str(p) for p in sidecar_paths(path)],
    }, packet_times


def rebind_path(info, path):
    """A cached probe of a renamed or copied file (same key): path and sidecars follow the current location."""
    path = Path(path)
    return dict(info, path=str(path), sidecar_subtitles=[str(p) for p in sidecar_paths(path)])


def run_ffprobe(args, path):
    try:
        result = subprocess.run([FFPROBE, "-v", "error", *args, str(path)], capture_output=True, text=True)
    except FileNotFoundError:
        raise VlError(EXIT_DEPENDENCY, "ffprobe not found", "Install ffmpeg (brew install ffmpeg)") from None
    if result.returncode != 0:
        reason = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else f"exit {result.returncode}"
        raise VlError(EXIT_INPUT, f"cannot read {path.name}: {reason}", "Check that the path is a readable video file")
    return result.stdout


def is_cover_art(stream):
    return bool(stream.get("disposition", {}).get("attached_pic"))


def read_packet_times(path, stream_index, format_start):
    """Presentation times of the stream's packets, sorted; packets flagged discard (edit-list trims) are skipped."""
    out = run_ffprobe(["-select_streams", str(stream_index), "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0"], path)
    times = []
    for line in out.splitlines():
        pts_text, _, flags = line.partition(",")
        if pts_text in ("", "N/A") or "D" in flags:
            continue
        times.append(float(pts_text) - format_start)
    return np.sort(np.array(times, dtype=np.float64))


def describe_video(stream, format_start, duration, packet_times):
    rotation = rotation_of(stream)
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    is_sideways = rotation % 180 == 90
    steps = np.diff(packet_times)
    steps = steps[steps > 0]
    median_dt = float(np.median(steps)) if steps.size else None
    return {
        "index": stream["index"],
        "codec": stream.get("codec_name"),
        "width": width, "height": height,
        "display_width": height if is_sideways else width,
        "display_height": width if is_sideways else height,
        "rotation": rotation,
        "duration_s": round(duration, 6),
        "start_time_s": round(format_start, 6),
        "video_start_offset_s": round(to_float(stream.get("start_time"), format_start) - format_start, 6),
        "frames": int(packet_times.size),
        "avg_fps": average_fps(stream.get("avg_frame_rate"), packet_times),
        "is_vfr": bool(steps.size and steps.max() > VFR_RATIO * median_dt),
        "median_dt_ms": round(median_dt * 1000, 3) if median_dt else None,
        "min_dt_ms": round(float(steps.min()) * 1000, 3) if steps.size else None,
        "max_dt_ms": round(float(steps.max()) * 1000, 3) if steps.size else None,
        "pts_source": "showinfo",
        "pts_count_check": None,
    }


def describe_audio(streams, format_start):
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        return None
    return {
        "index": audio["index"],
        "codec": audio.get("codec_name"),
        "sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
        "channels": audio.get("channels"),
        "lang": audio.get("tags", {}).get("language"),
        "start_time_s": round(to_float(audio.get("start_time"), format_start) - format_start, 6),
    }


def rotation_of(stream):
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            return int(round(float(side["rotation"])))
    return 0


def average_fps(rate_text, packet_times):
    if rate_text and rate_text != "0/0":
        rate = Fraction(rate_text)
        if rate > 0:
            return round(float(rate), 3)
    if packet_times.size < 2:
        return None
    return round((packet_times.size - 1) / float(packet_times[-1] - packet_times[0]), 3)


def estimate_duration(packet_times):
    if packet_times.size == 0:
        raise VlError(EXIT_INPUT, "the video stream has no frames", "Check that the file is not truncated")
    steps = np.diff(packet_times)
    return float(packet_times[-1] + (np.median(steps) if steps.size else 0.0))


def sidecar_paths(path):
    return [path.with_suffix(suffix) for suffix in SIDECAR_SUFFIXES if path.with_suffix(suffix).is_file()]


def to_float(text, default):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default
