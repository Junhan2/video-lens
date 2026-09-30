#!/usr/bin/env python3
"""Ground-truth evaluation set for video-analysis skills.

Builds four short videos whose motion, audio, scene and text facts are known exactly,
then verifies each file numerically and writes truth.json (+ truth_frames.json).

    python3 gen.py            # everything lands next to this file; scratch in ./_work

Rendering: real headless Google Chrome driven over the DevTools pipe (--remote-debugging-pipe).
For every output frame n every CSS animation on the page is paused and seeked to
currentTime = n / fps, then one screenshot is taken. The page CSS is the literal source of
truth (delays, durations, cubic-bezier numbers); Chrome's own getComputedStyle values are
recorded per frame and compared with an independent cubic-bezier solver.
(`chrome --headless --screenshot` was tried first: Chrome 153 writes the PNG but never exits.)

Needs only: Google Chrome, ffmpeg/ffprobe, Python 3 + numpy + cv2, macOS `say`.
Optional check: /usr/bin/swift (macOS Vision OCR of the Korean slides).
"""
import base64
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
PIX_THRESHOLD = 8      # grey-level difference that counts as "changed" (same as motion-timing)
MIN_PIXELS = 20        # changed pixels needed before a region counts as changed
X264 = ["-c:v", "libx264", "-preset", "slow", "-crf", "14", "-pix_fmt", "yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
BT709_VF = "scale=out_color_matrix=bt709:out_range=tv"


# ---------------------------------------------------------------- helpers

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
    """Max dy/dx of cubic-bezier p = (p1x, p1y, p2x, p2y), from the exact parametric derivative
    (finite differences on x under-read the x = 0 slope p1y/p1x: 624.9 instead of 625.0)."""
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


# ---------------------------------------------------------------- Chrome over the DevTools pipe

class Chrome:
    def __init__(self, width, height):
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
                  {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
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

    def js(self, expr):  # Runtime.evaluate inside our own generated page
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
    out[el.dataset.probe] = {transform: cs.transform, opacity: parseFloat(cs.opacity)};
  });
  return out;
})()"""

RECT_JS = """(() => { const out = {};
  document.querySelectorAll('[data-probe]').forEach(el => { const r = el.getBoundingClientRect();
    out[el.dataset.probe] = {x: r.x, y: r.y, w: r.width, h: r.height}; });
  return out; })()"""


def render_animation(name, html, width, height, fps, n_frames, rect_at_ms):
    """Render n_frames of `html` at t = n / fps. Returns (frames_dir, per-frame states, rects)."""
    vdir = os.path.join(WORK, name)
    frames = os.path.join(vdir, "frames")
    shutil.rmtree(vdir, ignore_errors=True)
    os.makedirs(frames)
    page = os.path.join(vdir, "page.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(html)
    chrome = Chrome(width, height)
    try:
        chrome.open(page)
        chrome.js(SEEK_JS % float(rect_at_ms))
        rects = chrome.js(RECT_JS)
        states = []
        for n in range(n_frames):
            t_ms = n * 1000.0 / fps
            states.append(chrome.js(SEEK_JS % t_ms))
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
            page = os.path.join(vdir, f"slide{i + 1}.html")
            with open(page, "w", encoding="utf-8") as f:
                f.write(html)
            chrome.open(page)
            png = os.path.join(vdir, f"slide{i + 1}.png")
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
    cap = cv2.VideoCapture(path)
    frames = []
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        frames.append(fr)
    return frames


def changed_pixels(a, b, box):
    x0, y0, x1, y1 = box
    d = cv2.absdiff(a[y0:y1, x0:x1], b[y0:y1, x0:x1]).max(axis=2)
    return int((d > PIX_THRESHOLD).sum())


def observed_window(frames, pts, box, t_from=0.0):
    """What a perfect frame-differencing tool can see inside `box` (pixel box x0,y0,x1,y1).

    first_change: first frame differing from the frame before it (after t_from).
    last_change:  last frame differing from the frame before it.
    before_first: pts of the frame just before first_change (the last still frame)."""
    moving = [n for n in range(1, len(frames))
              if pts[n] >= t_from - 1e-9 and changed_pixels(frames[n], frames[n - 1], box) >= MIN_PIXELS]
    if not moving:
        return None
    first, last = moving[0], moving[-1]
    return {"first_change_frame": first, "first_change_pts_s": round(pts[first], 6),
            "last_still_before_pts_s": round(pts[first - 1], 6),
            "last_change_frame": last, "last_change_pts_s": round(pts[last], 6),
            "changed_frames": len(moving), "box_xyxy": list(box)}


def split_runs(frames_idx, max_gap):
    runs, cur = [], [frames_idx[0]]
    for n in frames_idx[1:]:
        if n - cur[-1] <= max_gap:
            cur.append(n)
        else:
            runs.append(cur)
            cur = [n]
    runs.append(cur)
    return runs


def weight_map(frame, bg, box):
    x0, y0, x1, y1 = box
    return cv2.absdiff(frame[y0:y1, x0:x1], bg[y0:y1, x0:x1]).astype(np.float64).sum(axis=2)


def fit_warp(frame, bg, ref, box, make_matrix, values, margin=48):
    """Least-squares fit, on luma inside `box`, of  frame = bg + alpha * warp_v(ref - bg).

    `ref` shows the element in a known state, `bg` shows the page without it. make_matrix(v, X0, Y0)
    returns the 2x3 forward affine for candidate v in the coordinates of a crop starting at (X0, Y0).
    Returns (best v, alpha). Luma only: yuv420p halves chroma resolution and blurs colour edges."""
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
        cc = float((c * c).sum())   # elementwise: BLAS matmul raises spurious FP warnings on macOS
        if cc == 0:
            continue
        a = float((d * c).sum()) / cc
        r = float(((d - a * c) ** 2).mean())
        if best is None or r < best[0]:
            best = (r, float(v), a)
    return best[1], best[2]


def shift_y(v, X0, Y0):
    return np.float32([[1, 0, 0], [0, 1, v]])


def scale_about(cx, cy):
    return lambda s, X0, Y0: np.float32([[s, 0, (cx - X0) * (1 - s)], [0, s, (cy - Y0) * (1 - s)]])


def decode_audio(path, rate=48000):
    """Left channel only. L and R are identical here; ffmpeg's '-ac 1' downmix would add +3 dB
    (click peak 0.6421 instead of the per-channel 0.4541) and shift every dBFS threshold."""
    raw = run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-af", "pan=mono|c0=c0", "-ar", str(rate),
               "-f", "f32le", "-"], text=False).stdout
    return np.frombuffer(raw, np.float32)


def speech_spans(x, rate, win_s=0.010, floor_dbfs=-45.0):
    """10 ms RMS windows above floor_dbfs -> active. Returns list of active window start times."""
    hop = int(win_s * rate)
    n = len(x) // hop
    rms = np.sqrt((x[: n * hop].reshape(n, hop).astype(np.float64) ** 2).mean(axis=1) + 1e-20)
    db = 20 * np.log10(rms)
    return np.where(db > floor_dbfs)[0] * win_s, win_s


def check(name, measured, expected, tol, unit):
    ok = abs(measured - expected) <= tol
    return {"check": name, "measured": round(measured, 4), "expected": round(expected, 4),
            "tolerance": tol, "unit": unit, "pass": bool(ok)}


def encode_cfr(frames_dir, fps, out, audio_wav=None):
    cmd = ["ffmpeg", "-y", "-v", "error", "-framerate", str(fps), "-i", os.path.join(frames_dir, "%05d.png")]
    if audio_wav:
        cmd += ["-i", audio_wav]
    cmd += ["-vf", BT709_VF] + X264
    if audio_wav:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    run(cmd + ["-movflags", "+faststart", out])


def write_wav(path, mono, rate=48000, channels=2):
    pcm = np.clip(np.round(mono * 32767), -32768, 32767).astype(np.int16)
    if channels == 2:
        pcm = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


# ---------------------------------------------------------------- page templates

BASE_CSS = """*{box-sizing:border-box}
html,body{margin:0;overflow:hidden;color:#16181d;
  font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif;-webkit-font-smoothing:antialiased}"""

V1_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Projects</title><style>
%s
html,body{width:1280px;height:800px;background:#f5f6f8}
header{height:64px;display:flex;align-items:center;gap:36px;padding:0 48px;background:#fff;border-bottom:1px solid #e4e7ec}
.logo{font-weight:700;font-size:18px;letter-spacing:-.01em}
nav{display:flex;gap:24px;font-size:14px;color:#667085} nav .on{color:#16181d;font-weight:600}
.search{margin-left:auto;width:300px;height:38px;border:1.5px solid #4c6ef5;border-radius:9px;display:flex;
  align-items:center;padding:0 12px;font-size:14px;background:#fff}
.caret{display:inline-block;width:2px;height:18px;background:#4c6ef5;margin-left:1px;
  animation:blink 1060ms step-end infinite}
@keyframes blink{0%%{opacity:1}50%%{opacity:0}100%%{opacity:0}}
main{padding:52px 48px 0}
h1{font-size:30px;line-height:36px;margin:0 0 8px;letter-spacing:-.02em}
.lead{margin:0 0 36px;font-size:16px;line-height:24px;color:#667085}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:24px}
.card{height:340px;background:#fff;border:1px solid #e4e7ec;border-radius:14px;overflow:hidden;
  box-shadow:0 1px 2px rgba(16,24,40,.05),0 8px 20px rgba(16,24,40,.06);
  animation:card-in 400ms cubic-bezier(0.16,1,0.3,1) both}
.card:nth-child(1){animation-delay:1000ms}
.card:nth-child(2){animation-delay:1100ms}
.card:nth-child(3){animation-delay:1200ms}
@keyframes card-in{from{opacity:0;transform:translateY(40px)}to{opacity:1;transform:translateY(0)}}
.thumb{height:168px}
.c1 .thumb{background:linear-gradient(135deg,#c5d3ff,#eef2ff)}
.c2 .thumb{background:linear-gradient(135deg,#b8f0d8,#ecfdf5)}
.c3 .thumb{background:linear-gradient(135deg,#ffd6b3,#fff4ea)}
.body{padding:20px 22px} .body h3{margin:0 0 8px;font-size:18px;line-height:24px}
.body p{margin:0 0 16px;font-size:14px;line-height:21px;color:#667085} .meta{font-size:12px;color:#98a2b3}
</style></head><body>
<header><div class="logo">Northwind</div>
<nav><span class="on">Projects</span><span>Reports</span><span>Team</span><span>Settings</span></nav>
<div class="search"><span>motion</span><span class="caret" data-probe="caret"></span></div></header>
<main><h1>Recent projects</h1><p class="lead">Pick up where your team left off.</p>
<section class="grid">
<article class="card c1" data-probe="card1"><div class="thumb"></div><div class="body"><h3>Onboarding flow</h3>
<p>Five screens that take a new account from sign-up to its first project.</p><div class="meta">Updated 2 hours ago</div></div></article>
<article class="card c2" data-probe="card2"><div class="thumb"></div><div class="body"><h3>Q4 launch page</h3>
<p>Hero, pricing table and the FAQ for the October release.</p><div class="meta">Updated yesterday</div></div></article>
<article class="card c3" data-probe="card3"><div class="thumb"></div><div class="body"><h3>Design tokens</h3>
<p>Color, spacing and type scales shared by web and iOS.</p><div class="meta">Updated 3 days ago</div></div></article>
</section></main></body></html>""" % BASE_CSS

APP_SHELL_CSS = """html,body{width:1280px;height:800px;background:#eef0f3}
aside{position:absolute;left:0;top:0;bottom:0;width:220px;background:#fff;border-right:1px solid #e4e7ec;padding:24px 20px}
aside b{display:block;font-size:17px;margin-bottom:28px} aside div{font-size:14px;color:#667085;margin:0 0 16px}
.page{position:absolute;left:260px;top:40px;right:40px}
.page h1{font-size:26px;margin:0 0 20px;letter-spacing:-.02em}
.row{height:52px;background:#fff;border:1px solid #e4e7ec;border-radius:10px;margin-bottom:10px;display:flex;
  align-items:center;padding:0 18px;font-size:14px;gap:24px} .row span:last-child{margin-left:auto;color:#667085}"""

V2_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Projects</title><style>
%s
%s
.modal{position:absolute;left:400px;top:264px;width:480px;height:272px;background:#fff;border-radius:16px;padding:28px;
  box-shadow:0 24px 48px rgba(16,24,40,.18),0 2px 6px rgba(16,24,40,.08);transform-origin:50%% 50%%;
  animation:modal-in 240ms cubic-bezier(0,0,0.2,1) 800ms both, modal-out 160ms cubic-bezier(0.4,0,1,1) 2000ms forwards}
@keyframes modal-in{from{opacity:0;transform:scale(0.92)}to{opacity:1;transform:scale(1)}}
@keyframes modal-out{from{opacity:1;transform:scale(1)}to{opacity:0;transform:scale(0.96)}}
.modal h2{margin:0 0 10px;font-size:20px} .modal p{margin:0;font-size:15px;line-height:23px;color:#475467}
.btns{position:absolute;right:28px;bottom:28px;display:flex;gap:12px}
.btns span{height:40px;padding:0 18px;border-radius:9px;display:flex;align-items:center;font-size:14px;font-weight:600}
.cancel{border:1px solid #d0d5dd} .danger{background:#d92d20;color:#fff}
</style></head><body>
<aside><b>Northwind</b><div>Projects</div><div>Reports</div><div>Team</div><div>Settings</div></aside>
<div class="page"><h1>All projects</h1>
<div class="row"><span>Onboarding flow</span><span>Design</span><span>Edited 2h ago</span></div>
<div class="row"><span>Q4 launch page</span><span>Marketing</span><span>Edited yesterday</span></div>
<div class="row"><span>Design tokens</span><span>Platform</span><span>Edited 3 days ago</span></div>
<div class="row"><span>Billing revamp</span><span>Product</span><span>Edited last week</span></div>
<div class="row"><span>Help center</span><span>Support</span><span>Edited last week</span></div>
<div class="row"><span>Mobile nav</span><span>Design</span><span>Edited 2 weeks ago</span></div>
</div>
<div class="modal" data-probe="modal"><h2>Delete “Billing revamp”?</h2>
<p>This removes the project and its 14 files for everyone on the team. You can’t undo this.</p>
<div class="btns"><span class="cancel">Cancel</span><span class="danger">Delete project</span></div></div>
</body></html>""" % (BASE_CSS, APP_SHELL_CSS)

V3_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Uploads</title><style>
%s
%s
.panel{position:absolute;left:260px;top:110px;width:560px;height:120px;background:#fff;border:1px solid #e4e7ec;
  border-radius:12px;display:flex;align-items:center;gap:18px;padding:0 28px;font-size:15px}
.spinner{width:32px;height:32px;border-radius:50%%;border:4px solid #e4e7ec;border-top-color:#4c6ef5;
  animation:spin 800ms linear infinite}
@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
.panel small{display:block;color:#667085;font-size:13px;margin-top:4px}
.toast{position:absolute;right:24px;bottom:24px;width:320px;height:64px;border-radius:12px;background:#111827;color:#fff;
  display:flex;align-items:center;gap:12px;padding:0 18px;font-size:14px;box-shadow:0 4px 10px rgba(0,0,0,.18);
  animation:toast-in 300ms cubic-bezier(0.22,1,0.36,1) 1200ms both}
@keyframes toast-in{from{transform:translateX(360px)}to{transform:translateX(0)}}
.dot{width:22px;height:22px;border-radius:50%%;background:#12b76a;flex:none}
.toast span:last-child{margin-left:auto;color:#9db4ff;font-weight:600}
</style></head><body>
<aside><b>Northwind</b><div>Projects</div><div>Uploads</div><div>Team</div><div>Settings</div></aside>
<div class="page"><h1>Uploads</h1></div>
<div class="panel"><div class="spinner" data-probe="spinner"></div><div>Uploading 3 files…<small>brand-assets.zip · 42 MB</small></div></div>
<div class="toast" data-probe="toast"><div class="dot"></div><span>Changes saved</span><span>Undo</span></div>
</body></html>""" % (BASE_CSS, APP_SHELL_CSS)

SLIDES = [
    {"title_ko": "화면 녹화로 모션 읽기", "line_en": "Reading motion from screen recordings", "footer": "1 / 3",
     "narration_ko": "오늘은 화면 녹화에서 움직임을 재는 방법을 알아보겠습니다."},
    {"title_ko": "프레임 차이로 타이밍 재기", "line_en": "Timing from frame differences", "footer": "2 / 3",
     "narration_ko": "두 프레임의 차이를 계산하면 애니메이션이 시작하고 끝나는 시점을 알 수 있습니다."},
    {"title_ko": "글자와 음성은 따로 확인", "line_en": "Check on-screen text and speech separately", "footer": "3 / 3",
     "narration_ko": "마지막으로 화면 속 글자와 음성을 따로 확인하면 내용을 놓치지 않습니다."},
]
SLIDE_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>
%s
html,body{width:1280px;height:720px;background:#fbfbfa}
.bar{position:absolute;left:96px;top:190px;width:64px;height:8px;border-radius:4px;background:%s}
h1{position:absolute;left:96px;top:228px;margin:0;font-size:60px;line-height:76px;letter-spacing:-.02em;
  font-family:"Apple SD Gothic Neo",-apple-system,sans-serif;font-weight:700}
p{position:absolute;left:98px;top:334px;margin:0;font-size:30px;line-height:40px;color:#475467}
footer{position:absolute;right:72px;bottom:48px;font-size:20px;color:#98a2b3}
</style></head><body><div class="bar"></div><h1>%s</h1><p>%s</p><footer>%s</footer></body></html>"""
SLIDE_COLORS = ["#4c6ef5", "#12b76a", "#f79009"]


# ---------------------------------------------------------------- videos

def build_v1():
    name, fps, n = "v1_stagger", 60, 180
    frames_dir, states, rects = render_animation(name, V1_HTML, 1280, 800, fps, n, rect_at_ms=2500)
    out = os.path.join(HERE, name + ".mp4")
    encode_cfr(frames_dir, fps, out)
    info, pts = ffprobe(out)
    frames = read_frames(out)
    p = (0.16, 1, 0.3, 1)
    ease = cubic_bezier(*p)
    checks, events = [], []
    bg, final = frames[0], frames[-1]
    # Chrome's computed values vs the independent solver, every frame, every card
    for i in range(3):
        start = 1.0 + 0.1 * i
        err_ty = max(abs(parse_matrix(s[f"card{i + 1}"]["transform"])[5] - 40 * (1 - progress(k / fps, start, 400, ease)))
                     for k, s in enumerate(states))
        err_op = max(abs(s[f"card{i + 1}"]["opacity"] - progress(k / fps, start, 400, ease)) for k, s in enumerate(states))
        checks.append(check(f"card{i + 1} chrome translateY vs solver (max abs err, all frames)", err_ty, 0, 0.01, "px"))
        checks.append(check(f"card{i + 1} chrome opacity vs solver (max abs err, all frames)", err_op, 0, 0.001, "opacity"))
    png = lambda k: cv2.imread(os.path.join(frames_dir, f"{k:05d}.png"))
    for i in range(3):
        r = rects[f"card{i + 1}"]
        start = 1.0 + 0.1 * i
        mass_box = (math.ceil(r["x"]) + 4, int(r["y"]) - 20, int(r["x"] + r["w"]) - 4, int(r["y"] + r["h"]) + 80)
        edge_box = (math.ceil(r["x"]) + 4, int(r["y"]) - 15, int(r["x"] + r["w"]) - 4, int(r["y"]) + 185)  # no text
        s_final = weight_map(final, bg, mass_box).sum()
        for frac in (0.25, 0.5):
            k = int(round((start + 0.4 * frac) * fps))
            want = progress(k / fps, start, 400, ease)
            for label, fr, f0, fN in (("source PNG", png(k), png(0), png(n - 1)), ("decoded mp4", frames[k], bg, final)):
                ty, _ = fit_warp(fr, f0, fN, edge_box, shift_y, np.arange(0, 12.001, 0.01))
                checks.append(check(f"card{i + 1} {label} frame {k} (t={k / fps:.4f}s) translateY by sub-pixel "
                                    "shift fit (luma, border+thumb rows)", ty, 40 * (1 - want), 0.25, "px"))
            checks.append(check(f"card{i + 1} decoded frame {k} opacity from pixel mass",
                                weight_map(frames[k], bg, mass_box).sum() / s_final, want, 0.03, "opacity"))
    caret_r = rects["caret"]
    caret_box = (int(caret_r["x"]) - 1, int(caret_r["y"]) - 1, int(caret_r["x"] + caret_r["w"]) + 2,
                 int(caret_r["y"] + caret_r["h"]) + 2)
    toggles = [k for k in range(1, n) if states[k]["caret"]["opacity"] != states[k - 1]["caret"]["opacity"]]
    seen = [k for k in range(1, n) if changed_pixels(frames[k], frames[k - 1], caret_box) >= 10]
    checks.append({"check": "caret toggle frames: chrome opacity vs decoded pixels", "measured": seen,
                   "expected": toggles, "pass": seen == toggles})
    for i in range(3):
        r = rects[f"card{i + 1}"]
        start = 1.0 + 0.1 * i
        box = (math.ceil(r["x"]) + 4, int(r["y"]) - 20, int(r["x"] + r["w"]) - 4, int(r["y"] + r["h"]) + 80)
        obs = observed_window(frames, pts, box)
        mag = 40
        events.append({
            "id": f"card{i + 1}-enter", "element": f"card {i + 1} of 3 in the 'Recent projects' grid "
            f"(\"{['Onboarding flow', 'Q4 launch page', 'Design tokens'][i]}\")",
            "selector": f".card:nth-child({i + 1})", "kind": "entrance",
            "start_s": round(start, 3), "end_s": round(start + 0.4, 3), "duration_ms": 400,
            "delay_ms": 100 * i, "css_animation_delay_ms": 1000 + 100 * i,
            "easing": easing_block(p, "ease-out (expo-like)"),
            "properties": [{"property": "transform", "function": "translateY", "from": 40, "to": 0, "unit": "px"},
                           {"property": "opacity", "from": 0, "to": 1}],
            "direction": "up (moves from 40 px below its resting place to 0)", "magnitude_px": mag,
            "peak_speed_px_s": round(mag / 0.4 * peak_slope(p), 1),
            "bbox_final": {k2: round(v, 2) for k2, v in r.items()},
            "observed_in_file": obs,
        })
        if obs:
            events[-1]["observed_in_file"]["note"] = (
                "Pixels inside the card's own columns. The ease-out tail moves less than Chrome's 1/4 px raster "
                "step and 1/255 opacity step, so the pixels reach their final values ~2 frames before end_s.")
    caret_times = [round(k * 0.53, 2) for k in range(1, 6)]
    truth = {
        "file": name + ".mp4", "kind": "ui-motion", "container": info,
        "summary": "Web dashboard 'Northwind'. At 1.0 s three project cards fade in while rising 40 px, "
                   "400 ms each, staggered 100 ms (cards 1, 2, 3 left to right). A text caret blinks in the search box.",
        "events": events,
        "groups": [{"type": "stagger", "members": ["card1-enter", "card2-enter", "card3-enter"], "stagger_ms": 100,
                    "order": "left to right", "group_start_s": 1.0, "group_end_s": 1.6, "total_span_ms": 600}],
        "css_source": ".card{animation:card-in 400ms cubic-bezier(0.16,1,0.3,1) both} "
                      ".card:nth-child(1){animation-delay:1000ms} .card:nth-child(2){animation-delay:1100ms} "
                      ".card:nth-child(3){animation-delay:1200ms} "
                      "@keyframes card-in{from{opacity:0;transform:translateY(40px)}to{opacity:1;transform:translateY(0)}}",
        "expected_css_answer": {"duration": "400ms", "delays": ["0ms", "100ms", "200ms"],
                                "timing_function": "cubic-bezier(0.16, 1, 0.3, 1)",
                                "keyframes": "from {opacity: 0; transform: translateY(40px)} to {opacity: 1; transform: none}"},
        "distractors": [{
            "id": "caret-blink", "element": "text caret after 'motion' in the header search box", "selector": ".caret",
            "kind": "blink loop (not an entrance or transition)", "css": "animation: blink 1060ms step-end infinite",
            "period_ms": 1060, "toggle_interval_ms": 530, "starts_visible": True,
            "toggle_times_s": caret_times,
            "toggle_frames": toggles, "bbox": {k2: round(v, 2) for k2, v in caret_r.items()},
            "must_not_report_as": "an intended entrance/exit/transition event; at most mention it as a blinking caret loop",
        }],
        "traps": ["Toggles at 1.06 s and 1.59 s overlap the card animation window (1.0-1.6 s).",
                  "Cards 2 and 3 are still moving while card 1 settles; one bbox for the whole grid merges them."],
        "verification": checks,
    }
    frame_states = [{"n": k, "t_s": round(k / fps, 6),
                     **{f"card{i + 1}": {"translateY_px": round(parse_matrix(s[f'card{i + 1}']['transform'])[5], 4),
                                         "opacity": round(s[f'card{i + 1}']['opacity'], 5)} for i in range(3)},
                     "caret_opacity": s["caret"]["opacity"]} for k, s in enumerate(states)]
    return truth, frame_states


def build_v2():
    name, fps, n = "v2_modal_vfr", 60, 180
    frames_dir, states, rects = render_animation(name, V2_HTML, 1280, 800, fps, n, rect_at_ms=1500)
    # 1) ffmpeg mpdecimate decides which source frames a change-driven recorder would keep
    log = run(["ffmpeg", "-v", "info", "-framerate", str(fps), "-i", os.path.join(frames_dir, "%05d.png"),
               "-vf", "mpdecimate,showinfo", "-f", "null", "-"]).stderr
    kept = sorted({int(round(float(m) * fps)) for m in re.findall(r"pts_time:([\d.]+)", log)})
    decimated = list(kept)
    # 2) keep the final source frame too so the file lasts 3.0 s like a recording stopped at 3 s
    if n - 1 not in kept:
        kept.append(n - 1)
    out = os.path.join(HERE, name + ".mp4")
    select = "select='" + "+".join(f"eq(n,{k})" for k in kept) + "'"
    run(["ffmpeg", "-y", "-v", "error", "-framerate", str(fps), "-i", os.path.join(frames_dir, "%05d.png"),
         "-vf", f"{select},{BT709_VF}", "-fps_mode", "vfr"] + X264 + ["-movflags", "+faststart", out])
    info, pts = ffprobe(out)
    frames = read_frames(out)
    src_of = [int(round(t * fps)) for t in pts]
    checks = [{"check": "kept pts == mpdecimate decisions (+ final frame)", "measured": src_of, "expected": kept,
               "pass": src_of == kept},
              {"check": "decoded frame count == ffprobe pts count", "measured": len(frames), "expected": len(pts),
               "pass": len(frames) == len(pts)}]
    p_in, p_out = (0, 0, 0.2, 1), (0.4, 0, 1, 1)
    e_in, e_out = cubic_bezier(*p_in), cubic_bezier(*p_out)

    def css_state(t):
        if t < 2.0:
            y = progress(t, 0.8, 240, e_in)
            return 0.92 + 0.08 * y, y
        y = progress(t, 2.0, 160, e_out)
        return 1 - 0.04 * y, 1 - y
    err_s = max(abs(parse_matrix(s["modal"]["transform"])[0] - css_state(k / fps)[0]) for k, s in enumerate(states))
    err_o = max(abs(s["modal"]["opacity"] - css_state(k / fps)[1]) for k, s in enumerate(states))
    checks.append(check("chrome scale vs solver (max abs err, all 180 source frames)", err_s, 0, 1e-4, "scale"))
    checks.append(check("chrome opacity vs solver (max abs err, all 180 source frames)", err_o, 0, 1e-4, "opacity"))
    # pixel check on the decoded VFR file: fit scale (about the modal centre) and opacity against the
    # last kept frame before the exit, whose CSS state is scale 1, opacity 1
    full_idx = max(i for i, k in enumerate(kept) if k / fps < 2.0)
    checks.append(check("reference kept frame is fully in (CSS opacity)", css_state(kept[full_idx] / fps)[1], 1.0,
                        1e-6, "opacity"))
    r = rects["modal"]
    centre = scale_about(r["x"] + r["w"] / 2, r["y"] + r["h"] / 2)
    for target in (0.85, 0.9, 2.1):
        i = min(range(len(kept)), key=lambda j: abs(kept[j] / fps - target))
        t = kept[i] / fps
        scale_m, alpha = fit_warp(frames[i], frames[0], frames[full_idx], (330, 200, 950, 620), centre,
                                  np.arange(0.90, 1.00001, 0.0005))
        want_scale, want_op = css_state(t)
        checks.append(check(f"kept frame pts={t:.4f}s modal scale by least-squares fit (luma)", scale_m, want_scale,
                            0.002, "scale"))
        checks.append(check(f"kept frame pts={t:.4f}s modal opacity from the same fit", alpha, want_op, 0.02, "opacity"))
    kept_rows = [{"pts_s": round(t, 6), "source_frame": k, "scale": round(css_state(k / fps)[0], 5),
                  "opacity": round(css_state(k / fps)[1], 5)} for t, k in zip(pts, kept)]
    modal_box = (360, 220, 920, 600)
    obs_in = observed_window(frames[: full_idx + 1], pts, modal_box)
    obs_out = observed_window(frames, pts, modal_box, t_from=1.9)
    gaps = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1) if pts[i + 1] - pts[i] > 1.5 / fps]
    moving_dropped = [k for k in range(1, n) if css_state(k / fps) != css_state((k - 1) / fps) and k not in kept]
    r = rects["modal"]
    events = [
        {"id": "modal-enter", "element": "confirmation modal 'Delete “Billing revamp”?' centred on the page",
         "selector": ".modal", "kind": "entrance", "start_s": 0.8, "end_s": 1.04, "duration_ms": 240, "delay_ms": 0,
         "css_animation_delay_ms": 800, "easing": easing_block(p_in, "ease-out (Material decelerate)"),
         "properties": [{"property": "transform", "function": "scale", "from": 0.92, "to": 1, "origin": "50% 50%"},
                        {"property": "opacity", "from": 0, "to": 1}],
         "direction": "grows from the centre", "magnitude": "scale +0.08 (480x272 box grows from 441.6x250.2)",
         "bbox_final": {k2: round(v, 2) for k2, v in r.items()}, "observed_in_file": obs_in},
        {"id": "modal-exit", "element": "the same modal", "selector": ".modal", "kind": "exit", "start_s": 2.0,
         "end_s": 2.16, "duration_ms": 160, "delay_ms": 1200, "delay_note": "delay_ms = time from enter start to exit start",
         "css_animation_delay_ms": 2000, "easing": easing_block(p_out, "ease-in (Material accelerate)"),
         "properties": [{"property": "transform", "function": "scale", "from": 1, "to": 0.96, "origin": "50% 50%"},
                        {"property": "opacity", "from": 1, "to": 0}],
         "direction": "shrinks toward the centre while fading out", "magnitude": "scale -0.04",
         "observed_in_file": obs_out},
    ]
    truth = {
        "file": name + ".mp4", "kind": "ui-motion", "container": info,
        "summary": "Variable-frame-rate recording of a project list. A delete-confirmation modal scales up and fades in "
                   "at 0.8 s (240 ms, ease-out), stays, then shrinks slightly and fades out at 2.0 s (160 ms, ease-in).",
        "events": events,
        "vfr": {"source_fps": fps, "source_frames": n,
                "method": "ffmpeg mpdecimate (defaults hi=64*12, lo=64*5, frac=0.33) on the 60 fps source, "
                          "-fps_mode vfr; the final source frame is also kept so the file ends at 3.0 s",
                "mpdecimate_kept_source_frames": decimated, "kept_frames": kept_rows,
                "note": "Each kept frame shows the page state at its own pts. Frames absent from the file were "
                        "identical (or nearly identical) to the previous kept frame."},
        "css_source": ".modal{transform-origin:50% 50%; animation: modal-in 240ms cubic-bezier(0,0,0.2,1) 800ms both, "
                      "modal-out 160ms cubic-bezier(0.4,0,1,1) 2000ms forwards} "
                      "@keyframes modal-in{from{opacity:0;transform:scale(0.92)}to{opacity:1;transform:scale(1)}} "
                      "@keyframes modal-out{from{opacity:1;transform:scale(1)}to{opacity:0;transform:scale(0.96)}}",
        "expected_css_answer": {"enter": "240ms cubic-bezier(0, 0, 0.2, 1); scale 0.92 -> 1, opacity 0 -> 1",
                                "exit": "160ms cubic-bezier(0.4, 0, 1, 1); scale 1 -> 0.96, opacity 1 -> 0",
                                "gap_between": "exit starts 960 ms after the enter animation ends"},
        "distractors": [],
        "traps": ["The frame before the first changed frame has pts 0.0 s: a tool that takes 'last still frame' as "
                  "the start would report ~0 s instead of 0.8 s.",
                  "Frame gaps of " + ", ".join(f"{b - a:.3f} s ({a:.3f} -> {b:.3f})" for a, b in gaps) +
                  " are not pauses in the animation; the page was simply unchanged.",
                  ("Every source frame whose CSS state changed is in the file (mpdecimate dropped only still frames)"
                   if not moving_dropped else f"Source frames {moving_dropped} changed but were dropped by mpdecimate") +
                  ". The CSS ends (1.04 s, 2.16 s) are not on the 60 fps grid, so the last changed frames are "
                  f"{obs_in['last_change_pts_s']} s and {obs_out['last_change_pts_s']} s: durations measured from the "
                  f"true starts ({(obs_in['last_change_pts_s'] - 0.8) * 1000:.1f} ms, "
                  f"{(obs_out['last_change_pts_s'] - 2.0) * 1000:.1f} ms) run up to one frame LONGER than the CSS "
                  "durations (240 ms, 160 ms), not shorter."],
        "verification": checks,
    }
    frame_states = [{"n": k, "t_s": round(k / fps, 6), "kept": k in kept,
                     "modal": {"scale": round(parse_matrix(s["modal"]["transform"])[0], 6),
                               "opacity": round(s["modal"]["opacity"], 6)}} for k, s in enumerate(states)]
    return truth, frame_states


def build_v3():
    name, fps, n, rate = "v3_toast_sound", 60, 180, 48000
    frames_dir, states, rects = render_animation(name, V3_HTML, 1280, 800, fps, n, rect_at_ms=2500)
    click_s = 1.230
    audio = np.zeros(int(3.0 * rate))
    i0 = int(round(click_s * rate))
    tt = np.arange(int(0.008 * rate)) / rate
    audio[i0:i0 + len(tt)] = 0.5 * np.sin(2 * np.pi * 2500 * tt) * np.exp(-tt / 0.0012)
    wav = os.path.join(WORK, name, "click.wav")
    write_wav(wav, audio, rate)
    out = os.path.join(HERE, name + ".mp4")
    encode_cfr(frames_dir, fps, out, audio_wav=wav)
    info, pts = ffprobe(out)
    frames = read_frames(out)
    p = (0.22, 1, 0.36, 1)
    ease = cubic_bezier(*p)
    checks = []
    err_tx = max(abs(parse_matrix(s["toast"]["transform"])[4] - 360 * (1 - progress(k / fps, 1.2, 300, ease)))
                 for k, s in enumerate(states))
    checks.append(check("chrome toast translateX vs solver (max abs err, all frames)", err_tx, 0, 0.01, "px"))

    def spin_deg(s):
        a, b = parse_matrix(s["spinner"]["transform"])[:2]
        return math.degrees(math.atan2(b, a)) % 360
    err_sp = max(min(abs(spin_deg(s) - (360 * (k / fps) / 0.8) % 360), 360 - abs(spin_deg(s) - (360 * (k / fps) / 0.8) % 360))
                 for k, s in enumerate(states))
    checks.append(check("chrome spinner angle vs 450 deg/s linear (max abs err, all frames)", err_sp, 0, 0.01, "deg"))
    tr = rects["toast"]
    row = int(tr["y"] + tr["h"] / 2)
    page_bg = frames[0][row, 1000:1270].astype(float).mean(axis=0)
    toast_bg = frames[-1][row, int(tr["x"]) + 4].astype(float)
    for k in (78, 81, 84):  # t = 1.30, 1.35 (mid), 1.40
        line = frames[k][row].astype(float)
        cover = np.clip(((page_bg - line) / (page_bg - toast_bg)).mean(axis=1), 0, 1)
        j = int(np.argmax(cover >= 0.5))   # edge = where coverage crosses 0.5 between pixel centres
        left = (j - 0.5) + (0.5 - cover[j - 1]) / (cover[j] - cover[j - 1])
        want = tr["x"] + 360 * (1 - progress(k / fps, 1.2, 300, ease))
        checks.append(check(f"decoded frame {k} (t={k / fps:.4f}s) toast left edge (sub-pixel)", left, want, 0.35, "px"))
    sr = rects["spinner"]
    cx, cy = sr["x"] + sr["w"] / 2, sr["y"] + sr["h"] / 2
    for k in (0, 10, 30, 100):
        fr = frames[k].astype(float)
        y0, y1, x0, x1 = int(sr["y"]) - 2, int(sr["y"] + sr["h"]) + 3, int(sr["x"]) - 2, int(sr["x"] + sr["w"]) + 3
        patch = fr[y0:y1, x0:x1]
        blue = patch[..., 0] - patch[..., 2]   # accent #4c6ef5 has B-R = 169, the grey ring 8
        blue = np.where(blue > 60, blue, 0)
        ys, xs = np.mgrid[y0:y1, x0:x1]
        ang = math.degrees(math.atan2((blue * (ys + 0.5 - cy)).sum(), (blue * (xs + 0.5 - cx)).sum()))
        measured = (ang + 90) % 360
        want = (360 * (k / fps) / 0.8) % 360
        diff = min(abs(measured - want), 360 - abs(measured - want))
        checks.append({"check": f"decoded frame {k} spinner arc angle (0 = arc at top, clockwise)",
                       "measured": round(measured, 3), "expected": round(want, 3), "tolerance": 3.0, "unit": "deg",
                       "pass": diff <= 3.0})
    x = decode_audio(out, rate)
    peak = float(np.abs(x).max())
    onset10 = int(np.argmax(np.abs(x) >= 0.1 * peak)) / rate
    onset_60db = int(np.argmax(np.abs(x) >= 10 ** (-60 / 20))) / rate
    checks.append(check("decoded AAC click onset (first sample >= 10% of peak)", onset10, click_s, 0.002, "s"))
    checks.append(check("audio stream duration (ffprobe, edit list applied)", info["audio"]["duration_s"], 3.0, 1e-6, "s"))
    checks.append({"check": "decoded AAC sample count within one AAC frame of 3.0 s (encoder end padding)",
                   "measured": len(x), "expected": [144000, 144000 + 1024], "pass": 144000 <= len(x) <= 145024})
    toast_box = (900, int(tr["y"]) - 16, 1280, int(tr["y"] + tr["h"]) + 16)
    obs = observed_window(frames, pts, toast_box)
    mag = 360
    first_visible = obs["first_change_frame"]  # first decoded frame with toast (or its shadow) pixels on screen
    box_enters = next(k for k, s in enumerate(states) if tr["x"] + parse_matrix(s["toast"]["transform"])[4] < 1280)
    events = [{
        "id": "toast-enter", "element": "dark toast 'Changes saved' with an 'Undo' link, bottom-right corner",
        "selector": ".toast", "kind": "entrance", "start_s": 1.2, "end_s": 1.5, "duration_ms": 300, "delay_ms": 0,
        "css_animation_delay_ms": 1200, "easing": easing_block(p, "ease-out (quart-like)"),
        "properties": [{"property": "transform", "function": "translateX", "from": 360, "to": 0, "unit": "px"}],
        "direction": "right to left (slides in from beyond the right edge of the viewport)", "magnitude_px": mag,
        "fully_offscreen_before_start": True, "opacity_animated": False,
        "peak_speed_px_s": round(mag / 0.3 * peak_slope(p), 1),
        "bbox_final": {k2: round(v, 2) for k2, v in tr.items()},
        "observed_in_file": obs,
    }]
    truth = {
        "file": name + ".mp4", "kind": "ui-motion+audio", "container": info,
        "summary": "Uploads page with a loading spinner that turns continuously. At 1.2 s a dark 'Changes saved' toast "
                   "slides in from the right edge (300 ms, ease-out). A short click sound plays at 1.23 s, 30 ms after "
                   "the slide starts.",
        "events": events,
        "audio": {"sample_rate": rate, "channels": 2, "codec": "aac 192 kb/s",
                  "events": [{"id": "click", "kind": "click", "start_s": click_s,
                              "description": "8 ms 2.5 kHz decaying sine burst (time constant 1.2 ms), peak 0.5 FS before AAC; "
                                             "digital silence everywhere else",
                              "decoded_onset_s_10pct_peak": round(onset10, 5),
                              "decoded_onset_s_minus60dbfs": round(onset_60db, 5),
                              "decoded_first_nonzero_s": round(int(np.argmax(x != 0)) / rate, 5),
                              "decoded_peak": round(peak, 4),
                              "decoded_measured_on": "one channel of the decoded AAC (L and R are bit-identical); "
                                                     "an ffmpeg '-ac 1' downmix is +3 dB (peak 0.6421, -60 dBFS "
                                                     "onset 1.22888 s). The first non-zero sample is AAC pre-echo."}],
                  "sync": {"sound_minus_motion_ms": 30, "meaning": "the click lags the toast motion start by 30 ms "
                           "(sound 1.230 s vs motion 1.200 s); first visible toast pixels appear at frame "
                           f"{first_visible} (t={first_visible / fps:.4f} s)"},
                  "silence_elsewhere": True},
        "css_source": ".toast{animation:toast-in 300ms cubic-bezier(0.22,1,0.36,1) 1200ms both} "
                      "@keyframes toast-in{from{transform:translateX(360px)}to{transform:translateX(0)}} "
                      ".spinner{animation:spin 800ms linear infinite} "
                      "@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}",
        "expected_css_answer": {"duration": "300ms", "timing_function": "cubic-bezier(0.22, 1, 0.36, 1)",
                                "keyframes": "from {transform: translateX(360px)} to {transform: none}"},
        "distractors": [{
            "id": "spinner", "element": "loading spinner (ring with one blue arc) left of 'Uploading 3 files…'",
            "selector": ".spinner", "kind": "continuous rotation loop (not an entrance or transition)",
            "css": "animation: spin 800ms linear infinite", "period_ms": 800, "speed_deg_s": 450, "direction": "clockwise",
            "easing": "linear", "angle_at_t0_deg": 0, "runs_s": [0.0, 3.0],
            "bbox": {k2: round(v, 2) for k2, v in sr.items()},
            "must_not_report_as": "a discrete intended event with a start/end; may be reported as a looping spinner "
                                  "with an 800 ms period"}],
        "traps": ["Every frame differs somewhere because of the spinner; a whole-frame change detector finds one "
                  "3-second event.",
                  f"The toast's first visible frame is {first_visible} (t={first_visible / fps:.4f} s), not 1.2 s: at "
                  "t=1.2 its 360 px offset puts it (and its shadow) beyond the right edge. Its border box first "
                  f"crosses the edge at frame {box_enters} (t={box_enters / fps:.4f} s)."],
        "verification": checks,
    }
    frame_states = [{"n": k, "t_s": round(k / fps, 6),
                     "toast_translateX_px": round(parse_matrix(s["toast"]["transform"])[4], 4),
                     "spinner_deg": round(spin_deg(s), 3)} for k, s in enumerate(states)]
    return truth, frame_states


def build_v4():
    name, fps, rate = "v4_slides_ko", 30, 48000
    cut_frames = [0, 195, 396]            # 0.0 s, 6.5 s, 13.2 s
    n = 600                               # 20.0 s
    pages = [SLIDE_HTML % (BASE_CSS, SLIDE_COLORS[i], s["title_ko"], s["line_en"], s["footer"]) for i, s in enumerate(SLIDES)]
    vdir, pngs = render_stills(name, pages, 1280, 720)
    frames_dir = os.path.join(vdir, "frames")
    os.makedirs(frames_dir)
    for k in range(n):
        slide = sum(1 for c in cut_frames if k >= c) - 1
        os.link(pngs[slide], os.path.join(frames_dir, f"{k:05d}.png"))
    track = np.zeros(int(20.0 * rate))
    narr = []
    for i, s in enumerate(SLIDES):
        raw = os.path.join(vdir, f"narration{i + 1}.wav")
        run(["say", "-v", "Yuna", "-o", raw, "--file-format=WAVE", "--data-format=LEI16@48000", s["narration_ko"]])
        with wave.open(raw) as w:
            x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float64) / 32768
        above = np.where(np.abs(x) > 0.01)[0]
        x = x[above[0]:above[-1] + 1]          # trim say's leading/trailing silence
        at = int(round((cut_frames[i] / fps + 0.5) * rate))
        track[at:at + len(x)] += x
        narr.append({"raw_len_s": len(x) / rate, "placed_at_s": at / rate})
    wav = os.path.join(vdir, "narration.wav")
    write_wav(wav, track, rate)
    out = os.path.join(HERE, name + ".mp4")
    encode_cfr(frames_dir, fps, out, audio_wav=wav)
    info, pts = ffprobe(out)
    checks = []
    cap = cv2.VideoCapture(out)
    prev, diffs, mids, noisy = None, [], {}, []
    mid_frames = [int((cut_frames[i] + (cut_frames[i + 1] if i < 2 else n)) / 2) for i in range(3)]
    k = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if prev is not None:
            diffs.append(float(cv2.absdiff(fr, prev).mean()))
            g = cv2.absdiff(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY))
            changed = int((g > PIX_THRESHOLD).sum())
            if k not in cut_frames and changed >= MIN_PIXELS:   # x264 noise between identical source frames
                noisy.append((k, changed, int(g.max())))
        if k in mid_frames:
            mids[k] = fr
        prev, k = fr, k + 1
    cuts_seen = [i + 1 for i, d in enumerate(diffs) if d > 1.0]
    checks.append({"check": "hard cuts found by consecutive-frame mean abs diff > 1.0", "measured": cuts_seen,
                   "expected": cut_frames[1:], "pass": cuts_seen == cut_frames[1:],
                   "max_diff_outside_cuts": round(max(d for i, d in enumerate(diffs) if i + 1 not in cut_frames), 4)})
    x = decode_audio(out, rate)
    active, win = speech_spans(x, rate)
    bounds = [c / fps for c in cut_frames] + [n / fps]
    narration = []
    for i, s in enumerate(SLIDES):
        a = active[(active >= bounds[i]) & (active < bounds[i + 1])]
        start, end = float(a[0]), float(a[-1] + win)
        narration.append({"slide": i + 1, "text": s["narration_ko"], "language": "ko", "voice": "macOS say -v Yuna",
                          "start_s": round(start, 3), "end_s": round(end, 3), "duration_s": round(end - start, 3),
                          "placed_start_s": round(narr[i]["placed_at_s"], 3),
                          "placed_end_s": round(narr[i]["placed_at_s"] + narr[i]["raw_len_s"], 3)})
        checks.append(check(f"slide {i + 1} narration start (decoded, 10 ms RMS > -45 dBFS) vs placement",
                            start, narr[i]["placed_at_s"], 0.02, "s"))
    sil = run(["ffmpeg", "-v", "info", "-i", out, "-map", "0:a", "-af", "silencedetect=noise=-45dB:d=0.4",
               "-f", "null", "-"]).stderr
    silence = [(float(a), float(b)) for a, b in zip(re.findall(r"silence_start: ([\d.]+)", sil),
                                                       re.findall(r"silence_end: ([\d.]+)", sil))]
    ocr = ocr_check(vdir, mids, mid_frames)
    if ocr:
        checks.extend(ocr)
    scenes = [{"index": i + 1, "start_s": round(bounds[i], 3), "end_s": round(bounds[i + 1], 3),
               "start_frame": cut_frames[i], "end_frame_exclusive": cut_frames[i + 1] if i < 2 else n,
               "accent_bar_color": SLIDE_COLORS[i]} for i in range(3)]
    truth = {
        "file": name + ".mp4", "kind": "lecture (Korean)", "container": info,
        "summary": "Three static lecture slides with Korean titles and one English line each, hard cuts at 6.5 s and "
                   "13.2 s, Korean narration (macOS voice Yuna) starting 0.5 s after each cut.",
        "scenes": scenes, "cuts_s": [6.5, 13.2], "cut_frames": cut_frames[1:], "transition": "hard cut (no fade)",
        "text": [{"slide": i + 1, "title_ko": s["title_ko"], "line_en": s["line_en"], "footer": s["footer"],
                  "visible_s": [scenes[i]["start_s"], scenes[i]["end_s"]]} for i, s in enumerate(SLIDES)],
        "narration": narration,
        "audio": {"sample_rate": rate, "channels": 2, "codec": "aac 192 kb/s",
                  "speech_detector": "10 ms RMS windows above -45 dBFS on one channel of the decoded mp4 audio (L and R "
                                     "are identical; a '-ac 1' downmix is +3 dB); start = first active window in the "
                                     "slide, end = end of the last active window in the slide",
                  "ffmpeg_silencedetect_-45dB_0.4s": [[round(a, 3), round(b, 3)] for a, b in silence]},
        "events": [], "distractors": [],
        "traps": ["The spoken sentence and the slide title are different texts; a summary must not merge them.",
                  "No motion between cuts: slides are fully static, so no animation events should be reported.",
                  "x264 noise right after each I-frame changes a few title-glyph pixels between identical source "
                  "frames: " + ", ".join(f"frame {k} ({c} px > {PIX_THRESHOLD} grey, max {m})" for k, c, m in noisy) +
                  f". A detector at {PIX_THRESHOLD} grey / {MIN_PIXELS} px sees these as tiny events; they are "
                  "codec noise, not motion."],
        "verification": checks,
    }
    return truth, None


OCR_SWIFT = r"""import Foundation
import Vision
import AppKit
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let img = NSImage(contentsOf: url), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { exit(2) }
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.recognitionLanguages = ["ko-KR", "en-US"]
req.usesLanguageCorrection = false
try VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
for o in (req.results ?? []) { if let t = o.topCandidates(1).first { print(t.string) } }
"""


def ocr_check(vdir, mids, mid_frames):
    """Optional: read the decoded mid-slide frames back with macOS Vision OCR (Korean + English)."""
    swift = shutil.which("swift")
    if not swift:
        return None
    src = os.path.join(vdir, "ocr.swift")
    with open(src, "w") as f:
        f.write(OCR_SWIFT)
    results = []
    for i, k in enumerate(mid_frames):
        png = os.path.join(vdir, f"decoded_mid_{i + 1}.png")
        cv2.imwrite(png, mids[k])
        r = subprocess.run([swift, src, png], capture_output=True, text=True, timeout=300)
        lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        norm = lambda s: re.sub(r"\s+", "", s)
        want = [SLIDES[i]["title_ko"], SLIDES[i]["line_en"], SLIDES[i]["footer"]]
        found = [any(norm(w) == norm(ln) for ln in lines) for w in want]
        results.append({"check": f"slide {i + 1} Vision OCR on decoded frame {k} finds every string exactly",
                        "measured": lines, "expected": want, "pass": all(found)})
    return results


def main():
    os.makedirs(WORK, exist_ok=True)
    chrome_version = run([CHROME, "--version"]).stdout.strip()
    videos, frame_truth = [], {}
    for build in (build_v1, build_v2, build_v3, build_v4):
        truth, states = build()
        videos.append(truth)
        if states:
            frame_truth[truth["file"]] = states
        failed = [c["check"] for c in truth["verification"] if not c["pass"]]
        print(f"{truth['file']}: {len(truth['verification'])} checks, {len(failed)} failed", *failed, sep="\n  ")
    doc = {
        "schema": "video-eval-truth/1",
        "created": "2026-09-27",
        "generator": "gen.py (python3 gen.py regenerates every file and this truth)",
        "renderer": f"{chrome_version}, headless, DevTools pipe; each frame = all CSS animations paused at "
                    "currentTime = t, then Page.captureScreenshot; encoded with ffmpeg libx264 crf 14 yuv420p bt709",
        "renderer_notes": ["Chrome rasterises a moving layer in 1/4 px steps and opacity in 1/255 steps, so a rendered "
                           "position can differ from the CSS value by up to 0.125 px; measured on the source PNGs.",
                           "Near the end of an ease-out Chrome holds the layer at its last raster step for a few frames, "
                           "then snaps it to the final position and re-rasterises it once as part of the page (in v1 "
                           "two frames before end_s, while the CSS values are still 0.003 px / 0.0001 short of final); "
                           "that step is where observed_in_file.last_change usually lands.",
                           "The composited layer's pixels differ by about 1% of (card - background) from the final "
                           "flattened page, so opacity measured against the last frame reads up to +0.03 high late in "
                           "the v1 fades (above 1.0 while the layer is held). Measured against the last layered frame "
                           "on the high-contrast thumbnails it matches the CSS opacity within 0.02; the white card body "
                           "is only ~10 grey levels from the background and cannot resolve opacity finer than ~0.04."],
        "time_convention": "Seconds of presentation time (pts) from the first frame. In the CFR files frame n shows the "
                           "page state at exactly t = n / fps. In the VFR file each kept frame shows the state at its "
                           "own pts. start_s/end_s/duration_ms/delay_ms/easing are the authored CSS values (exact). "
                           "observed_in_file gives what pixel differencing can actually see (threshold 8 grey levels, "
                           "20 pixels) and is where perfect measurements land when the easing tail is sub-pixel.",
        "field_notes": {"delay_ms": "offset from the first event of the same group (stagger delay); for the v2 exit it "
                                    "is the offset from the enter start",
                        "css_animation_delay_ms": "animation-delay as written in the page CSS (relative to t = 0)",
                        "peak_speed_px_s": "magnitude / duration x max slope of the easing curve (exact parametric "
                                           "derivative; for these ease-outs the max is p1y/p1x at x = 0)",
                        "observed_in_file.changed_frames": "counted on cv2's BGR decode (max channel > 8, >= 20 px); "
                                                           "it can differ by 1 with another YUV->RGB matrix (v1 card2 "
                                                           "frame 87 has 24 changed px via cv2, 18 via BT.709). "
                                                           "first/last_change frames do not depend on it.",
                        "distractors": "moving things that must NOT be reported as intended entrance/exit/transition "
                                       "motion (they may be named as loops)"},
        "scoring_hints": {"start_s": "+-1 frame (16.7 ms at 60 fps, 33.3 ms at 30 fps)",
                          "duration_ms": "+-1 frame of the CSS duration; a value between observed "
                                         "(last_change - start) and the CSS duration is 'observed-correct'",
                          "delay_ms": "+-1 frame", "easing": "each control point +-0.1, or max |y_pred(x)-y_true(x)| "
                                                             "<= 0.05 on x = 0..1 (progress_at_x has the truth curve)",
                          "audio_onset_s": "+-10 ms", "sync_ms": "+-10 ms", "cuts_s": "+-1 frame",
                          "narration_s": "+-150 ms", "text": "exact after whitespace normalisation; Korean CER <= 5% "
                                                            "as partial credit",
                          "distractors": "reporting a distractor as an intended event is a false positive"},
        "videos": videos,
    }
    with open(os.path.join(HERE, "truth.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    with open(os.path.join(HERE, "truth_frames.json"), "w", encoding="utf-8") as f:
        json.dump({"note": "Per-frame values read back from Chrome getComputedStyle at each rendered source frame "
                           "(for v2 this includes frames that the VFR file dropped; see 'kept').",
                   "videos": frame_truth}, f, ensure_ascii=False)
    total = sum(len(v["verification"]) for v in videos)
    bad = sum(1 for v in videos for c in v["verification"] if not c["pass"])
    print(f"truth.json written: {total} checks, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
