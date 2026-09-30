"""Audio and speech fixtures (spec 10.3), ported from D2 gen_synth.py: a 20 s synthetic talk with Korean TTS,
cuts, a dissolve, flashes with beeps and an audio track 40 ms late; the Jian paragraph of A3/A8, its 10 minute
loop (A5) and the reference subtitles of A6.

Speech comes from macOS `say` with ko_KR voices. Every build_* writes one file at the path it is given.
"""
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

from fixtures_core import encode_bgr, ffmpeg

TALK_SIZE = (640, 360)
TALK_FPS = 30
TALK_SECONDS = 20.0
CUTS_S = (6.0, 15.5)
DISSOLVE_S = (11.25, 11.75)
FLASHES_S = (12.5, 13.5, 14.5)
FLASH_SECONDS = 0.1
FLASH_BOX = (400, 220, 600, 330)
AUDIO_LATE_S = 0.040            # the whole audio stream is muxed 40 ms after the picture (-itsoffset)
AUDIO_RATE = 48000
BEEP_HZ = 1000
BEEP_AMPLITUDE = 0.5
TALK_VOICE = "Yuna"
TALK_LINES = ((0.40, "안녕하세요."),                                # in-track start (s), text
              (2.00, "오늘은 영상 분석 도구를 소개합니다."),
              (6.20, "두 번째 장면에서는 장면 전환을 확인합니다."),
              (15.70, "마지막으로 처음 화면으로 돌아옵니다."))
PARAGRAPH_VOICE = "Jian (Premium)"
PARAGRAPH = ("안녕하세요. 오늘은 랜딩 페이지의 첫 화면 애니메이션을 분석해 보겠습니다. 먼저 제목이 아래에서 위로 "
             "올라오면서 나타나고, 약 영점 삼 초 뒤에 설명 문구가 이어서 등장합니다. 버튼은 마지막에 나타나는데, "
             "마우스를 올리면 색이 부드럽게 바뀝니다. 이 영상에서 중요한 점은 세 요소 사이의 간격이 일정하다는 "
             "것입니다. 다음 장면에서는 가격표 화면으로 넘어가서, 월 구독과 연 구독의 차이를 설명하겠습니다.")
PARAGRAPH_LEAD_S = 0.5
LOOP_REPEATS = 20
LOOP_GAP_S = 1.0
NON_SPEECH_SECONDS = 30.0
NON_SPEECH_BEEP_EVERY_S = 3.0      # 880 Hz, 200 ms: whisper wrote an invented Korean sentence over this
NON_SPEECH_BEEP = (880, 0.2)
STILL_SIZE = "160x90"
STILL_FPS = 2
REFERENCE_CUES = ((0.483, 1.477, "안녕하세요."),                    # A6: odd milliseconds test the ±1 ms rule
                  (2.031, 5.109, "<i>오늘은</i> 영상 분석 도구를\n소개합니다."),
                  (6.289, 9.851, "두 번째 장면에서는 장면 전환을 확인합니다."),
                  (15.781, 18.999, "마지막으로 처음 화면으로 돌아옵니다."))
REFERENCE_TEXTS = ("안녕하세요.", "오늘은 영상 분석 도구를 소개합니다.", "두 번째 장면에서는 장면 전환을 확인합니다.",
                   "마지막으로 처음 화면으로 돌아옵니다.")


def korean_voices():
    """Installed ko_KR voice names, e.g. 'Yuna' or 'Jian (Premium)'."""
    listing = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    return [line.split("  ")[0].strip() for line in listing.splitlines() if " ko_KR " in line]


def has_voice(name):
    return name in korean_voices()


def tts(text, voice):
    """Mono float32 PCM at AUDIO_RATE of `say -v voice text`."""
    with tempfile.TemporaryDirectory() as folder:
        aiff = Path(folder) / "tts.aiff"
        subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
        raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(aiff), "-ac", "1", "-ar", str(AUDIO_RATE),
                              "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def place(track, clip, at_s):
    first = round(at_s * AUDIO_RATE)
    track[first:first + len(clip)] += clip[:max(0, len(track) - first)]


def beep():
    t = np.arange(round(FLASH_SECONDS * AUDIO_RATE)) / AUDIO_RATE
    return (BEEP_AMPLITUDE * np.sin(2 * np.pi * BEEP_HZ * t)).astype(np.float32)


def talk_track():
    track = np.zeros(round(TALK_SECONDS * AUDIO_RATE), np.float32)
    for at_s, text in TALK_LINES:
        place(track, tts(text, TALK_VOICE), at_s)
    for flash_s in FLASHES_S:
        place(track, beep(), flash_s)
    return track


def slides():
    """Four flat slides with a few blocks; slide 4 repeats slide 1 (D2 synth_talk layout)."""
    width, height = TALK_SIZE
    looks = (((236, 236, 236), (60, 60, 60)), ((150, 90, 40), (240, 240, 240)), ((60, 130, 60), (230, 230, 230)))
    images = []
    for background, ink in looks:
        image = np.full((height, width, 3), background, np.uint8)
        cv2.rectangle(image, (40, 40), (600, 90), ink, -1)
        cv2.rectangle(image, (40, 130), (360, 150), ink, -1)
        cv2.rectangle(image, (40, 180), (300, 200), ink, -1)
        images.append(image)
    return images + [images[0]]


def talk_frame(t, images):
    if t < CUTS_S[0]:
        return images[0]
    if t < DISSOLVE_S[0]:
        return images[1]
    if t < DISSOLVE_S[1]:
        alpha = (t - DISSOLVE_S[0]) / (DISSOLVE_S[1] - DISSOLVE_S[0])
        return cv2.addWeighted(images[1], 1 - alpha, images[2], alpha, 0)
    if t < CUTS_S[1]:
        frame = images[2].copy()
        if any(start <= t < start + FLASH_SECONDS - 1e-9 for start in FLASHES_S):
            cv2.rectangle(frame, FLASH_BOX[:2], FLASH_BOX[2:], (255, 255, 255), -1)
        return frame
    return images[3]


def mux_audio(video, track, path, late_s=0.0):
    """video + float track as AAC; `late_s` shifts the whole audio stream on the timeline."""
    with tempfile.TemporaryDirectory() as folder:
        wav = Path(folder) / "track.wav"
        proc = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(AUDIO_RATE),
                               "-ac", "1", "-i", "-", "-c:a", "pcm_s16le", str(wav)], input=track.tobytes())
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg failed to write the fixture audio")
        ffmpeg("-i", video, *(["-itsoffset", f"{late_s:.3f}"] if late_s else []), "-i", wav,
               "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", path)


def build_talk(path):
    """20 s, 30 fps: cuts 6.0/15.5, dissolve 11.25-11.75, flashes 12.5/13.5/14.5 (100 ms) with beeps, Korean
    speech; the audio stream is AUDIO_LATE_S late, so beeps sound at 12.540/13.540/14.540."""
    images = slides()
    with tempfile.TemporaryDirectory() as folder:
        video = Path(folder) / "video.mp4"
        frames = (talk_frame(i / TALK_FPS, images) for i in range(round(TALK_SECONDS * TALK_FPS)))
        encode_bgr(frames, video, TALK_SIZE, TALK_FPS)
        mux_audio(video, talk_track(), path, AUDIO_LATE_S)


def reference_srt():
    return "\n".join(f"{n}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n"
                     for n, (start, end, text) in enumerate(REFERENCE_CUES, 1))


def reference_vtt():
    cues = "\n".join(f"{srt_time(start).replace(',', '.')} --> {srt_time(end).replace(',', '.')}\n{text}\n"
                     for start, end, text in REFERENCE_CUES)
    return "WEBVTT\n\n" + cues


def srt_time(t):
    millis = round(t * 1000)
    return f"{millis // 3600_000:02d}:{millis // 60_000 % 60:02d}:{millis // 1000 % 60:02d},{millis % 1000:03d}"


def build_text(content):
    return lambda path: Path(path).write_text(content, encoding="utf-8")


def build_talk_with_subtitles(talk_path):
    """The talk with REFERENCE_CUES muxed as a mov_text stream (language kor)."""
    def build(path):
        with tempfile.TemporaryDirectory() as folder:
            srt = Path(folder) / "reference.srt"
            srt.write_text(reference_srt(), encoding="utf-8")
            ffmpeg("-i", talk_path, "-i", srt, "-map", "0", "-map", "1", "-c", "copy", "-c:s", "mov_text",
                   "-metadata:s:s:0", "language=kor", path)
    return build


def still_video(path, seconds):
    ffmpeg("-f", "lavfi", "-i", f"color=c=0x303840:s={STILL_SIZE}:r={STILL_FPS}:d={seconds:.3f}",
           "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", path)


def speech_video(path, track):
    """A still video as long as `track` (float PCM at AUDIO_RATE) with the track as its audio."""
    with tempfile.TemporaryDirectory() as folder:
        video = Path(folder) / "still.mp4"
        still_video(video, len(track) / AUDIO_RATE)
        mux_audio(video, track, path)


def build_paragraph(path):
    """A3/A8: the Jian paragraph (about 30 s) after PARAGRAPH_LEAD_S of silence."""
    speech_video(path, paragraph_track())


def build_loop(path):
    """A5: the paragraph looped LOOP_REPEATS times, about 10 minutes."""
    speech_video(path, loop_track(paragraph_track()))


def build_non_speech(path):
    """No speech at all: a 200 ms 880 Hz beep every 3 s over digital silence (whisper hallucination case)."""
    hz, seconds = NON_SPEECH_BEEP
    t = np.arange(round(seconds * AUDIO_RATE)) / AUDIO_RATE
    tone = (BEEP_AMPLITUDE * np.sin(2 * np.pi * hz * t)).astype(np.float32)
    track = np.zeros(round(NON_SPEECH_SECONDS * AUDIO_RATE), np.float32)
    for at_s in np.arange(0.0, NON_SPEECH_SECONDS, NON_SPEECH_BEEP_EVERY_S):
        place(track, tone, at_s)
    speech_video(path, track)


def paragraph_track():
    speech = tts(PARAGRAPH, PARAGRAPH_VOICE)
    track = np.zeros(round(PARAGRAPH_LEAD_S * AUDIO_RATE) * 2 + len(speech), np.float32)
    place(track, speech, PARAGRAPH_LEAD_S)
    return track


def loop_track(paragraph):
    """LOOP_REPEATS copies of the paragraph track with LOOP_GAP_S of silence between them (about 10 min)."""
    gap = np.zeros(round(LOOP_GAP_S * AUDIO_RATE), np.float32)
    return np.concatenate([part for _ in range(LOOP_REPEATS) for part in (paragraph, gap)][:-1])
