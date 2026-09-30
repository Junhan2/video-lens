#!/usr/bin/env python3
"""HELD-OUT hard evaluation set for video-analysis skills (companion to ../evalset/gen.py).

Three clips whose motion, colour, audio, scene and text facts are known exactly:

  h1_list_dpr2.mp4             settings list, six rows staggered 45 ms (heavy overlap) with a custom,
                               non-named cubic-bezier; CSS viewport 1280x800 at deviceScaleFactor 2 (file
                               2560x1600, truth in CSS px); 30 fps CFR; gradient background; shimmer loop.
  h2_colour_overshoot_vfr.mp4  colour-only button change (blue -> red of equal BT.709 AND BT.601 luma),
                               badge pop that overshoots scale 1, bottom drawer; 60 fps source made VFR
                               with ffmpeg mpdecimate (-fps_mode vfr).
  h3_lecture10_ko.mp4          10-minute Korean lecture, 24 slides, irregular hard cuts, macOS `say -v Yuna`
                               narration starting 0.3-1.0 s after each cut, long silences.

    python3 gen_hard.py            # build all three, verify, write truth_hard.json
    python3 gen_hard.py h2         # rebuild one clip; the others are taken from _work/<name>.truth.json

Rendering is the evalset method: real headless Chrome over the DevTools pipe; for every output frame every
animation on the page is paused and seeked to currentTime = t, then one Page.captureScreenshot. The page CSS
is the literal source of truth; Chrome's getComputedStyle values are compared with an independent solver.
Helpers are copied from ../evalset/gen.py rather than imported: importing it would put its Chrome profile
under evalset/_work, and this set must not write there.
"""
import base64
import colorsys
import json
import math
import os
import re
import shutil
import subprocess
import sys
import wave

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "_work")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PIX_THRESHOLD = 8      # level difference that counts as "changed" (same as evalset / motion-timing)
MIN_PIXELS = 20        # changed pixels needed before a region counts as changed
X264 = ["-c:v", "libx264", "-preset", "slow", "-crf", "14", "-pix_fmt", "yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
BT709_VF = "scale=out_color_matrix=bt709:out_range=tv"
NAMES = {"h1": "h1_list_dpr2", "h2": "h2_colour_overshoot_vfr", "h3": "h3_lecture10_ko"}


# ---------------------------------------------------------------- helpers (from evalset/gen.py)

def run(cmd, text=True):
    r = subprocess.run(cmd, capture_output=True, text=text)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr[-2000:]}")
    return r


def cubic_bezier(p1x, p1y, p2x, p2y):
    """CSS cubic-bezier timing function: x (time fraction) -> y (progress). Bisection, 1e-15 in t."""
    def coord(t, a, b):
        return 3 * a * (1 - t) ** 2 * t + 3 * b * (1 - t) * t * t + t ** 3

    def ease(x):
        if x <= 0:
            return 0.0
        if x >= 1:
            return 1.0
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if coord(mid, p1x, p2x) < x:
                lo = mid
            else:
                hi = mid
        return coord((lo + hi) / 2, p1y, p2y)
    return ease


def progress(t_s, start_s, dur_ms, ease):
    return ease((t_s - start_s) * 1000.0 / dur_ms)


def peak_slope(p):
    """Max dy/dx of cubic-bezier p from the exact parametric derivative."""
    t = np.linspace(0, 1, 20001)
    dx = 3 * (1 - t) ** 2 * p[0] + 6 * (1 - t) * t * (p[2] - p[0]) + 3 * t ** 2 * (1 - p[2])
    dy = 3 * (1 - t) ** 2 * p[1] + 6 * (1 - t) * t * (p[3] - p[1]) + 3 * t ** 2 * (1 - p[3])
    ok = dx > 1e-12
    return float(np.max(dy[ok] / dx[ok]))


def easing_block(p, name):
    return {"css": f"cubic-bezier({p[0]}, {p[1]}, {p[2]}, {p[3]})", "p1x": p[0], "p1y": p[1],
            "p2x": p[2], "p2y": p[3], "name": name,
            "progress_at_x": {f"{x:.1f}": round(cubic_bezier(*p)(x), 4) for x in np.linspace(0, 1, 11)}}


def parse_matrix(s):
    """getComputedStyle transform -> (a, b, c, d, e, f)."""
    if s in ("none", ""):
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    return tuple(float(v) for v in re.findall(r"-?[\d.]+(?:e-?\d+)?", s.split("(", 1)[1]))


def parse_rgb(s):
    return tuple(float(v) for v in re.findall(r"-?[\d.]+", s)[:3])


def luma709(c):
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def luma601(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


class Chrome:
    def __init__(self, width, height, dpr=1):
        profile = os.path.join(WORK, "chrome-profile")
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()

        def child_fds():  # Chrome reads commands on fd 3 and writes replies on fd 4
            hi_r, hi_w = os.dup(r_in), os.dup(w_out)
            os.dup2(hi_r, 3)
            os.dup2(hi_w, 4)
        self.proc = subprocess.Popen(
            [CHROME, "--headless", "--remote-debugging-pipe", "--disable-gpu", "--hide-scrollbars",
             "--force-color-profile=srgb", "--no-first-run", "--no-default-browser-check",
             "--disable-extensions", f"--user-data-dir={profile}", f"--window-size={width},{height}", "about:blank"],
            preexec_fn=child_fds, close_fds=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(r_in)
        os.close(w_out)
        self.w = os.fdopen(w_in, "wb", buffering=0)
        self.r = os.fdopen(r_out, "rb", buffering=0)
        self.buf, self.nid, self.events, self.sid = b"", 0, [], None
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.sid = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": dpr, "mobile": False})
        self.call("Page.enable")

    def _read(self):
        while b"\0" not in self.buf:
            chunk = self.r.read(1 << 20)
            if not chunk:
                raise RuntimeError("Chrome closed the DevTools pipe")
            self.buf += chunk
        msg, self.buf = self.buf.split(b"\0", 1)
        return json.loads(msg)

    def call(self, method, params=None):
        self.nid += 1
        msg = {"id": self.nid, "method": method, "params": params or {}}
        if self.sid and not method.startswith(("Target.", "Browser.")):
            msg["sessionId"] = self.sid
        self.w.write(json.dumps(msg).encode() + b"\0")
        while True:
            reply = self._read()
            if reply.get("id") == self.nid:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply["result"]
            self.events.append(reply)

    def open(self, path):
        self.call("Page.navigate", {"url": "file://" + path})
        while not any(e.get("method") == "Page.loadEventFired" for e in self.events):
            self.events.append(self._read())
        self.events.clear()
        self.js("document.fonts.ready.then(() => true)")

    def js(self, expr):
        r = self.call("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
        if "exceptionDetails" in r:
            raise RuntimeError(f"JS error: {r['exceptionDetails']}")
        return r["result"].get("value")

    def screenshot(self, path):
        with open(path, "wb") as f:
            f.write(base64.b64decode(self.call("Page.captureScreenshot", {"format": "png"})["data"]))

    def close(self):
        try:
            self.call("Browser.close")
        except Exception:
            pass
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


SEEK_JS = """(async () => {
  const T = %r;
  document.getAnimations().forEach(a => { a.pause(); a.currentTime = T; });
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const out = {};
  document.querySelectorAll('[data-probe]').forEach(el => {
    const cs = getComputedStyle(el);
    out[el.dataset.probe] = {transform: cs.transform, opacity: parseFloat(cs.opacity), bg: cs.backgroundColor};
  });
  return out;
})()"""

RECT_JS = """(() => { const out = {};
  document.querySelectorAll('[data-probe]').forEach(el => { const r = el.getBoundingClientRect();
    out[el.dataset.probe] = {x: r.x, y: r.y, w: r.width, h: r.height}; });
  return out; })()"""


def render_animation(name, html, width, height, fps, n_frames, rect_at_ms, dpr=1):
    """Render n_frames of `html` at t = n / fps. Returns (frames_dir, per-frame states, CSS-px rects)."""
    vdir = os.path.join(WORK, name)
    frames = os.path.join(vdir, "frames")
    shutil.rmtree(vdir, ignore_errors=True)
    os.makedirs(frames)
    page = os.path.join(vdir, "page.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(html)
    chrome = Chrome(width, height, dpr)
    try:
        chrome.open(page)
        chrome.js(SEEK_JS % float(rect_at_ms))
        rects = chrome.js(RECT_JS)
        states = []
        for n in range(n_frames):
            states.append(chrome.js(SEEK_JS % (n * 1000.0 / fps)))
            chrome.screenshot(os.path.join(frames, f"{n:05d}.png"))
    finally:
        chrome.close()
    return frames, states, rects


def render_stills(name, pages, width, height):
    vdir = os.path.join(WORK, name)
    shutil.rmtree(vdir, ignore_errors=True)
    os.makedirs(vdir)
    chrome = Chrome(width, height)
    out = []
    try:
        for i, html in enumerate(pages):
            page = os.path.join(vdir, f"slide{i + 1:02d}.html")
            with open(page, "w", encoding="utf-8") as f:
                f.write(html)
            chrome.open(page)
            png = os.path.join(vdir, f"slide{i + 1:02d}.png")
            chrome.screenshot(png)
            out.append(png)
    finally:
        chrome.close()
    return vdir, out


# ---------------------------------------------------------------- probing and pixel checks

def ffprobe(path):
    j = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path]).stdout)
    pts = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time",
               "-of", "csv=p=0", path]).stdout.split()
    pts = [p.strip(",") for p in pts if p.strip(",")]
    info = {"format": j["format"]["format_name"], "duration_s": round(float(j["format"]["duration"]), 6),
            "size_bytes": int(j["format"]["size"])}
    for s in j["streams"]:
        if s["codec_type"] == "video":
            info["video"] = {"codec": s["codec_name"], "profile": s.get("profile"), "width": s["width"],
                             "height": s["height"], "pix_fmt": s["pix_fmt"], "r_frame_rate": s["r_frame_rate"],
                             "avg_frame_rate": s["avg_frame_rate"], "time_base": s["time_base"],
                             "nb_frames": len(pts), "color_space": s.get("color_space"),
                             "first_pts_s": float(pts[0]), "last_pts_s": float(pts[-1])}
        elif s["codec_type"] == "audio":
            info["audio"] = {"codec": s["codec_name"], "sample_rate": int(s["sample_rate"]),
                             "channels": s["channels"], "duration_s": round(float(s["duration"]), 6),
                             "start_time_s": float(s.get("start_time", 0))}
    return info, [float(p) for p in pts]


def read_frames(path):
    """cv2's own decode (its YUV->BGR matrix is BT.601; geometry fits use it, colour checks do not)."""
    cap = cv2.VideoCapture(path)
    frames = []
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        frames.append(fr)
    return frames


def read_rgb_bt709(path, w, h):
    """Colour-exact decode: ffmpeg rgb24 with the file's own BT.709 limited-range matrix."""
    raw = run(["ffmpeg", "-v", "error", "-i", path, "-vf",
               "scale=in_color_matrix=bt709:in_range=tv:out_range=pc,format=rgb24",
               "-fps_mode", "passthrough", "-f", "rawvideo", "-"], text=False).stdout
    n = w * h * 3
    return [np.frombuffer(raw[i:i + n], np.uint8).reshape(h, w, 3) for i in range(0, len(raw), n)]


def read_y_plane(path, w, h):
    """The luma plane exactly as stored in the file (limited range codes 16-235)."""
    raw = run(["ffmpeg", "-v", "error", "-i", path, "-fps_mode", "passthrough", "-f", "rawvideo",
               "-pix_fmt", "yuv420p", "-"], text=False).stdout
    n = w * h * 3 // 2
    return [np.frombuffer(raw[i:i + w * h], np.uint8).reshape(h, w) for i in range(0, len(raw), n)]


def changed_pixels(a, b, box):
    x0, y0, x1, y1 = box
    d = cv2.absdiff(a[y0:y1, x0:x1], b[y0:y1, x0:x1])
    if d.ndim == 3:
        d = d.max(axis=2)
    return int((d > PIX_THRESHOLD).sum())


def observed_window(frames, pts, box, t_from=0.0, t_to=1e9):
    """What a perfect frame-differencing tool can see inside `box` (file-pixel box x0,y0,x1,y1)."""
    moving = [n for n in range(1, len(frames)) if t_from - 1e-9 <= pts[n] <= t_to + 1e-9
              and changed_pixels(frames[n], frames[n - 1], box) >= MIN_PIXELS]
    if not moving:
        return None
    first, last = moving[0], moving[-1]
    return {"first_change_frame": first, "first_change_pts_s": round(pts[first], 6),
            "last_still_before_pts_s": round(pts[first - 1], 6),
            "last_change_frame": last, "last_change_pts_s": round(pts[last], 6),
            "changed_frames": len(moving), "box_xyxy": [int(v) for v in box]}


def fit_warp(frame, bg, ref, box, make_matrix, values, margin=48):
    """Least-squares fit, on luma inside `box`, of  frame = bg + alpha * warp_v(ref - bg).
    `ref` shows the element in a known state, `bg` the page without it. Returns (best v, alpha)."""
    x0, y0, x1, y1 = box
    X0, Y0 = max(x0 - margin, 0), max(y0 - margin, 0)
    X1, Y1 = x1 + margin, y1 + margin

    def luma(im):
        return cv2.cvtColor(np.ascontiguousarray(im[Y0:Y1, X0:X1]), cv2.COLOR_BGR2GRAY).astype(np.float32)
    inner = (slice(y0 - Y0, y1 - Y0), slice(x0 - X0, x1 - X0))
    d = (luma(frame) - luma(bg))[inner].ravel().astype(np.float64)
    c_src = luma(ref) - luma(bg)
    best = None
    for v in values:
        c = cv2.warpAffine(c_src, make_matrix(v, X0, Y0), (c_src.shape[1], c_src.shape[0]),
                           flags=cv2.INTER_LINEAR)[inner].ravel().astype(np.float64)
        cc = float((c * c).sum())
        if cc == 0:
            continue
        a = float((d * c).sum()) / cc
        r = float(((d - a * c) ** 2).mean())
        if best is None or r < best[0]:
            best = (r, float(v), a)
    return best[1], best[2]


def fit_coarse_fine(frame, bg, ref, box, make_matrix, lo, hi, coarse, fine, margin=48):
    v, _ = fit_warp(frame, bg, ref, box, make_matrix, np.arange(lo, hi + coarse / 2, coarse), margin)
    return fit_warp(frame, bg, ref, box, make_matrix,
                    np.arange(max(lo, v - 2 * coarse), min(hi, v + 2 * coarse) + fine / 2, fine), margin)


def shift_x(v, X0, Y0):
    return np.float32([[1, 0, v], [0, 1, 0]])


def shift_y(v, X0, Y0):
    return np.float32([[1, 0, 0], [0, 1, v]])


def scale_about(cx, cy):
    return lambda s, X0, Y0: np.float32([[s, 0, (cx - X0) * (1 - s)], [0, s, (cy - Y0) * (1 - s)]])


def decode_audio(path, rate=48000):
    """First channel only (the h3 track is mono)."""
    raw = run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-af", "pan=mono|c0=c0", "-ar", str(rate),
               "-f", "f32le", "-"], text=False).stdout
    return np.frombuffer(raw, np.float32)


def speech_spans(x, rate, win_s=0.010, floor_dbfs=-45.0):
    """10 ms RMS windows above floor_dbfs -> active. Returns active window start times."""
    hop = int(win_s * rate)
    n = len(x) // hop
    rms = np.sqrt((x[: n * hop].reshape(n, hop).astype(np.float64) ** 2).mean(axis=1) + 1e-20)
    db = 20 * np.log10(rms)
    return np.where(db > floor_dbfs)[0] * win_s, win_s


def check(name, measured, expected, tol, unit):
    ok = abs(measured - expected) <= tol
    return {"check": name, "measured": round(measured, 4), "expected": round(expected, 4),
            "tolerance": tol, "unit": unit, "pass": bool(ok)}


def write_wav(path, mono, rate=48000):
    pcm = np.clip(np.round(mono * 32767), -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def rect_px(r, dpr=1):
    return {k: round(v, 2) for k, v in r.items()} if dpr == 1 else {k: round(v * dpr, 2) for k, v in r.items()}


# ---------------------------------------------------------------- named easing curves (h1 uniqueness check)

NAMED_BEZIERS = {
    "CSS linear": (0, 0, 1, 1), "CSS ease": (0.25, 0.1, 0.25, 1), "CSS ease-in": (0.42, 0, 1, 1),
    "CSS ease-out": (0, 0, 0.58, 1), "CSS ease-in-out": (0.42, 0, 0.58, 1),
    "easings.net easeInSine": (0.12, 0, 0.39, 0), "easings.net easeOutSine": (0.61, 1, 0.88, 1),
    "easings.net easeInOutSine": (0.37, 0, 0.63, 1), "easings.net easeInQuad": (0.11, 0, 0.5, 0),
    "easings.net easeOutQuad": (0.5, 1, 0.89, 1), "easings.net easeInOutQuad": (0.45, 0, 0.55, 1),
    "easings.net easeInCubic": (0.32, 0, 0.67, 0), "easings.net easeOutCubic": (0.33, 1, 0.68, 1),
    "easings.net easeInOutCubic": (0.65, 0, 0.35, 1), "easings.net easeInQuart": (0.5, 0, 0.75, 0),
    "easings.net easeOutQuart": (0.25, 1, 0.5, 1), "easings.net easeInOutQuart": (0.76, 0, 0.24, 1),
    "easings.net easeInQuint": (0.64, 0, 0.78, 0), "easings.net easeOutQuint": (0.22, 1, 0.36, 1),
    "easings.net easeInOutQuint": (0.83, 0, 0.17, 1), "easings.net easeInExpo": (0.7, 0, 0.84, 0),
    "easings.net easeOutExpo": (0.16, 1, 0.3, 1), "easings.net easeInOutExpo": (0.87, 0, 0.13, 1),
    "easings.net easeInCirc": (0.55, 0, 1, 0.45), "easings.net easeOutCirc": (0, 0.55, 0.45, 1),
    "easings.net easeInOutCirc": (0.85, 0, 0.15, 1), "easings.net easeInBack": (0.36, 0, 0.66, -0.56),
    "easings.net easeOutBack": (0.34, 1.56, 0.64, 1), "easings.net easeInOutBack": (0.68, -0.6, 0.32, 1.6),
    "legacy easings.net easeInSine": (0.47, 0, 0.745, 0.715), "legacy easings.net easeOutSine": (0.39, 0.575, 0.565, 1),
    "legacy easings.net easeInOutSine": (0.445, 0.05, 0.55, 0.95), "legacy easings.net easeInQuad": (0.55, 0.085, 0.68, 0.53),
    "legacy easings.net easeOutQuad": (0.25, 0.46, 0.45, 0.94), "legacy easings.net easeInOutQuad": (0.455, 0.03, 0.515, 0.955),
    "legacy easings.net easeInCubic": (0.55, 0.055, 0.675, 0.19), "legacy easings.net easeOutCubic": (0.215, 0.61, 0.355, 1),
    "legacy easings.net easeInOutCubic": (0.645, 0.045, 0.355, 1), "legacy easings.net easeInQuart": (0.895, 0.03, 0.685, 0.22),
    "legacy easings.net easeOutQuart": (0.165, 0.84, 0.44, 1), "legacy easings.net easeInOutQuart": (0.77, 0, 0.175, 1),
    "legacy easings.net easeInQuint": (0.755, 0.05, 0.855, 0.06), "legacy easings.net easeOutQuint": (0.23, 1, 0.32, 1),
    "legacy easings.net easeInOutQuint": (0.86, 0, 0.07, 1), "legacy easings.net easeInExpo": (0.95, 0.05, 0.795, 0.035),
    "legacy easings.net easeOutExpo": (0.19, 1, 0.22, 1), "legacy easings.net easeInOutExpo": (1, 0, 0, 1),
    "legacy easings.net easeInCirc": (0.6, 0.04, 0.98, 0.335), "legacy easings.net easeOutCirc": (0.075, 0.82, 0.165, 1),
    "legacy easings.net easeInOutCirc": (0.785, 0.135, 0.15, 0.86), "legacy easings.net easeInBack": (0.6, -0.28, 0.735, 0.045),
    "legacy easings.net easeOutBack": (0.175, 0.885, 0.32, 1.275), "legacy easings.net easeInOutBack": (0.68, -0.55, 0.265, 1.55),
    "Material standard": (0.4, 0, 0.2, 1), "Material decelerate": (0, 0, 0.2, 1),
    "Material accelerate": (0.4, 0, 1, 1), "Material sharp": (0.4, 0, 0.6, 1),
    "Material 3 standard": (0.2, 0, 0, 1), "Material 3 standard-decelerate": (0, 0, 0, 1),
    "Material 3 standard-accelerate": (0.3, 0, 1, 1), "Material 3 emphasized-decelerate": (0.05, 0.7, 0.1, 1),
    "Material 3 emphasized-accelerate": (0.3, 0, 0.8, 0.15), "iOS spring-ish": (0.32, 0.72, 0, 1),
    # Open Props (argyleink/open-props src/props.easing.js); --ease-5 is the nearest curve to H1_EASE (0.061)
    "Open Props --ease-1": (0.25, 0, 0.5, 1), "Open Props --ease-2": (0.25, 0, 0.4, 1),
    "Open Props --ease-3": (0.25, 0, 0.3, 1), "Open Props --ease-4": (0.25, 0, 0.2, 1),
    "Open Props --ease-5": (0.25, 0, 0.1, 1), "Open Props --ease-out-1": (0, 0, 0.75, 1),
    "Open Props --ease-out-2": (0, 0, 0.5, 1), "Open Props --ease-out-3": (0, 0, 0.3, 1),
    "Open Props --ease-out-4": (0, 0, 0.1, 1), "Open Props --ease-out-5": (0, 0, 0, 1),
    "Open Props --ease-in-out-1": (0.1, 0, 0.9, 1), "Open Props --ease-in-out-2": (0.3, 0, 0.7, 1),
    "Open Props --ease-in-out-3": (0.5, 0, 0.5, 1), "Open Props --ease-in-out-4": (0.7, 0, 0.3, 1),
    "Open Props --ease-in-out-5": (0.9, 0, 0.1, 1),
}
# Material 3 'emphasized' is a two-segment path, not a single cubic-bezier.
M3_EMPHASIZED = [((0, 0), (0.05, 0), (0.133333, 0.06), (0.166666, 0.4)),
                 ((0.166666, 0.4), (0.208333, 0.82), (0.25, 1), (1, 1))]
_C1 = 1.70158
PENNER = {  # exact easings.net functions (the beziers above are approximations of these)
    "Penner easeInSine": lambda x: 1 - np.cos(x * np.pi / 2), "Penner easeOutSine": lambda x: np.sin(x * np.pi / 2),
    "Penner easeInOutSine": lambda x: -(np.cos(np.pi * x) - 1) / 2,
    "Penner easeInExpo": lambda x: np.where(x == 0, 0, 2 ** (10 * x - 10)),
    "Penner easeOutExpo": lambda x: np.where(x == 1, 1, 1 - 2 ** (-10 * x)),
    "Penner easeInOutExpo": lambda x: np.where(x == 0, 0, np.where(x == 1, 1, np.where(
        x < 0.5, 2 ** (20 * x - 10) / 2, (2 - 2 ** (-20 * x + 10)) / 2))),
    "Penner easeInCirc": lambda x: 1 - np.sqrt(1 - x ** 2), "Penner easeOutCirc": lambda x: np.sqrt(1 - (x - 1) ** 2),
    "Penner easeInOutCirc": lambda x: np.where(x < 0.5, (1 - np.sqrt(np.clip(1 - (2 * x) ** 2, 0, 1))) / 2,
                                               (np.sqrt(np.clip(1 - (-2 * x + 2) ** 2, 0, 1)) + 1) / 2),
    "Penner easeInBack": lambda x: (_C1 + 1) * x ** 3 - _C1 * x ** 2,
    "Penner easeOutBack": lambda x: 1 + (_C1 + 1) * (x - 1) ** 3 + _C1 * (x - 1) ** 2,
    "Penner easeInOutBack": lambda x: np.where(
        x < 0.5, ((2 * x) ** 2 * ((_C1 * 1.525 + 1) * 2 * x - _C1 * 1.525)) / 2,
        ((2 * x - 2) ** 2 * ((_C1 * 1.525 + 1) * (x * 2 - 2) + _C1 * 1.525) + 2) / 2),
}
for _n, _k in (("Quad", 2), ("Cubic", 3), ("Quart", 4), ("Quint", 5)):
    PENNER[f"Penner easeIn{_n}"] = (lambda k: lambda x: x ** k)(_k)
    PENNER[f"Penner easeOut{_n}"] = (lambda k: lambda x: 1 - (1 - x) ** k)(_k)
    PENNER[f"Penner easeInOut{_n}"] = (lambda k: lambda x: np.where(
        x < 0.5, 2 ** (k - 1) * x ** k, 1 - (-2 * x + 2) ** k / 2))(_k)


def _segment_xy(seg, n=40001):
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = seg
    t = np.linspace(0, 1, n)
    b = [(1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3]
    return b[0] * x0 + b[1] * x1 + b[2] * x2 + b[3] * x3, b[0] * y0 + b[1] * y1 + b[2] * y2 + b[3] * y3


def bezier_on(p, xs):
    x, y = _segment_xy(((0, 0), (p[0], p[1]), (p[2], p[3]), (1, 1)))
    return np.interp(xs, x, y)


def named_curve_distances(p):
    """max |y_named(x) - y_p(x)| for every named curve, on a dense grid and on the scorer's 11 points."""
    dense, eleven = np.linspace(0, 1, 2001), np.linspace(0, 1, 11)
    curves = {k: (lambda xs, q=q: bezier_on(q, xs)) for k, q in NAMED_BEZIERS.items()}
    xa, ya = _segment_xy(M3_EMPHASIZED[0])
    xb, yb = _segment_xy(M3_EMPHASIZED[1])
    curves["Material 3 emphasized (path)"] = lambda xs: np.interp(xs, np.r_[xa, xb[1:]], np.r_[ya, yb[1:]])
    for k, f in PENNER.items():
        curves[k] = lambda xs, f=f: np.asarray(f(xs), float)
    rows = []
    for name, f in curves.items():
        row = {"name": name, "max_abs_dy_dense": round(float(np.max(np.abs(f(dense) - bezier_on(p, dense)))), 4),
               "max_abs_dy_11pt": round(float(np.max(np.abs(f(eleven) - bezier_on(p, eleven)))), 4)}
        if name in NAMED_BEZIERS:
            row["control_points_within_0.1"] = all(abs(a - b) <= 0.1 for a, b in zip(NAMED_BEZIERS[name], p))
        rows.append(row)
    return sorted(rows, key=lambda r: r["max_abs_dy_dense"])


# ---------------------------------------------------------------- page templates

BASE_CSS = """*{box-sizing:border-box}
html,body{margin:0;overflow:hidden;color:#16181d;
  font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif;-webkit-font-smoothing:antialiased}"""

H1_EASE = (0.34, 0.12, 0.08, 0.98)
H1_ROWS = [  # title, description, right-hand control, icon colour
    ("Notifications", "Email and push alerts for mentions", '<span class="tog"></span>', "#4c6ef5"),
    ("Privacy", "Who can see your profile and activity", "Team only <i>›</i>", "#12b76a"),
    ("Appearance", "Theme, density and font size", "System <i>›</i>", "#f79009"),
    ("Language &amp; region", "Korean · Asia/Seoul", "<i>›</i>", "#ee46bc"),
    ("Connected apps", "Slack, GitHub and 3 more", "5 <i>›</i>", "#7a5af8"),
    ("Two-step verification", "Authenticator app is set up", '<span class="tog"></span>', "#0ba5ec"),
]
H1_START_MS, H1_STAGGER_MS, H1_DUR_MS, H1_SHIFT = 900, 45, 520, 24
H1_GLINT_W, H1_SKEL_W, H1_GLINT_MS = 120, 250, 1200

H1_HTML = ("""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Settings</title><style>
""" + BASE_CSS + """
html,body{width:1280px;height:800px;background:linear-gradient(180deg,#f7f9fd 0%,#dce4f2 100%)}
aside{position:absolute;left:0;top:0;bottom:0;width:232px;background:rgba(255,255,255,.7);border-right:1px solid #dbe2ee;
  padding:28px 22px}
aside b{display:block;font-size:17px;margin-bottom:30px} aside div{font-size:14px;color:#5b6475;margin:0 0 16px}
aside .on{color:#16181d;font-weight:600}
.head{position:absolute;left:288px;top:48px} .head h1{margin:0 0 6px;font-size:28px;line-height:34px;letter-spacing:-.02em}
.head p{margin:0;font-size:15px;color:#5b6475}
.list{position:absolute;left:288px;top:150px;width:620px}
.row{height:64px;margin-bottom:12px;background:#fff;border:1px solid #dde3ee;border-radius:12px;display:flex;
  align-items:center;gap:16px;padding:0 20px;box-shadow:0 1px 2px rgba(16,24,40,.05);
  animation:row-in 520ms cubic-bezier(0.34,0.12,0.08,0.98) both}
""" + "\n".join(f".row:nth-child({i + 1}){{animation-delay:{H1_START_MS + H1_STAGGER_MS * i}ms}}" for i in range(6)) + """
@keyframes row-in{from{opacity:0;transform:translateX(-24px)}to{opacity:1;transform:translateX(0)}}
.ic{width:36px;height:36px;border-radius:9px;flex:none}
.tx b{display:block;font-size:15px;line-height:20px} .tx span{font-size:13px;line-height:18px;color:#667085}
.ctl{margin-left:auto;font-size:13px;color:#667085;display:flex;align-items:center;gap:8px} .ctl i{font-style:normal;font-size:18px}
.tog{display:block;width:40px;height:24px;border-radius:12px;background:#3b82f6;position:relative}
.tog:after{content:"";position:absolute;right:3px;top:3px;width:18px;height:18px;border-radius:50%;background:#fff}
.panel{position:absolute;left:948px;top:150px;width:292px;height:196px;background:#fff;border:1px solid #dde3ee;
  border-radius:12px;padding:20px}
.panel h2{margin:0 0 4px;font-size:15px} .panel p{margin:0 0 18px;font-size:13px;color:#667085}
.skeleton{position:relative;width:250px;height:80px;overflow:hidden}
.bar{height:16px;border-radius:8px;background:#e1e6ef;margin-bottom:16px}
.glint{position:absolute;left:0;top:0;width:120px;height:80px;
  background:linear-gradient(90deg,rgba(255,255,255,0),rgba(255,255,255,.85),rgba(255,255,255,0));
  animation:shimmer 1200ms linear infinite}
@keyframes shimmer{from{transform:translateX(-120px)}to{transform:translateX(250px)}}
</style></head><body>
<aside><b>Northwind</b><div>Overview</div><div>Projects</div><div>Team</div><div class="on">Settings</div></aside>
<div class="head"><h1>Settings</h1><p>Manage your account, security and connected services.</p></div>
<div class="list">
""" + "\n".join(
    f'<div class="row" data-probe="row{i + 1}"><span class="ic" style="background:{c}"'
    + (' data-probe="icon1"' if i == 0 else "") + f'></span><div class="tx"><b>{t}</b><span>{d}</span></div>'
    f'<div class="ctl">{ctl}</div></div>' for i, (t, d, ctl, c) in enumerate(H1_ROWS)) + """
</div>
<div class="panel"><h2>Recent activity</h2><p>Loading the last 30 days…</p>
<div class="skeleton" data-probe="skeleton"><div class="bar" style="width:250px"></div><div class="bar" style="width:196px"></div>
<div class="bar" style="width:228px"></div><div class="glint" data-probe="glint"></div></div></div>
</body></html>""")

# Blue -> dark red with equal BT.709 AND BT.601 luma (within 0.25 levels), so every common greyscale path misses
# the change. Kept away from 0/255: at #1414ff -> #840c00 the chroma of white label pixels clipped on decode and
# 24-39 text-edge pixels changed > 8 grey levels vs frame 0 (above the 20 px detector floor); here at most 9.
H2_BLUE, H2_RED = (40, 36, 220), (118, 31, 43)
H2_BADGE_EASE, H2_DRAWER_EASE, H2_LINEAR = (0.34, 1.56, 0.64, 1), (0.32, 0.72, 0, 1), (0, 0, 1, 1)
H2_DRAWER_H = 300


def hexc(c):
    return "#%02x%02x%02x" % tuple(c)


H2_HTML = ("""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Recorder</title><style>
""" + BASE_CSS + """
html,body{width:1280px;height:800px;background:#eef0f3}
header{position:absolute;left:0;top:0;right:0;height:64px;background:#fff;border-bottom:1px solid #e4e7ec;display:flex;
  align-items:center;gap:36px;padding:0 40px}
.logo{font-weight:700;font-size:18px} nav{display:flex;gap:24px;font-size:14px;color:#667085} nav .on{color:#16181d;font-weight:600}
.bell{position:absolute;right:40px;top:14px;width:36px;height:36px;border-radius:9px;background:#f2f4f7;display:flex;
  align-items:center;justify-content:center}
.badge{position:absolute;right:28px;top:6px;width:26px;height:26px;border-radius:50%;background:#e5484d;color:#fff;
  font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center;box-shadow:0 0 0 2px #fff;
  transform-origin:50% 50%;animation:badge-pop 350ms cubic-bezier(0.34,1.56,0.64,1) 1000ms both}
@keyframes badge-pop{from{transform:scale(0)}to{transform:scale(1)}}
h1{position:absolute;left:64px;top:96px;margin:0;font-size:26px;letter-spacing:-.02em}
.sub{position:absolute;left:64px;top:136px;margin:0;font-size:15px;color:#667085}
.card{position:absolute;left:64px;top:190px;width:760px;height:270px;background:#fff;border:1px solid #e4e7ec;
  border-radius:14px;padding:22px}
.preview{height:180px;border-radius:10px;background:linear-gradient(135deg,#d9e2ff,#f1f4ff)}
.card small{display:block;margin-top:14px;font-size:14px;color:#475467}
.rec{position:absolute;left:880px;top:190px;width:200px;height:52px;border-radius:12px;background:BLUE_HEX;color:#fff;
  font-size:16px;font-weight:600;display:flex;align-items:center;justify-content:center;
  animation:rec-colour 200ms linear 600ms both}
@keyframes rec-colour{from{background-color:BLUE_HEX}to{background-color:RED_HEX}}
.ghost{position:absolute;left:880px;top:258px;width:200px;height:48px;border-radius:12px;border:1px solid #d0d5dd;
  background:#fff;font-size:15px;display:flex;align-items:center;justify-content:center;color:#344054}
.drawer{position:absolute;left:260px;bottom:0;width:760px;height:300px;background:#fff;border:1px solid #d0d5dd;
  border-bottom:0;border-radius:18px 18px 0 0;padding:14px 32px 0;
  animation:drawer-in 450ms cubic-bezier(0.32,0.72,0,1) 1800ms both}
@keyframes drawer-in{from{transform:translateY(100%)}to{transform:translateY(0)}}
.handle{width:48px;height:5px;border-radius:3px;background:#c4cad4;margin:0 auto 22px}
.drawer h2{margin:0 0 6px;font-size:20px} .drawer p{margin:0 0 18px;font-size:14px;color:#667085}
.thumbs{display:flex;gap:12px;margin-bottom:22px} .thumbs span{width:120px;height:68px;border-radius:8px}
.acts{display:flex;gap:12px} .acts span{height:42px;padding:0 18px;border-radius:10px;display:flex;align-items:center;
  font-size:14px;font-weight:600} .acts .dark{background:#111827;color:#fff} .acts .line{border:1px solid #d0d5dd}
</style></head><body>
<header><div class="logo">Northwind Capture</div><nav><span class="on">Recorder</span><span>Library</span><span>Shared</span></nav>
<div class="bell"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#344054" stroke-width="2"
 stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/>
 <path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg></div>
<div class="badge" data-probe="badge">3</div></header>
<h1>Screen recorder</h1><p class="sub">Capture a window, a region or the full screen.</p>
<div class="card"><div class="preview"></div><small>Source: Built-in Retina Display · 2560 × 1600 · 60 fps</small></div>
<div class="rec" data-probe="rec">Record</div><div class="ghost">Settings</div>
<div class="drawer" data-probe="drawer"><div class="handle"></div><h2>Recording saved</h2>
<p>capture-2026-09-27.mov · 00:42 · 18.6 MB</p>
<div class="thumbs"><span style="background:#c7d7fe"></span><span style="background:#a6f4c5"></span>
<span style="background:#fedf89"></span><span style="background:#fecdca"></span><span style="background:#d9d6fe"></span></div>
<div class="acts"><span class="dark">Share link</span><span class="line">Open folder</span></div></div>
</body></html>""").replace("BLUE_HEX", hexc(H2_BLUE)).replace("RED_HEX", hexc(H2_RED))

SLIDE_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>
%s
html,body{width:1280px;height:720px;background:#fbfbfa}
.bar{position:absolute;left:96px;top:190px;width:64px;height:8px;border-radius:4px;background:%s}
h1{position:absolute;left:96px;top:228px;margin:0;font-size:60px;line-height:76px;letter-spacing:-.02em;
  font-family:"Apple SD Gothic Neo",-apple-system,sans-serif;font-weight:700}
p{position:absolute;left:98px;top:334px;margin:0;font-size:30px;line-height:40px;color:#475467}
footer{position:absolute;right:72px;bottom:48px;font-size:20px;color:#98a2b3}
</style></head><body><div class="bar"></div><h1>%s</h1><p>%s</p><footer>%s</footer></body></html>"""
SLIDE_COLORS = ["#4c6ef5", "#12b76a", "#f79009", "#ee46bc", "#7a5af8", "#0ba5ec"]
LECTURE = [  # (title_ko, line_en, narration_ko)
    ("웹 성능, 왜 중요한가", "Why web performance matters",
     "페이지 로딩이 1초 늦어질 때마다 전환율이 약 7퍼센트 떨어진다는 조사가 있습니다."),
    ("렌더링 파이프라인", "The rendering pipeline",
     "브라우저는 HTML을 파싱해 DOM을 만들고, 스타일 계산과 레이아웃을 거쳐 화면에 픽셀을 그립니다."),
    ("프레임 예산 16.7밀리초", "A 16.7 ms frame budget",
     "초당 60 프레임을 유지하려면 한 프레임을 16.7밀리초 안에 끝내야 합니다. 실제로 스크립트에 쓸 수 있는 시간은 10밀리초 정도입니다."),
    ("메인 스레드를 막는 작업", "Long tasks block the main thread",
     "50밀리초가 넘는 작업을 long task라고 부르며, 그동안에는 클릭에 반응할 수 없습니다."),
    ("핵심 웹 지표 세 가지", "Three Core Web Vitals",
     "Core Web Vitals는 LCP, INP, CLS 세 가지 지표로 사용자 경험을 요약합니다."),
    ("가장 큰 콘텐츠 표시 시간", "Largest Contentful Paint",
     "LCP는 2.5초 이내면 좋음, 4초를 넘으면 나쁨으로 분류됩니다."),
    ("다음 페인트까지의 상호작용", "Interaction to Next Paint",
     "INP는 사용자의 입력부터 다음 화면 갱신까지 걸린 시간이며, 200밀리초 이하를 목표로 합니다."),
    ("누적 레이아웃 이동", "Cumulative Layout Shift",
     "CLS 점수는 0.1 이하로 유지해야 합니다. 이미지에 width와 height를 지정하는 것만으로도 크게 줄어듭니다."),
    ("측정 도구 고르기", "Choosing a measurement tool",
     "실험실 측정에는 Lighthouse를, 실제 사용자 데이터에는 RUM 도구를 씁니다."),
    ("네트워크 요청 줄이기", "Fewer network requests",
     "첫 화면에 필요한 요청이 30개를 넘으면 연결을 재사용해도 대기 시간이 쌓입니다."),
    ("이미지 최적화", "Optimizing images",
     "같은 사진이라도 AVIF로 바꾸면 JPEG보다 용량이 절반 가까이 줄어듭니다."),
    ("지연 로딩", "Lazy loading below the fold",
     "화면 아래쪽 이미지는 loading 속성을 lazy로 지정해 나중에 불러옵니다."),
    ("자바스크립트 번들 나누기", "Splitting the JavaScript bundle",
     "번들 크기가 300킬로바이트를 넘으면 코드 분할을 검토하세요. 경로별로 나누면 첫 로딩이 가벼워집니다."),
    ("캐시 전략", "Caching strategy",
     "정적 파일에는 1년짜리 Cache-Control 헤더를 붙이고, 파일 이름에 해시를 넣어 갱신합니다."),
    ("CDN과 지연 시간", "CDNs and latency",
     "서울에서 미국 서부까지 왕복 시간은 약 130밀리초이므로, 가까운 CDN 엣지 서버가 필요합니다."),
    ("폰트 로딩", "Loading web fonts",
     "font-display를 swap으로 두면 글꼴이 늦게 도착해도 글자가 먼저 보입니다."),
    ("애니메이션 성능", "Animation performance",
     "transform과 opacity만 움직이면 레이아웃을 다시 계산하지 않아서 부드럽습니다."),
    ("레이아웃 스래싱 피하기", "Avoiding layout thrashing",
     "읽기와 쓰기를 번갈아 하면 브라우저가 한 프레임에 레이아웃을 여러 번 계산합니다. 읽기를 먼저 모아서 처리하세요."),
    ("서버 응답 시간", "Time to First Byte",
     "TTFB가 800밀리초를 넘으면 나머지 최적화의 효과가 크게 줄어듭니다."),
    ("중급 휴대폰을 가정하기", "Assume a mid-range phone",
     "테스트할 때는 CPU를 4배 느리게 하고, 네트워크 속도도 함께 제한해 보세요."),
    ("성능 예산 세우기", "Setting a performance budget",
     "LCP 2초, 번들 200킬로바이트처럼 팀이 합의한 예산을 CI에서 자동으로 검사합니다."),
    ("회귀 막기", "Catching regressions",
     "배포할 때마다 지표를 기록하면 어느 커밋에서 느려졌는지 바로 찾을 수 있습니다."),
    ("사례: 3초에서 1.2초로", "Case study: from 3 s to 1.2 s",
     "한 쇼핑몰은 이미지와 폰트만 손봐서 LCP를 3초에서 1.2초로 줄였습니다."),
    ("정리와 다음 단계", "Summary and next steps",
     "오늘 내용 중 세 가지만 기억하세요. 먼저 측정하고, 예산을 세우고, 회귀를 막는 것입니다."),
]
H3_LONG_SILENCE = {4, 9, 13, 18, 22}   # 1-based slides followed by a long silent stretch


# ---------------------------------------------------------------- h1: DPR 2 staggered list

def h1_named_curve_checks():
    """Distance of H1_EASE from every named curve, and the two checks that it matches none of them."""
    names = named_curve_distances(H1_EASE)
    nearest = names[0]
    accepted = [r["name"] for r in names if r["max_abs_dy_11pt"] <= 0.05 or r.get("control_points_within_0.1")]
    return names, [
        {"check": "easing is not a named curve: smallest max|dy| (dense x grid) over "
                  f"{len(names)} named curves > 0.05", "measured": nearest["max_abs_dy_dense"],
         "nearest": nearest["name"], "expected": "> 0.05", "pass": nearest["max_abs_dy_dense"] > 0.05},
        {"check": "no named curve passes the scorer's easing test (11-pt max|dy| <= 0.05 or every control "
                  "point within 0.1)", "measured": accepted, "expected": [], "pass": not accepted}]


def h1_traps(frames, pts, events, names, skeleton_css, dpr):
    """h1 trap sentences; every timing and pixel number is read from the decoded file (frames, pts, observed_in_file)."""
    obs = [e["observed_in_file"] for e in events]
    lag_ms = [round((o["first_change_pts_s"] - e["start_s"]) * 1000) for o, e in zip(obs, events)]
    after_end_ms = [round((o["last_change_pts_s"] - e["end_s"]) * 1000) for o, e in zip(obs, events)]
    seen_ms = [round((o["last_change_pts_s"] - o["first_change_pts_s"]) * 1000) for o in obs]
    frame_ms = 1000 * (pts[1] - pts[0])
    late_rows = [i + 1 for i, lag in enumerate(lag_ms) if lag > frame_ms + 0.5]
    h, w = frames[0].shape[:2]
    busy = [k for k in range(1, len(frames)) if changed_pixels(frames[k], frames[k - 1], (0, 0, w, h)) >= MIN_PIXELS]
    runs = []
    for k in busy:
        if runs and k == runs[-1][1] + 1:
            runs[-1][1] = k
        else:
            runs.append([k, k])
    sk = [int(round(v * dpr)) for v in (skeleton_css["x"], skeleton_css["y"],
                                        skeleton_css["x"] + skeleton_css["w"], skeleton_css["y"] + skeleton_css["h"])]
    glint_max = max(int(cv2.absdiff(frames[k][sk[1]:sk[3], sk[0]:sk[2]], frames[k - 1][sk[1]:sk[3], sk[0]:sk[2]]).max())
                    for k in range(1, len(frames)))
    late = f"; rows {', '.join(map(str, late_rows))} miss the +-1 frame window" if late_rows else ""
    noise = " (frame 1 alone is I-to-P codec noise)" if runs and runs[0] == [1, 1] else ""
    return [
        "DPR 2: every distance measured in file pixels is twice the CSS value (48 file px of travel = translateX(-24px)).",
        "The stagger (45 ms) is shorter than the 30 fps frame interval (33.3 ms) and not a multiple of it: rows 2-6 "
        "start between frames (0.945, 0.990, 1.035, 1.080, 1.125 s). Because the easing starts slowly (slope 0.35 at "
        f"x = 0), the first changed frame comes {', '.join(map(str, lag_ms))} ms after the CSS start (rows 1-6), up to "
        f"{max(lag_ms) / frame_ms:.2f} frames late{late}. Fitting the curve recovers the start.",
        "Rows overlap heavily in time: one bounding box around the list merges six events into one 745 ms blob.",
        "The easing ends very softly (slope 0.02 at x = 1), but at DPR 2 the tail still moves text edges by fractions "
        "of a file pixel: every row keeps changing up to the last frame before end_s, and the detector's last change "
        f"is the settling frame {min(after_end_ms)}-{max(after_end_ms)} ms after end_s. Observed last - first change is "
        f"{min(seen_ms)}-{max(seen_ms)} ms: 520 ms minus the start lag plus that settling delay. Nearest named curves: "
        + "; ".join(f"{r['name']} (max |dy| {r['max_abs_dy_dense']})" for r in names[:3]) + "; none is within 0.05.",
        f"The shimmer is faint: consecutive frames differ by at most {glint_max} levels inside the skeleton, and while "
        "the glint enters or leaves it (around each loop restart at 0, 1.2, 2.4 s) fewer than 20 px pass the 8-level "
        "threshold. A whole-frame detector (8 levels, 20 px, consecutive frames) is busy at pts "
        + ", ".join(f"{pts[a]:.3f}" if a == b else f"{pts[a]:.3f}-{pts[b]:.3f}" for a, b in runs)
        + f" s{noise} and quiet in between (cv2 decode; these frames sit near the threshold, so another decoder moves "
          "the span edges by a frame or two): not one 3-second event, and its spans do not mark the rows.",
        "The background is a vertical gradient, so 'changed vs frame 0' masks differ in brightness by row.",
    ]


def build_h1():
    name, fps, n, dpr = NAMES["h1"], 30, 90, 2
    frames_dir, states, rects = render_animation(name, H1_HTML, 1280, 800, fps, n, rect_at_ms=2900, dpr=dpr)
    png = lambda k: cv2.imread(os.path.join(frames_dir, f"{k:05d}.png"))
    out = os.path.join(HERE, name + ".mp4")
    run(["ffmpeg", "-y", "-v", "error", "-framerate", str(fps), "-i", os.path.join(frames_dir, "%05d.png"),
         "-vf", BT709_VF] + X264 + ["-movflags", "+faststart", out])
    info, pts = ffprobe(out)
    frames = read_frames(out)
    ease = cubic_bezier(*H1_EASE)
    starts = [(H1_START_MS + H1_STAGGER_MS * i) / 1000 for i in range(6)]
    checks = [
        {"check": "source screenshot size (px) at deviceScaleFactor 2", "measured": list(png(0).shape[1::-1]),
         "expected": [2560, 1600], "pass": list(png(0).shape[1::-1]) == [2560, 1600]},
        {"check": "decoded frames: count and size", "measured": [len(frames), frames[0].shape[1], frames[0].shape[0]],
         "expected": [n, 2560, 1600], "pass": [len(frames), frames[0].shape[1], frames[0].shape[0]] == [n, 2560, 1600]},
        {"check": "CFR 30 fps: pts == n / 30 for every frame", "measured": round(max(abs(t - k / fps) for k, t in enumerate(pts)), 6),
         "expected": 0, "pass": max(abs(t - k / fps) for k, t in enumerate(pts)) < 1e-4},
    ]
    names, easing_checks = h1_named_curve_checks()
    checks += easing_checks

    def row_state(i, t):
        y = progress(t, starts[i], H1_DUR_MS, ease)
        return -H1_SHIFT * (1 - y), y
    for i in range(6):
        err_tx = max(abs(parse_matrix(s[f"row{i + 1}"]["transform"])[4] - row_state(i, k / fps)[0]) for k, s in enumerate(states))
        err_op = max(abs(s[f"row{i + 1}"]["opacity"] - row_state(i, k / fps)[1]) for k, s in enumerate(states))
        checks.append(check(f"row{i + 1} chrome translateX (CSS px) vs solver (max abs err, all frames)", err_tx, 0, 0.01, "px"))
        checks.append(check(f"row{i + 1} chrome opacity vs solver (max abs err, all frames)", err_op, 0, 0.001, "opacity"))
    k_all = 36
    moving = [i + 1 for i in range(6) if 0 < row_state(i, k_all / fps)[1] < 1]
    checks.append({"check": f"all six rows are mid-animation at frame {k_all} (t={k_all / fps:.4f}s)", "measured": moving,
                   "expected": [1, 2, 3, 4, 5, 6], "pass": moving == [1, 2, 3, 4, 5, 6]})
    glint_speed = (H1_SKEL_W + H1_GLINT_W) / (H1_GLINT_MS / 1000)

    def glint_tx(t):
        return -H1_GLINT_W + glint_speed * ((t * 1000) % H1_GLINT_MS) / 1000
    err_g = max(abs(parse_matrix(s["glint"]["transform"])[4] - glint_tx(k / fps)) for k, s in enumerate(states))
    checks.append(check("glint chrome translateX vs 1200 ms linear loop (max abs err, all frames)", err_g, 0, 0.01, "px"))

    # DPR mapping: the first row's icon edges in the final decoded frame sit at 2x its CSS rect
    ic = rects["icon1"]
    row_y = int(round((ic["y"] + ic["h"] / 2) * dpr))
    line = frames[-1][row_y].astype(float).mean(axis=1)
    inside, outside = line[int((ic["x"] + ic["w"] / 2) * dpr)], line[int((ic["x"] - 6) * dpr)]
    cover = np.clip((outside - line) / (outside - inside), 0, 1)

    def crossing(j0, j1, rising):
        for j in range(j0, j1):
            a, b = cover[j], cover[j + 1]
            if (a < 0.5 <= b) if rising else (a >= 0.5 > b):
                return j + 0.5 + (0.5 - a) / (b - a)
        return float("nan")
    left = crossing(int((ic["x"] - 4) * dpr), int((ic["x"] + 6) * dpr), True)
    right = crossing(int((ic["x"] + ic["w"] - 6) * dpr), int((ic["x"] + ic["w"] + 4) * dpr), False)
    checks.append(check("row1 icon left edge in final decoded frame (file px, sub-pixel) == 2 x CSS x", left,
                        ic["x"] * dpr, 0.6, "file px"))
    checks.append(check("row1 icon width in final decoded frame (file px) == 2 x 36 CSS px", right - left,
                        ic["w"] * dpr, 0.8, "file px"))

    # per-row sub-pixel translateX + opacity fits at three times each (file px, reported in CSS px)
    bg, final = frames[0], frames[-1]
    boxes = {}
    for i in range(6):
        r = rects[f"row{i + 1}"]
        boxes[i] = (int((r["x"] - H1_SHIFT - 3) * dpr), int((r["y"] - 2) * dpr),
                    int((r["x"] + r["w"] + 3) * dpr), int((r["y"] + r["h"] + 2) * dpr))
    for i in range(6):
        for frac in (0.3, 0.5, 0.75):
            k = int(round((starts[i] + frac * H1_DUR_MS / 1000) * fps))
            want_tx, want_op = row_state(i, k / fps)
            for label, fr, f0, fN in (("decoded mp4", frames[k], bg, final), ("source PNG", png(k), png(0), png(n - 1))):
                if label == "source PNG" and frac != 0.5:
                    continue
                v, a = fit_coarse_fine(fr, f0, fN, boxes[i], shift_x, -(H1_SHIFT + 2) * dpr, 0, 0.5, 0.01, margin=64)
                checks.append(check(f"row{i + 1} {label} frame {k} (t={k / fps:.4f}s) translateX by sub-pixel shift fit "
                                    "(luma, file px / 2)", v / dpr, want_tx, 0.2, "CSS px"))
                checks.append(check(f"row{i + 1} {label} frame {k} opacity from the same fit", a, want_op, 0.03, "opacity"))

    # shimmer glint centre from the brightness excess over the first skeleton bar
    sk = rects["skeleton"]
    bar_y = int(round((sk["y"] + 8) * dpr))
    xs0, xs1 = int(round(sk["x"] * dpr)), int(round((sk["x"] + H1_SKEL_W) * dpr))
    base = frames[0][bar_y, xs0:xs1].astype(float).mean(axis=1)   # t = 0: glint just left of the bar (tx = -120)
    for k in (15, 54, 87):
        e = np.clip(frames[k][bar_y, xs0:xs1].astype(float).mean(axis=1) - base, 0, None)
        centre = float((e * (np.arange(len(e)) + 0.5)).sum() / e.sum()) / dpr
        checks.append(check(f"glint centre in decoded frame {k} (t={k / fps:.4f}s), CSS px from the bar's left edge",
                            centre, glint_tx(k / fps) + H1_GLINT_W / 2, 1.0, "CSS px"))

    events = []
    for i, (title, desc, _, _) in enumerate(H1_ROWS):
        r = rects[f"row{i + 1}"]
        obs = observed_window(frames, pts, boxes[i])
        obs["box_units"] = "file px (2 x CSS px)"
        events.append({
            "id": f"row{i + 1}-enter",
            "element": f"settings row {i + 1} of 6 (\"{title.replace('&amp;', '&')}\" · {desc})",
            "selector": f".row:nth-child({i + 1})", "kind": "entrance",
            "start_s": round(starts[i], 3), "end_s": round(starts[i] + H1_DUR_MS / 1000, 3), "duration_ms": H1_DUR_MS,
            "delay_ms": H1_STAGGER_MS * i, "css_animation_delay_ms": H1_START_MS + H1_STAGGER_MS * i,
            "easing": easing_block(H1_EASE, "custom (not a named curve; slow start, fast middle, long soft tail)"),
            "properties": [{"property": "transform", "function": "translateX", "from": -H1_SHIFT, "to": 0, "unit": "CSS px",
                            "from_file_px": -H1_SHIFT * dpr},
                           {"property": "opacity", "from": 0, "to": 1}],
            "direction": "left to right (moves from 24 CSS px left of its resting place to 0)",
            "magnitude_px": H1_SHIFT, "magnitude_file_px": H1_SHIFT * dpr,
            "peak_speed_px_s": round(H1_SHIFT / (H1_DUR_MS / 1000) * peak_slope(H1_EASE), 1),
            "bbox_final": rect_px(r), "bbox_final_file_px": rect_px(r, dpr),
            "observed_in_file": obs,
        })
    glint_r = rects["glint"]
    truth = {
        "file": name + ".mp4", "kind": "ui-motion", "container": info,
        "geometry": {"css_viewport": [1280, 800], "device_scale_factor": dpr, "file_px_per_css_px": dpr,
                     "units": "translate, magnitude_px, bbox_final and peak_speed_px_s are CSS px; *_file_px and "
                              "observed_in_file.box_xyxy are file pixels (2 x CSS px)"},
        "summary": "Settings page recorded on a 2x (Retina) display: the file is 2560x1600 but the page is 1280x800 CSS px. "
                   "Six setting rows fade in while sliding 24 CSS px to the right (48 file px), 520 ms each, staggered 45 ms "
                   "from 0.900 s (rows 1-6 top to bottom, overlapping heavily), easing cubic-bezier(0.34, 0.12, 0.08, 0.98). "
                   "A skeleton shimmer loops in the 'Recent activity' panel the whole time.",
        "events": events,
        "groups": [{"type": "stagger", "members": [e["id"] for e in events], "stagger_ms": H1_STAGGER_MS,
                    "order": "top to bottom", "group_start_s": starts[0], "group_end_s": round(starts[5] + H1_DUR_MS / 1000, 3),
                    "total_span_ms": H1_STAGGER_MS * 5 + H1_DUR_MS,
                    "overlap": "every row starts before the previous one is 10% through its duration; all six move "
                               f"together from {starts[5]:.3f} s to {starts[0] + H1_DUR_MS / 1000:.3f} s"}],
        "css_source": ".row{animation:row-in 520ms cubic-bezier(0.34,0.12,0.08,0.98) both} "
                      + " ".join(f".row:nth-child({i + 1}){{animation-delay:{H1_START_MS + H1_STAGGER_MS * i}ms}}" for i in range(6))
                      + " @keyframes row-in{from{opacity:0;transform:translateX(-24px)}to{opacity:1;transform:translateX(0)}} "
                        ".glint{animation:shimmer 1200ms linear infinite} "
                        "@keyframes shimmer{from{transform:translateX(-120px)}to{transform:translateX(250px)}}",
        "expected_css_answer": {"duration": "520ms", "delays": [f"{H1_STAGGER_MS * i}ms" for i in range(6)],
                                "first_start": "900ms", "timing_function": "cubic-bezier(0.34, 0.12, 0.08, 0.98)",
                                "keyframes": "from {opacity: 0; transform: translateX(-24px)} to {opacity: 1; transform: none}"},
        "distractors": [{
            "id": "skeleton-shimmer", "element": "skeleton placeholder (three grey bars) in the 'Recent activity' panel, "
                                                 "a white glint sweeps across it",
            "selector": ".glint", "kind": "shimmer loop (not an entrance or transition)",
            "css": "animation: shimmer 1200ms linear infinite; translateX(-120px -> 250px) inside a 250 px wide clip",
            "period_ms": H1_GLINT_MS, "easing": "linear", "speed_px_s": round(glint_speed, 3), "direction": "left to right",
            "phase_at_t0": "glint just left of the bars (translateX -120 CSS px)", "runs_s": [0.0, round(n / fps, 3)],
            "bbox": rect_px(sk), "glint_bbox_at_rect_time": rect_px(glint_r),
            "must_not_report_as": "an intended entrance/exit/transition event; at most mention it as a looping shimmer",
        }],
        "traps": h1_traps(frames, pts, events, names, rects["skeleton"], dpr),
        "named_curve_check": {"curves_compared": len(names), "nearest": names[:5],
                              "method": "max |y_named(x) - y(x)| on 2001 x points and on the scorer's 11 points; beziers "
                                        "sampled parametrically; Penner functions exact; Material 3 'emphasized' as its "
                                        "two-segment path"},
        "verification": checks,
    }
    return truth


# ---------------------------------------------------------------- h2: colour-only change, overshoot, drawer, VFR

def build_h2():
    name, fps, n = NAMES["h2"], 60, 192
    frames_dir, states, rects = render_animation(name, H2_HTML, 1280, 800, fps, n, rect_at_ms=3150)
    log = run(["ffmpeg", "-v", "info", "-framerate", str(fps), "-i", os.path.join(frames_dir, "%05d.png"),
               "-vf", "mpdecimate,showinfo", "-f", "null", "-"]).stderr
    kept = sorted({int(round(float(m) * fps)) for m in re.findall(r"pts_time:([\d.]+)", log)})
    decimated = list(kept)
    if n - 1 not in kept:          # keep the final source frame so the file lasts 3.2 s
        kept.append(n - 1)
    out = os.path.join(HERE, name + ".mp4")
    select = "select='" + "+".join(f"eq(n,{k})" for k in kept) + "'"
    # -bf 0: with B-frames the last packet (pts 3.183) is decoded at dts 2.2, and ffprobe reports the dts span
    # (2.85 s) as the duration although mvhd/elst say 3.2 s. Without reordering dts == pts and both read 3.2 s.
    run(["ffmpeg", "-y", "-v", "error", "-framerate", str(fps), "-i", os.path.join(frames_dir, "%05d.png"),
         "-vf", f"{select},{BT709_VF}", "-fps_mode", "vfr"] + X264 + ["-bf", "0", "-movflags", "+faststart", out])
    info, pts = ffprobe(out)
    frames = read_frames(out)
    rgb = read_rgb_bt709(out, 1280, 800)
    ys = read_y_plane(out, 1280, 800)
    src_of = [int(round(t * fps)) for t in pts]
    checks = [{"check": "kept pts == mpdecimate decisions (+ final frame)", "measured": src_of, "expected": kept,
               "pass": src_of == kept},
              {"check": "decoded frame counts (cv2, ffmpeg rgb24, Y plane) == ffprobe pts count",
               "measured": [len(frames), len(rgb), len(ys)], "expected": [len(pts)] * 3,
               "pass": len(frames) == len(rgb) == len(ys) == len(pts)},
              check("container duration (ffprobe format) == source frames / fps (last kept pts + one source frame)",
                    info["duration_s"], n / fps, 1e-3, "s")]
    e_lin, e_badge, e_drawer = cubic_bezier(*H2_LINEAR), cubic_bezier(*H2_BADGE_EASE), cubic_bezier(*H2_DRAWER_EASE)

    def css_state(t):
        x = progress(t, 0.6, 200, e_lin)
        return {"rec_rgb": tuple(a + (b - a) * x for a, b in zip(H2_BLUE, H2_RED)),
                "badge_scale": progress(t, 1.0, 350, e_badge),
                "drawer_ty": H2_DRAWER_H * (1 - progress(t, 1.8, 450, e_drawer))}
    # luma equality of the two colours
    l7 = (luma709(H2_BLUE), luma709(H2_RED))
    l6 = (luma601(H2_BLUE), luma601(H2_RED))
    checks.append(check("BT.709 luma: blue vs red (full-range 8-bit levels)", abs(l7[0] - l7[1]), 0, 2.0, "levels"))
    checks.append(check("BT.601 luma: blue vs red (full-range 8-bit levels)", abs(l6[0] - l6[1]), 0, 2.0, "levels"))
    err_c = max(max(abs(a - b) for a, b in zip(parse_rgb(s["rec"]["bg"]), css_state(k / fps)["rec_rgb"]))
                for k, s in enumerate(states))
    err_b = max(abs(parse_matrix(s["badge"]["transform"])[0] - css_state(k / fps)["badge_scale"]) for k, s in enumerate(states))
    err_d = max(abs(parse_matrix(s["drawer"]["transform"])[5] - css_state(k / fps)["drawer_ty"]) for k, s in enumerate(states))
    checks.append(check("chrome background-color vs linear sRGB interpolation (max channel err, all 192 source frames; "
                        "Chrome reports integer channels, so up to 0.5 is rounding)", err_c, 0, 0.501, "levels"))
    checks.append(check("chrome badge scale vs solver (max abs err, all source frames)", err_b, 0, 1e-4, "scale"))
    checks.append(check("chrome drawer translateY vs solver (max abs err, all source frames)", err_d, 0, 0.01, "px"))
    t_dense = np.linspace(0, 1, 200001)
    yb = 3 * 1.56 * (1 - t_dense) ** 2 * t_dense + 3 * (1 - t_dense) * t_dense ** 2 + t_dense ** 3
    xb = 3 * 0.34 * (1 - t_dense) ** 2 * t_dense + 3 * 0.64 * (1 - t_dense) * t_dense ** 2 + t_dense ** 3
    j = int(np.argmax(yb))
    peak_scale, peak_x = float(yb[j]), float(xb[j])
    chrome_peak = max(parse_matrix(s["badge"]["transform"])[0] for s in states)
    checks.append(check("chrome badge max scale over source frames vs analytic peak", chrome_peak, peak_scale, 0.002, "scale"))

    # button colour, pixel check (ffmpeg rgb24 decode with the BT.709 matrix), interior left of the label
    r = rects["rec"]
    ix0, iy0, ix1, iy1 = int(r["x"]) + 10, int(r["y"]) + 12, int(r["x"]) + 42, int(r["y"] + r["h"]) - 12
    idx = lambda target: min(range(len(pts)), key=lambda q: abs(pts[q] - target))
    for target in (0.65, 0.70, 0.75, 1.5):
        q = idx(target)
        got = rgb[q][iy0:iy1, ix0:ix1].reshape(-1, 3).astype(float).mean(axis=0)
        want = css_state(pts[q])["rec_rgb"]
        err = float(np.max(np.abs(got - np.array(want))))
        checks.append({"check": f"kept frame pts={pts[q]:.4f}s button interior RGB (bt709 decode) vs CSS colour",
                       "measured": [round(v, 1) for v in got], "expected": [round(v, 1) for v in want],
                       "tolerance": 3.0, "unit": "levels per channel", "pass": err <= 3.0})
    bbox_btn = (int(r["x"]) - 4, int(r["y"]) - 4, int(math.ceil(r["x"] + r["w"])) + 4, int(math.ceil(r["y"] + r["h"])) + 4)
    x0, y0, x1, y1 = bbox_btn
    y_int = [float(y[iy0:iy1, ix0:ix1].mean()) for y in ys]
    cv_gray = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    rgb601 = [(fr[..., 0] * 0.299 + fr[..., 1] * 0.587 + fr[..., 2] * 0.114) for fr in rgb]
    rgb709 = [(fr[..., 0] * 0.2126 + fr[..., 1] * 0.7152 + fr[..., 2] * 0.0722) for fr in rgb]

    def box_range(planes):
        """largest per-pixel |change vs frame 0| inside the whole button box (text and edges included)"""
        ref = planes[0][y0:y1, x0:x1].astype(float)
        return max(float(np.abs(p[y0:y1, x0:x1].astype(float) - ref).max()) for p in planes)

    def box_count(planes):
        ref = planes[0][y0:y1, x0:x1].astype(float)
        return max(int((np.abs(p[y0:y1, x0:x1].astype(float) - ref) > PIX_THRESHOLD).sum()) for p in planes)
    luma_stats = {
        "file_Y_plane_interior_mean_range_levels": round(max(y_int) - min(y_int), 3),
        "file_Y_plane_max_px_change_vs_frame0": box_range(ys),
        "cv2_decode_BGR2GRAY_max_px_change_vs_frame0": box_range(cv_gray),
        "bt709_rgb_then_bt601_grey_max_px_change_vs_frame0": round(box_range(rgb601), 2),
        "bt709_rgb_then_bt709_grey_max_px_change_vs_frame0": round(box_range(rgb709), 2),
        "px_over_8_levels_any_frame": {"file_Y": box_count(ys), "cv2_gray": box_count(cv_gray),
                                       "rgb_bt601_grey": box_count(rgb601), "rgb_bt709_grey": box_count(rgb709)},
        "max_channel_rgb_px_change_vs_frame0": max(
            float(np.abs(fr[y0:y1, x0:x1].astype(float) - rgb[0][y0:y1, x0:x1].astype(float)).max()) for fr in rgb),
    }
    checks.append(check("file Y plane: button-interior mean luma range over all frames", luma_stats[
        "file_Y_plane_interior_mean_range_levels"], 0, 2.0, "levels"))
    for path_key, label in (("file_Y", "file Y plane"), ("cv2_gray", "cv2 decode + BGR2GRAY"),
                            ("rgb_bt601_grey", "bt709 rgb decode + BT.601 grey (PIL 'L', ffmpeg->png->cv2 gray)"),
                            ("rgb_bt709_grey", "bt709 rgb decode + BT.709 grey")):
        count = luma_stats["px_over_8_levels_any_frame"][path_key]
        checks.append({"check": f"{label}: pixels in the button box that differ from frame 0 by > {PIX_THRESHOLD} levels "
                                f"(worst frame) stay below the {MIN_PIXELS} px detector floor",
                       "measured": count, "expected": f"< {MIN_PIXELS}", "pass": count < MIN_PIXELS})
    obs_col = observed_window(frames, pts, bbox_btn)
    obs_luma = observed_window(ys, pts, bbox_btn)
    obs_cvg = observed_window(cv_gray, pts, bbox_btn)
    checks.append({"check": "colour change is visible to a max-channel BGR diff but to no luma/grey diff "
                            "(consecutive frames, > 8 levels, >= 20 px)",
                   "measured": {"bgr_max_channel": bool(obs_col), "file_Y": bool(obs_luma), "cv2_gray": bool(obs_cvg)},
                   "expected": {"bgr_max_channel": True, "file_Y": False, "cv2_gray": False},
                   "pass": bool(obs_col) and not obs_luma and not obs_cvg})

    # badge scale about its centre, least-squares fit on luma against the final frame (scale 1)
    br = rects["badge"]
    bcx, bcy = br["x"] + br["w"] / 2, br["y"] + br["h"] / 2
    badge_box = (int(br["x"]) - 6, max(int(br["y"]) - 6, 0), int(math.ceil(br["x"] + br["w"])) + 6,
                 int(math.ceil(br["y"] + br["h"])) + 6)
    fits = []
    badge_kept = [q for q in range(len(pts)) if 1.0 < pts[q] < 1.36]
    for q in badge_kept:
        s_m, a = fit_coarse_fine(frames[q], frames[0], frames[-1], badge_box, scale_about(bcx, bcy), 0.05, 1.3, 0.01,
                                 0.0005, margin=24)
        fits.append((q, s_m, a))
    for target in (1.06, 1.13, 1.20, 1.28):
        q = idx(target)
        s_m, a = next((s, a) for qq, s, a in fits if qq == q)
        checks.append(check(f"kept frame pts={pts[q]:.4f}s badge scale by least-squares fit about its centre (luma)",
                            s_m, css_state(pts[q])["badge_scale"], 0.03, "scale"))
    q_peak, s_peak, _ = max(fits, key=lambda f: f[1])
    checks.append(check(f"badge overshoot: largest fitted scale (kept frame pts={pts[q_peak]:.4f}s) vs CSS at that pts",
                        s_peak, css_state(pts[q_peak])["badge_scale"], 0.03, "scale"))
    checks.append({"check": "badge overshoot is visible in the file: largest fitted scale > 1.05",
                   "measured": round(s_peak, 4), "expected": "> 1.05", "pass": s_peak > 1.05})

    # drawer translateY fit (content shifted down by v, clipped at the bottom edge)
    dr = rects["drawer"]
    drawer_box = (int(dr["x"]) + 2, int(dr["y"]) - 20, int(dr["x"] + dr["w"]) - 2, 800)
    for target in (1.83, 1.90, 2.00, 2.12):
        q = idx(target)
        v, a = fit_coarse_fine(frames[q], frames[0], frames[-1], drawer_box, shift_y, 0, H2_DRAWER_H, 1.0, 0.02, margin=8)
        checks.append(check(f"kept frame pts={pts[q]:.4f}s drawer translateY by shift fit (luma)", v,
                            css_state(pts[q])["drawer_ty"], 0.5, "px"))

    obs_badge = observed_window(frames, pts, badge_box)
    obs_drawer = observed_window(frames, pts, drawer_box)
    control = {"badge": bool(observed_window(ys, pts, badge_box)), "drawer": bool(observed_window(ys, pts, drawer_box)),
               "badge_cv2_gray": bool(observed_window(cv_gray, pts, badge_box))}
    checks.append({"check": "positive control: the same luma-only detectors DO see the badge and the drawer",
                   "measured": control, "expected": {"badge": True, "drawer": True, "badge_cv2_gray": True},
                   "pass": all(control.values())})
    kept_rows = [{"pts_s": round(t, 6), "source_frame": k,
                  "rec_rgb": [round(v, 2) for v in css_state(k / fps)["rec_rgb"]],
                  "badge_scale": round(css_state(k / fps)["badge_scale"], 5),
                  "drawer_translateY_px": round(css_state(k / fps)["drawer_ty"], 3)} for t, k in zip(pts, kept)]

    def changed(k):
        return css_state(k / fps) != css_state((k - 1) / fps)
    moving_dropped = [k for k in range(1, n) if changed(k) and k not in kept]
    gaps = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1) if pts[i + 1] - pts[i] > 1.5 / fps]
    first_visible_drawer = obs_drawer["first_change_pts_s"]
    drawer_tail = ""
    if obs_drawer["last_change_frame"] == len(pts) - 1:   # the forced final frame, not the end of the travel
        dx0, dy0, dx1, dy1 = drawer_box
        d_end = cv2.absdiff(frames[-1][dy0:dy1, dx0:dx1], frames[-2][dy0:dy1, dx0:dx1]).max(axis=2)
        travel_end = observed_window(frames, pts, drawer_box, t_to=pts[-2])["last_change_pts_s"]
        drawer_tail = (f"The last change is the final frame (pts {pts[-1]:.4f} s): {int((d_end > PIX_THRESHOLD).sum())} px, "
                       f"max {int(d_end.max())} levels, where Chrome re-rasterises the settled drawer (translateY "
                       f"{css_state(pts[-2])['drawer_ty']:.3f} px at pts {pts[-2]:.4f} s, then 0). That is not motion: "
                       f"the visible travel ends at pts {travel_end} s.")
        obs_drawer["note"] = drawer_tail
    obs_col["luma"] = luma_stats
    obs_col["luma_only_detectors"] = {"file_Y_plane": obs_luma, "cv2_BGR2GRAY": obs_cvg}
    obs_col["note"] = "Found by a per-channel (max of B, G, R) diff. The file's luma plane never changes by more " \
                      "than the codec noise listed in 'luma'."
    badge_dropped = [k for k in moving_dropped if 1.0 < k / fps < 1.35]
    badge_step_px = max(abs(css_state(k / fps)["badge_scale"] - css_state((k - 1) / fps)["badge_scale"])
                        for k in badge_dropped) * (br["w"] / 2 + 2)
    best_kept = max((k for k in kept if 1.0 < k / fps < 1.35), key=lambda k: css_state(k / fps)["badge_scale"])
    peak_in_file = (best_kept / fps, css_state(best_kept / fps)["badge_scale"])
    obs_badge["note"] = (f"Around the peak and in the tail the badge edge (radius 13 px + 2 px ring) moves at most "
                         f"{badge_step_px:.2f} px per source frame, so mpdecimate dropped source frames {badge_dropped}. "
                         f"The analytic peak ({peak_scale:.4f} at {1.0 + 0.35 * peak_x:.4f} s) is NOT in the file; the "
                         f"largest scale present is {peak_in_file[1]:.4f} at pts {peak_in_file[0]:.4f} s.")
    events = [
        {"id": "record-colour", "element": "primary 'Record' button, right of the preview card",
         "selector": ".rec", "kind": "transition", "start_s": 0.6, "end_s": 0.8, "duration_ms": 200, "delay_ms": 0,
         "delay_note": "independent event; no stagger group", "css_animation_delay_ms": 600,
         "easing": easing_block(H2_LINEAR, "linear"),
         "properties": [{"property": "background-color", "from": hexc(H2_BLUE), "to": hexc(H2_RED),
                         "from_rgb": list(H2_BLUE), "to_rgb": list(H2_RED),
                         "interpolation": "sRGB channel-wise (CSS legacy colour)"}],
         "direction": "no movement: colour only (blue to dark red); label, size and position unchanged",
         "luma": {"bt709_from": round(l7[0], 3), "bt709_to": round(l7[1], 3),
                  "bt601_from": round(l6[0], 3), "bt601_to": round(l6[1], 3)},
         "bbox_final": rect_px(r), "observed_in_file": obs_col},
        {"id": "badge-pop", "element": "red notification badge '3' on the bell icon, top-right of the header",
         "selector": ".badge", "kind": "entrance", "start_s": 1.0, "end_s": 1.35, "duration_ms": 350, "delay_ms": 0,
         "delay_note": "independent event; no stagger group", "css_animation_delay_ms": 1000,
         "easing": easing_block(H2_BADGE_EASE, "ease-out-back (overshoot; easings.net easeOutBack)"),
         "properties": [{"property": "transform", "function": "scale", "from": 0, "to": 1, "origin": "50% 50%"}],
         "direction": "grows from its centre, overshoots past full size, settles back",
         "overshoot": {"peak_scale": round(peak_scale, 4), "peak_at_x": round(peak_x, 4),
                       "peak_at_s": round(1.0 + 0.35 * peak_x, 4), "overshoot_percent": round((peak_scale - 1) * 100, 2)},
         "magnitude": f"scale 0 -> 1 (26 px circle + 2 px white ring), peak {peak_scale:.4f} at {1.0 + 0.35 * peak_x:.4f} s",
         "bbox_final": rect_px(br), "observed_in_file": obs_badge},
        {"id": "drawer-enter", "element": "bottom drawer 'Recording saved' (760 x 300 px, rounded top corners)",
         "selector": ".drawer", "kind": "entrance", "start_s": 1.8, "end_s": 2.25, "duration_ms": 450, "delay_ms": 0,
         "delay_note": "independent event; no stagger group", "css_animation_delay_ms": 1800,
         "easing": easing_block(H2_DRAWER_EASE, "iOS-style spring-ish ease-out"),
         "properties": [{"property": "transform", "function": "translateY", "from": "100%", "to": 0,
                         "from_px": H2_DRAWER_H, "unit": "px"}],
         "direction": "up (slides in from fully below the bottom edge)", "magnitude_px": H2_DRAWER_H,
         "fully_offscreen_before_start": True, "opacity_animated": False,
         "peak_speed_px_s": round(H2_DRAWER_H / 0.45 * peak_slope(H2_DRAWER_EASE), 1),
         "bbox_final": rect_px(dr), "observed_in_file": obs_drawer},
    ]
    truth = {
        "file": name + ".mp4", "kind": "ui-motion", "container": info,
        "summary": "Variable-frame-rate recording of a screen-recorder app. At 0.6 s the 'Record' button's background "
                   f"turns from blue {hexc(H2_BLUE)} to dark red {hexc(H2_RED)} (200 ms, linear) with no change in "
                   "brightness. At 1.0 s a notification badge pops in (scale 0 -> 1, 350 ms, overshooting to "
                   f"{peak_scale:.3f}). At 1.8 s a bottom drawer slides up from below the screen edge (450 ms).",
        "events": events, "groups": [],
        "vfr": {"source_fps": fps, "source_frames": n,
                "method": "ffmpeg mpdecimate (defaults hi=64*12, lo=64*5, frac=0.33) on the 60 fps source PNGs, "
                          "-fps_mode vfr; the final source frame is also kept so the file ends at 3.2 s",
                "mpdecimate_kept_source_frames": decimated, "kept_frames": kept_rows,
                "moving_source_frames_dropped": moving_dropped,
                "note": "Each kept frame shows the page state at its own pts. Frames absent from the file were "
                        "identical or nearly identical to the previous KEPT frame (mpdecimate compares with the last "
                        "kept frame, so slow changes are kept in steps)."},
        "css_source": ".rec{animation:rec-colour 200ms linear 600ms both} "
                      f"@keyframes rec-colour{{from{{background-color:{hexc(H2_BLUE)}}}to{{background-color:{hexc(H2_RED)}}}}} "
                      ".badge{transform-origin:50% 50%;animation:badge-pop 350ms cubic-bezier(0.34,1.56,0.64,1) 1000ms both} "
                      "@keyframes badge-pop{from{transform:scale(0)}to{transform:scale(1)}} "
                      ".drawer{animation:drawer-in 450ms cubic-bezier(0.32,0.72,0,1) 1800ms both} "
                      "@keyframes drawer-in{from{transform:translateY(100%)}to{transform:translateY(0)}}",
        "expected_css_answer": {"record-colour": f"background-color {hexc(H2_BLUE)} -> {hexc(H2_RED)}, 200ms linear, starts 600ms",
                                "badge-pop": "transform scale(0) -> scale(1), 350ms cubic-bezier(0.34, 1.56, 0.64, 1), "
                                             "starts 1000ms, origin centre",
                                "drawer-enter": "transform translateY(100%) -> translateY(0), 450ms "
                                                "cubic-bezier(0.32, 0.72, 0, 1), starts 1800ms"},
        "distractors": [],
        "traps": [
            "The button colour change keeps luma constant (BT.709 and BT.601 within 0.4 levels): a greyscale frame "
            "diff (cv2 BGR2GRAY, PIL 'L', ffmpeg Y plane) sees nothing; a per-channel or chroma diff sees a large change.",
            f"The badge overshoots: peak scale {peak_scale:.4f} at {1.0 + 0.35 * peak_x:.4f} s. A monotone ease-out fitted to "
            "its size misses the overshoot; y > 1 needs p1y or p2y > 1.",
            f"The overshoot peak itself fell into a VFR hole (source frames {badge_dropped} dropped): the largest badge "
            f"scale present in the file is {peak_in_file[1]:.4f} at {peak_in_file[0]:.4f} s, so the peak height and time "
            "must be inferred from the curve, not read off a frame.",
            f"The drawer starts fully below the viewport: nothing is visible at 1.8 s; its first visible frame is "
            f"pts {first_visible_drawer} s. " + drawer_tail,
            "Frame gaps of " + ", ".join(f"{b - a:.3f} s ({a:.3f} -> {b:.3f})" for a, b in gaps) +
            " are VFR holes where the page did not change (or changed too little for mpdecimate), not pauses.",
            ("Every source frame whose CSS state changed is in the file" if not moving_dropped else
             f"{len(moving_dropped)} source frames whose CSS state changed were dropped by mpdecimate "
             f"(source frames {moving_dropped}); the kept frames around them show the change in larger steps") + ".",
            "Three unrelated events: reporting them as one stagger group, or giving them delay_ms relative to each other, "
            "is wrong (each has delay_ms 0).",
        ],
        "verification": checks,
    }
    return truth


# ---------------------------------------------------------------- h3: 10-minute Korean lecture

OCR_SWIFT = r"""import Foundation
import Vision
import AppKit
for path in CommandLine.arguments.dropFirst() {
  print("### " + path)
  let url = URL(fileURLWithPath: path)
  guard let img = NSImage(contentsOf: url), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { print("!! load"); continue }
  let req = VNRecognizeTextRequest()
  req.recognitionLevel = .accurate
  req.recognitionLanguages = ["ko-KR", "en-US"]
  req.usesLanguageCorrection = false
  try VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
  for o in (req.results ?? []) { if let t = o.topCandidates(1).first { print(t.string) } }
}
"""


def ocr_check(vdir, pngs, wants):
    """Optional: read decoded mid-slide frames back with macOS Vision OCR (Korean + English)."""
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return None
    src, binary = os.path.join(vdir, "ocr.swift"), os.path.join(vdir, "ocr_bin")
    with open(src, "w") as f:
        f.write(OCR_SWIFT)
    if subprocess.run([swiftc, "-O", src, "-o", binary], capture_output=True).returncode != 0:
        return None
    text = subprocess.run([binary] + pngs, capture_output=True, text=True, timeout=900).stdout
    blocks = {}
    for chunk in text.split("### ")[1:]:
        head, *lines = chunk.splitlines()
        blocks[head.strip()] = [ln.strip() for ln in lines if ln.strip()]
    norm = lambda s: re.sub(r"\s+", "", s)
    out = []
    for png, want in zip(pngs, wants):
        lines = blocks.get(png, [])
        found = [any(norm(w) == norm(ln) for ln in lines) for w in want]
        out.append({"check": f"Vision OCR on {os.path.basename(png)} finds every slide string exactly (whitespace-insensitive)",
                    "measured": lines, "expected": want, "pass": all(found)})
    return out


def build_h3():
    name, fps, rate, n = NAMES["h3"], 30, 48000, 18000
    pages = [SLIDE_HTML % (BASE_CSS, SLIDE_COLORS[i % len(SLIDE_COLORS)], t, e, f"{i + 1} / 24")
             for i, (t, e, _) in enumerate(LECTURE)]
    vdir, pngs = render_stills(name, pages, 1280, 720)
    clips = []
    for i, (_, _, say_text) in enumerate(LECTURE):
        raw = os.path.join(vdir, f"narration{i + 1:02d}.wav")
        run(["say", "-v", "Yuna", "-o", raw, "--file-format=WAVE", "--data-format=LEI16@48000", say_text])
        with wave.open(raw) as w:
            x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float64) / 32768
        above = np.where(np.abs(x) > 0.01)[0]
        clips.append(x[above[0]:above[-1] + 1])          # trim say's leading/trailing silence
    speech = [len(c) / rate for c in clips]
    rng = np.random.default_rng(20260927)
    offsets = [round(float(rng.uniform(0.3, 1.0)), 2) for _ in LECTURE]
    tails = [float(rng.uniform(38, 62)) if i + 1 in H3_LONG_SILENCE else float(rng.uniform(2.5, 16)) for i in range(24)]
    scale = (n / fps - sum(offsets) - sum(speech)) / sum(tails)
    tails = [t * scale for t in tails]
    cut_frames, t = [0], 0.0
    for i in range(23):
        t += offsets[i] + speech[i] + tails[i]
        cut_frames.append(int(round(t * fps)))
    bounds_f = cut_frames + [n]
    for i in range(24):
        room = (bounds_f[i + 1] - bounds_f[i]) / fps - offsets[i] - speech[i]
        if room < 1.0:
            raise RuntimeError(f"slide {i + 1}: only {room:.2f} s after its narration")
    frames_dir = os.path.join(vdir, "frames")
    os.makedirs(frames_dir)
    for k in range(n):
        slide = sum(1 for c in cut_frames if k >= c) - 1
        os.link(pngs[slide], os.path.join(frames_dir, f"{k:05d}.png"))
    track = np.zeros(int(n / fps * rate))
    placed = []
    for i, c in enumerate(clips):
        at = int(round((cut_frames[i] / fps + offsets[i]) * rate))
        track[at:at + len(c)] += c
        placed.append((at / rate, (at + len(c)) / rate))
    wav = os.path.join(vdir, "narration.wav")
    write_wav(wav, track, rate)
    out = os.path.join(HERE, name + ".mp4")
    x264 = [a if a != "14" else "16" for a in X264]       # crf 16: the slides are static, text stays crisp
    run(["ffmpeg", "-y", "-v", "error", "-framerate", str(fps), "-i", os.path.join(frames_dir, "%05d.png"), "-i", wav,
         "-vf", BT709_VF] + x264 + ["-tune", "stillimage", "-g", "600", "-c:a", "aac", "-b:a", "64k", "-ac", "1",
                                     "-ar", str(rate), "-movflags", "+faststart", out])
    info, pts = ffprobe(out)
    checks = [{"check": "frame count, CFR 30 fps, duration", "measured": [info["video"]["nb_frames"],
               info["video"]["r_frame_rate"], info["video"]["avg_frame_rate"], info["duration_s"]],
               "expected": [n, "30/1", "30/1", 600.0],
               "pass": info["video"]["nb_frames"] == n and info["video"]["avg_frame_rate"] == "30/1"
               and abs(info["duration_s"] - 600.0) < 0.05},
              {"check": "audio stream AAC, 600 s", "measured": [info["audio"]["codec"], info["audio"]["duration_s"]],
               "expected": ["aac", 600.0], "pass": info["audio"]["codec"] == "aac" and abs(info["audio"]["duration_s"] - 600) < 0.05},
              {"check": "file size (bytes) stays small", "measured": info["size_bytes"], "expected": "< 12 MB",
               "pass": info["size_bytes"] < 12_000_000}]
    cap = cv2.VideoCapture(out)
    prev, diffs, noisy, k = None, [], [], 0
    mid_frames = [(bounds_f[i] + bounds_f[i + 1]) // 2 for i in range(24)]
    mids = {}
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if prev is not None:
            diffs.append(float(cv2.absdiff(fr, prev).mean()))
            if k not in cut_frames:
                g = cv2.absdiff(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY))
                changed = int((g > PIX_THRESHOLD).sum())
                if changed >= MIN_PIXELS:
                    noisy.append((k, changed, int(g.max())))
        if k in mid_frames:
            mids[k] = fr
        prev, k = fr, k + 1
    cuts_seen = [i + 1 for i, d in enumerate(diffs) if d > 1.0]
    checks.append({"check": "hard cuts found by consecutive-frame mean abs diff > 1.0 (all 18000 decoded frames)",
                   "measured": cuts_seen, "expected": cut_frames[1:], "pass": cuts_seen == cut_frames[1:],
                   "min_diff_at_cuts": round(min(diffs[c - 1] for c in cut_frames[1:]), 4),
                   "max_diff_outside_cuts": round(max(d for i, d in enumerate(diffs) if i + 1 not in cut_frames), 4)})
    x = decode_audio(out, rate)
    active, win = speech_spans(x, rate)
    bounds = [c / fps for c in bounds_f]
    narration = []
    for i, (_, _, say_text) in enumerate(LECTURE):
        a = active[(active >= bounds[i]) & (active < bounds[i + 1])]
        start, end = float(a[0]), float(a[-1] + win)
        narration.append({"slide": i + 1, "text": say_text, "language": "ko", "voice": "macOS say -v Yuna",
                          "start_s": round(start, 3), "end_s": round(end, 3), "duration_s": round(end - start, 3),
                          "placed_start_s": round(placed[i][0], 3), "placed_end_s": round(placed[i][1], 3),
                          "offset_after_cut_s": offsets[i],
                          "silence_after_s": round(bounds[i + 1] - end, 3)})
        checks.append(check(f"slide {i + 1} narration start (decoded, 10 ms RMS > -45 dBFS) vs placement",
                            start, placed[i][0], 0.02, "s"))
        checks.append(check(f"slide {i + 1} narration end (decoded) vs placement", end, placed[i][1], 0.03, "s"))
    offs = [nr["start_s"] - bounds[i] for i, nr in enumerate(narration)]
    checks.append({"check": "every narration starts 0.3-1.0 s after its cut (measured)", "measured": [round(o, 3) for o in offs],
                   "expected": "[0.29, 1.01]", "pass": all(0.29 <= o <= 1.01 for o in offs)})
    sil = run(["ffmpeg", "-v", "info", "-i", out, "-map", "0:a", "-af", "silencedetect=noise=-45dB:d=0.4",
               "-f", "null", "-"]).stderr
    silence = [(float(a), float(b)) for a, b in zip(re.findall(r"silence_start: ([\d.]+)", sil),
                                                   re.findall(r"silence_end: ([\d.]+)", sil))]
    dec_pngs = []
    for i, k in enumerate(mid_frames):
        p = os.path.join(vdir, f"decoded_mid_{i + 1:02d}.png")
        cv2.imwrite(p, mids[k])
        dec_pngs.append(p)
    ocr = ocr_check(vdir, dec_pngs, [[t, e, f"{i + 1} / 24"] for i, (t, e, _) in enumerate(LECTURE)])
    if ocr:
        checks.extend(ocr)
    scenes = [{"index": i + 1, "start_s": round(bounds[i], 3), "end_s": round(bounds[i + 1], 3),
               "start_frame": bounds_f[i], "end_frame_exclusive": bounds_f[i + 1],
               "accent_bar_color": SLIDE_COLORS[i % len(SLIDE_COLORS)]} for i in range(24)]
    longest = sorted(narration, key=lambda nr: -nr["silence_after_s"])[:5]
    truth = {
        "file": name + ".mp4", "kind": "lecture (Korean)", "container": info,
        "summary": "Ten-minute Korean lecture on web performance: 24 static slides (Korean title, one English line, "
                   "footer 'n / 24'), hard cuts at irregular times, Korean narration (macOS voice Yuna) of 1-2 sentences "
                   "per slide starting 0.3-1.0 s after each cut, with long silences on several slides.",
        "scenes": scenes, "cuts_s": [round(b, 3) for b in bounds[1:24]], "cut_frames": cut_frames[1:],
        "transition": "hard cut (no fade)",
        "text": [{"slide": i + 1, "title_ko": t, "line_en": e, "footer": f"{i + 1} / 24",
                  "visible_s": [scenes[i]["start_s"], scenes[i]["end_s"]]} for i, (t, e, _) in enumerate(LECTURE)],
        "narration": narration,
        "audio": {"sample_rate": rate, "channels": 1, "codec": "aac 64 kb/s mono",
                  "speech_detector": "10 ms RMS windows above -45 dBFS on the decoded mp4 audio; start = first active "
                                     "window in the slide, end = end of the last active window in the slide",
                  "ffmpeg_silencedetect_-45dB_0.4s": [[round(a, 3), round(b, 3)] for a, b in silence]},
        "events": [], "distractors": [],
        "traps": [
            "Cut times are irregular and not on whole seconds; sampling one frame per second (or per N seconds) "
            "places a cut only to within that interval (scoring needs +-1 frame = 33 ms).",
            "Slide durations range from " + f"{min(b - a for a, b in zip(bounds, bounds[1:])):.1f} s to "
            f"{max(b - a for a, b in zip(bounds, bounds[1:])):.1f} s; the longest silences follow slides "
            + ", ".join(f"{nr['slide']} ({nr['silence_after_s']:.1f} s)" for nr in longest) +
            ". A transcript aligned by 'one sentence per N seconds' drifts.",
            "Each narration is a different text from its slide title; numbers (16.7, 2.5, 0.1, 300 ...) and English "
            "terms (LCP, INP, CLS, Lighthouse, Cache-Control, TTFB ...) are written in the script as digits/Latin; ASR "
            "may spell them in Hangul, which costs character error rate against this script.",
            "Several narrations contain two sentences with a pause between them: they are one entry per slide.",
            "No motion between cuts: slides are fully static, so no animation events should be reported.",
            f"x264 noise after I-frames changes a few title-glyph pixels between identical source frames in "
            f"{len(noisy)} frames (largest: " + ", ".join(f"frame {kk} ({c} px, max {m})" for kk, c, m in
                                                           sorted(noisy, key=lambda z: -z[1])[:3]) +
            f"). A detector at {PIX_THRESHOLD} levels / {MIN_PIXELS} px sees these as tiny events; they are codec noise.",
        ],
        "verification": checks,
    }
    return truth


# ---------------------------------------------------------------- assembly

def main():
    os.makedirs(WORK, exist_ok=True)
    wanted = sys.argv[1:] or list(NAMES)
    builders = {"h1": build_h1, "h2": build_h2, "h3": build_h3}
    for key in wanted:
        truth = builders[key]()
        with open(os.path.join(WORK, NAMES[key] + ".truth.json"), "w", encoding="utf-8") as f:
            json.dump(truth, f, ensure_ascii=False, indent=1)
        failed = [c["check"] for c in truth["verification"] if not c["pass"]]
        print(f"{truth['file']}: {len(truth['verification'])} checks, {len(failed)} failed", *failed, sep="\n  ")
    videos = []
    for key in NAMES:
        path = os.path.join(WORK, NAMES[key] + ".truth.json")
        if not os.path.exists(path):
            print(f"missing {path}; run: python3 gen_hard.py {key}")
            return 2
        with open(path, encoding="utf-8") as f:
            videos.append(json.load(f))
    chrome_version = run([CHROME, "--version"]).stdout.strip()
    op_bias = [c["measured"] - c["expected"] for c in videos[0]["verification"] if c["check"].endswith("opacity from the same fit")]
    doc = {
        "schema": "video-eval-truth/1",
        "created": "2026-09-27",
        "generator": "gen_hard.py (python3 gen_hard.py regenerates every file and this truth)",
        "renderer": f"{chrome_version}, headless, DevTools pipe; each frame = all CSS animations paused at currentTime = t, "
                    "then Page.captureScreenshot (h1 with Emulation deviceScaleFactor 2); encoded with ffmpeg libx264 "
                    "yuv420p bt709 limited range (crf 14; h2 -bf 0 so the mp4 duration equals the presentation span; "
                    "h3 crf 16 -tune stillimage)",
        "renderer_notes": [
            "Chrome rasterises a moving layer in sub-pixel steps and opacity in 1/255 steps, so a rendered position can "
            "differ from the CSS value by a fraction of a pixel; pixel checks carry tolerances for this.",
            "Near the end of a soft ease-out Chrome holds the layer at its last raster step, then re-rasterises it once "
            "as part of the page; that step is where observed_in_file.last_change usually lands.",
            "cv2.VideoCapture decodes these BT.709 files with a BT.601 matrix: geometry fits use it, colour checks use "
            "an ffmpeg rgb24 decode with in_color_matrix=bt709.",
            "h1 is rendered at deviceScaleFactor 2: every file-pixel distance is 2x the CSS distance.",
            f"h1 opacity fitted against the final (flattened) frame reads {min(op_bias):+.3f} to {max(op_bias):+.3f} "
            "versus the CSS value while the rows are still composited layers (same effect as evalset v1).",
        ],
        "time_convention": "Seconds of presentation time (pts) from the first frame. In the CFR files frame n shows the page "
                           "state at exactly t = n / fps. In the VFR file each kept frame shows the state at its own pts. "
                           "start_s/end_s/duration_ms/delay_ms/easing are the authored CSS values (exact). observed_in_file "
                           "gives what pixel differencing can actually see (threshold 8 levels on the max of B, G, R, 20 "
                           "pixels; the same thresholds are used at DPR 2, where 20 file px = 5 CSS px).",
        "field_notes": {"delay_ms": "offset from the first event of the same group (stagger delay); independent events "
                                    "have 0 and a delay_note",
                        "css_animation_delay_ms": "animation-delay as written in the page CSS (relative to t = 0)",
                        "peak_speed_px_s": "magnitude / duration x max slope of the easing curve (exact parametric "
                                           "derivative), in CSS px per second",
                        "geometry": "h1 only: CSS viewport, device scale factor and which fields are CSS vs file px",
                        "overshoot": "h2 badge only: analytic maximum of the easing (y > 1) and when it happens",
                        "luma": "h2 colour event: BT.709/BT.601 luma of both colours and what greyscale detectors see",
                        "distractors": "moving things that must NOT be reported as intended entrance/exit/transition "
                                       "motion (they may be named as loops)",
                        "named_curve_check": "h1: distance of the custom easing from 80+ named curves"},
        "scoring_hints": {"start_s": "+-1 frame (16.7 ms at 60 fps, 33.3 ms at 30 fps)",
                          "duration_ms": "+-1 frame of the CSS duration; a value between observed (last_change - start) "
                                         "and the CSS duration is 'observed-correct'",
                          "delay_ms": "+-1 frame", "easing": "each control point +-0.1, or max |y_pred(x)-y_true(x)| "
                                                             "<= 0.05 on x = 0..1 (progress_at_x has the truth curve)",
                          "translate_px": "CSS px; for h1 a value in file px (2x) is wrong",
                          "colour": "h2 record-colour: property background-color (or 'color'/'colour' change) with a blue "
                                    "start and a red end; any hue-only / luma-constant wording is a bonus, not required",
                          "overshoot": "h2 badge-pop: easing must exceed 1 somewhere (p1y or p2y > 1) to be correct",
                          "cuts_s": "+-1 frame", "narration_s": "+-150 ms",
                          "text": "exact after whitespace normalisation; Korean CER <= 5% as partial credit",
                          "distractors": "reporting a distractor as an intended event is a false positive"},
        "videos": videos,
    }
    with open(os.path.join(HERE, "truth_hard.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    total = sum(len(v["verification"]) for v in videos)
    bad = sum(1 for v in videos for c in v["verification"] if not c["pass"])
    print(f"truth_hard.json written: {total} checks, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
