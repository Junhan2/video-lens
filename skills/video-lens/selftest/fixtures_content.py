"""Content fixtures (spec 10.2), ported from D2 gen_synth.py + render_slide/render_sub.swift and D3 make_talk.py /
ko_slide.html. Korean text is drawn by swift/render.swift (Hershey has no Hangul); each build_* writes one video.
"""
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

import fixtures_core as fc
from vl.swiftbuild import helper_path

# D2 synth_talk: cuts at 6.0 and 15.5, dissolve 11.25-11.75, three 100 ms flashes on slide 3, slide 4 = slide 1
SYNTH_SIZE = (1280, 720)
SYNTH_FPS = 30
SYNTH_SECONDS = 20.0
SYNTH_SLIDES = {
    1: ((30, 60, 120), ("영상 분석 테스트", "첫 번째 장면입니다", "Scene one: introduction")),
    2: ((120, 40, 40), ("두 번째 장면", "장면 전환 감지 2026년 9월 27일", "가격 12,900원")),
    3: ((20, 100, 60), ("세 번째 장면", "Audio sync check", "자막 위치 확인")),
}
SYNTH_TITLE_PT = 64
SYNTH_BODY_PT = 36
SYNTH_CUTS = (6.0, 15.5)
SYNTH_DISSOLVE = (11.25, 11.75)
SYNTH_FLASHES = (12.5, 13.5, 14.5)
SYNTH_FLASH_S = 0.1
SYNTH_FLASH_BOX = ((900, 450), (1180, 650))

# D3 talk10m: 40 slides x 15 s, 1080p30, each with a 4-digit code, plus a slowly moving grey dot as the "speaker"
TALK_SIZE = (1920, 1080)
TALK_FPS = 30
TALK_SLIDES = 40
TALK_SLIDE_S = 15.0
TALK_CODE_SEED = 1

# Same-template deck: 8 slides x 6 s that differ only in a title word and a code (slide 6 repeats slide 2). At 160 px
# some swaps change fewer pixels than any survey threshold (talk10 review fixture: 0.08 %), so only the DETAIL_W
# comparison finds them.
DECK_SIZE = (1280, 720)
DECK_FPS = 30
DECK_SLIDE_S = 6.0
DECK_WORDS = ("사과", "바다", "기차", "하늘", "연필", "바다", "나무", "우산")
DECK_REPEAT = {5: 1}            # slide index 5 shows slide index 1
DECK_FONT = "AppleSDGothicNeo-Regular"      # 48/36 pt: every swap moves 0.01 to 0.17 % of the 160 px pixels
DECK_SLIDES = len(DECK_WORDS)

# D3 ko_slide.html, drawn natively at 1920x1080 (h1 96 px, two 44 px lines, a 24 px source line)
KO_SLIDE_SIZE = (1920, 1080)
KO_SLIDE_LINES = ((96, "분기 실적 요약"), (44, "매출 전년 대비 23% 증가"), (44, "신규 고객 1,284명 확보"),
                  (24, "출처: 내부 집계 2026년 3분기"))
KO_SLIDE_SECONDS = 2

# D2 subtitle legibility: D2's sentence at 24/32/48 pt over 8 px hue stripes, 1920x1080, one second each; the
# frames are byte-identical to D2's sub_24/32/48.png. Vision's accurate level finds no line at all in some other
# sizes on this background at 1920 px (46-50 pt, measured 2026-09-27), independent of glyph height.
SUB_SIZE = (1920, 1080)
SUB_FPS = 30
SUB_TEXT = "그래서 이 버튼을 누르면 결제 화면으로 바로 넘어갑니다"
SUB_POINTS = (24, 32, 48)


def render(path, size, lines, *options):
    """swift/render.swift: `lines` are (points, text) pairs; `options` are extra render flags."""
    width, height = size
    specs = [f"{points}|{text}" for points, text in lines]
    subprocess.run([str(helper_path("render")), str(path), str(width), str(height), *map(str, options), *specs], check=True)


def synth_slides(folder):
    """{1: bgr, 2: bgr, 3: bgr}: D2's three slides, byte-identical to its s1-s3.png."""
    slides = {}
    for number, (rgb, (title, *body)) in SYNTH_SLIDES.items():
        path = Path(folder) / f"s{number}.png"
        lines = [(SYNTH_TITLE_PT, title)] + [(SYNTH_BODY_PT, text) for text in body]
        render(path, SYNTH_SIZE, lines, "--bg", ",".join(map(str, rgb)))
        slides[number] = cv2.imread(str(path))
    return slides


def synth_frame(slides, t):
    if t < SYNTH_CUTS[0]:
        return slides[1]
    if t < SYNTH_DISSOLVE[0]:
        return slides[2]
    if t < SYNTH_DISSOLVE[1]:
        alpha = (t - SYNTH_DISSOLVE[0]) / (SYNTH_DISSOLVE[1] - SYNTH_DISSOLVE[0])
        return cv2.addWeighted(slides[2], 1 - alpha, slides[3], alpha, 0)
    if t < SYNTH_CUTS[1]:
        frame = slides[3].copy()
        if any(start <= t < start + SYNTH_FLASH_S for start in SYNTH_FLASHES):
            cv2.rectangle(frame, *SYNTH_FLASH_BOX, (255, 255, 255), -1)
        return frame
    return slides[1]


def build_synth_talk(path):
    with tempfile.TemporaryDirectory() as folder:
        slides = synth_slides(folder)
        frames = (synth_frame(slides, index / SYNTH_FPS) for index in range(int(SYNTH_SECONDS * SYNTH_FPS)))
        fc.encode_bgr(frames, path, SYNTH_SIZE, SYNTH_FPS)


def talk_codes():
    """The 4-digit code shown on each of the 40 slides (same generator as D3 make_talk.py)."""
    rng = np.random.default_rng(TALK_CODE_SEED)
    return [int(rng.integers(1000, 9999)) for _ in range(TALK_SLIDES)]


def talk_slide(number, code):
    width, height = TALK_SIZE
    slide = np.full((height, width, 3), (250 - number * 3 % 60, 240, 230), np.uint8)
    cv2.putText(slide, f"SLIDE {number + 1:02d}", (160, 300), cv2.FONT_HERSHEY_DUPLEX, 5, (30, 30, 30), 10)
    cv2.putText(slide, f"topic code {code}", (160, 520), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (60, 60, 160), 4)
    for row in range(5):
        cv2.putText(slide, f"- bullet {row + 1} of slide {number + 1}", (200, 650 + row * 70),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (50, 50, 50), 2)
    return slide


def talk_frames():
    for number, code in enumerate(talk_codes()):
        slide = talk_slide(number, code)
        for index in range(int(TALK_SLIDE_S * TALK_FPS)):
            t = number * TALK_SLIDE_S + index / TALK_FPS
            frame = slide.copy()
            centre = (int(1650 + 60 * np.sin(t * 1.3)), int(900 + 30 * np.cos(t * 0.9)))
            cv2.circle(frame, centre, 40, (80, 80, 80), -1)
            yield frame


def build_talk10m(path):
    """crf 18 like every fixture, but preset veryfast: 18,000 1080p frames take minutes at the default preset."""
    width, height = TALK_SIZE
    proc = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                             "-s", f"{width}x{height}", "-r", str(TALK_FPS), "-i", "-", "-c:v", "libx264",
                             "-preset", "veryfast", "-crf", fc.CRF, "-pix_fmt", "yuv420p", str(path)], stdin=subprocess.PIPE)
    for frame in talk_frames():
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed to encode {path}")


def deck_code(number):
    """The code slide `number` (0-based) shows; letters avoid I and O, which OCR reads as digits."""
    shown = DECK_REPEAT.get(number, number)
    return f"KEY-{shown + 1:02d}{'ABCDEFGH'[shown]}"


def deck_slide(folder, number):
    shown = DECK_REPEAT.get(number, number)
    png = Path(folder) / f"deck_{shown}.png"
    lines = [(48, f"제{shown + 1}장 키워드 {DECK_WORDS[shown]}"), (36, deck_code(number)), (24, "video-lens 리뷰")]
    render(png, DECK_SIZE, lines, "--bg", "250,250,250", "--fg", "30,30,30", "--x", 100, "--font", DECK_FONT)
    return cv2.imread(str(png))


def build_template_deck(path):
    with tempfile.TemporaryDirectory() as folder:
        slides = [deck_slide(folder, number) for number in range(DECK_SLIDES)]
    per_slide = int(DECK_SLIDE_S * DECK_FPS)
    fc.encode_bgr((slide for slide in slides for _ in range(per_slide)), path, DECK_SIZE, DECK_FPS)


def build_ko_slide(path):
    with tempfile.TemporaryDirectory() as folder:
        png = Path(folder) / "ko_slide.png"
        render(png, KO_SLIDE_SIZE, KO_SLIDE_LINES, "--bg", "247,244,238", "--fg", "51,51,51", "--x", 160, "--y0", 216)
        fc.ffmpeg("-loop", 1, "-i", png, "-t", KO_SLIDE_SECONDS, "-r", 30, "-c:v", "libx264", "-crf", fc.CRF,
                  "-pix_fmt", "yuv420p", path)


def subtitle_frame(folder, points):
    png = Path(folder) / f"sub_{points}.png"
    render(png, SUB_SIZE, [(points, SUB_TEXT)], "--stripes", "--font", "AppleSDGothicNeo-Medium", "--stroke", 3,
           "--center", "--bottom", 60)
    return cv2.imread(str(png))


def subtitle_glyph_px(image):
    """Ink height of the subtitle line in source px: rows holding the white fill or the black outline."""
    ink = (image.min(axis=2) > 230) | (image.max(axis=2) < 40)
    rows = np.flatnonzero(ink.any(axis=1))
    return int(rows[-1] - rows[0] + 1) if rows.size else 0


def build_subtitles(path):
    """One second per subtitle size, in SUB_POINTS order."""
    with tempfile.TemporaryDirectory() as folder:
        stills = [subtitle_frame(folder, points) for points in SUB_POINTS]
    frames = (still for still in stills for _ in range(SUB_FPS))
    fc.encode_bgr(frames, path, SUB_SIZE, SUB_FPS)


def subtitle_glyphs():
    """{points: ink height in source px}, measured on freshly rendered frames."""
    with tempfile.TemporaryDirectory() as folder:
        return {points: subtitle_glyph_px(subtitle_frame(folder, points)) for points in SUB_POINTS}
