"""Core fixtures, plus the encoders every fixtures_*.py shares (spec 10: libx264 crf 18, yuv420p;
VFR copies via mpdecimate=hi=64:lo=32:frac=0.33 -fps_mode vfr).

Each build_* function writes one file at the path it is given (Ctx.fixture passes a temp path with the
final extension) and returns nothing.
"""
import shutil
import subprocess

import cv2
import numpy as np

CRF = "18"
MOTION_SIZE = (480, 270)
MOTION_FPS = 60
MOTION_SECONDS = 3.0
MOVE_SPANS = ((0.3, 0.8), (1.5, 2.2))      # the card moves in these spans and rests elsewhere
MOVE_PX = 180
BACKGROUND_BGR = (245, 243, 240)
KOREAN_NAME = "한글 이름 영상.mp4"
TS_OFFSET_S = 5.0
VIDEO_OFFSET_S = 0.022


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *map(str, args)], check=True)


def encode_bgr(frames, path, size, fps):
    """Pipes BGR uint8 frames into libx264 crf 18 yuv420p at a constant frame rate."""
    width, height = size
    proc = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                             "-s", f"{width}x{height}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-crf", CRF,
                             "-pix_fmt", "yuv420p", str(path)], stdin=subprocess.PIPE)
    for frame in frames:
        proc.stdin.write(np.ascontiguousarray(frame, np.uint8).tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed to encode {path}")


def make_vfr(src, dst):
    """Drops near-still frames the way a macOS screen recording does."""
    ffmpeg("-i", src, "-vf", "mpdecimate=hi=64:lo=32:frac=0.33", "-fps_mode", "vfr",
           "-c:v", "libx264", "-crf", CRF, "-pix_fmt", "yuv420p", dst)


def card_x(t):
    """Left edge of the card: two linear moves of MOVE_PX, so every moving frame differs from its neighbours."""
    x = 40.0
    for start, end in MOVE_SPANS:
        x += MOVE_PX / 2 * float(np.clip((t - start) / (end - start), 0, 1))
    return x


def motion_frames():
    width, height = MOTION_SIZE
    for index in range(int(MOTION_SECONDS * MOTION_FPS)):
        frame = np.full((height, width, 3), BACKGROUND_BGR, np.uint8)
        cv2.putText(frame, "video-lens core", (16, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (60, 60, 60), 1, cv2.LINE_AA)
        x = int(round(card_x(index / MOTION_FPS)))
        cv2.rectangle(frame, (x, 90), (x + 120, 200), (200, 120, 40), -1)
        cv2.rectangle(frame, (x + 12, 110), (x + 100, 122), (255, 255, 255), -1)
        yield frame


def build_motion_cfr(path):
    encode_bgr(motion_frames(), path, MOTION_SIZE, MOTION_FPS)


def build_motion_vfr(cfr_path):
    return lambda path: make_vfr(cfr_path, path)


def build_ts_shifted(base_path):
    """Same packets, container start_time = 5 s (format.start_time is 0 of the timeline)."""
    return lambda path: ffmpeg("-i", base_path, "-c", "copy", "-output_ts_offset", TS_OFFSET_S, path)


def build_video_offset(base_path):
    """Audio starts at 0, video 22 ms later: video_start_offset_s = 0.022."""
    return lambda path: ffmpeg("-itsoffset", VIDEO_OFFSET_S, "-i", base_path,
                               "-f", "lavfi", "-i", f"sine=frequency=440:duration={MOTION_SECONDS}:sample_rate=48000",
                               "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", path)


def build_rotated(base_path):
    """Display-matrix rotation of 90 degrees; coded frames stay landscape."""
    return lambda path: ffmpeg("-display_rotation", "90", "-i", base_path, "-c", "copy", path)


def build_audio_only(path):
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=48000", "-c:a", "pcm_s16le", path)


def build_copy(src_path):
    return lambda path: shutil.copyfile(src_path, path)
