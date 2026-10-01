# Changelog

## 1.1.0 (2026-10-01)

- Scene digest, only on request: `vl.py digest OUT` picks scenes (a YouTube video's chapters when it has them, else screen states merged at cuts), saves one capture per scene and contact sheets; Claude writes one or two lines per scene and `vl.py digest OUT --captions FILE` renders `digest.md` and a self-contained `digest.html` with links to each moment. Ordinary analyses never make one.
- The capture for a scene is the screen shown longest in it, and menu bars, clocks and watermarks are left out of the scene text.
- YouTube downloads that fail with HTTP 403 are retried once with another player client, and a failed download leaves no partial files. A hand download made with `yt-dlp --write-info-json` keeps its chapters.

## 1.0.1 (2026-09-30)

- A first run without numpy or OpenCV prints one line with the exact `pip install` command (exit 5) instead of a traceback.

## 1.0.0 (2026-09-30)

First public release.

- Frame-accurate UI motion: start, duration, easing (named curves or a fitted cubic-bezier with near ties and ranges), stagger, edge-clipped entrances, overshoot, repeats, CSS output.
- Content: scene cuts, keyframes, Korean and English on-screen text (macOS Vision), speech transcripts (Apple SpeechTranscriber on device, or whisper.cpp), audio onsets and audio/video sync.
- Output folders record which video they belong to, so parallel sessions never mix results.
- Colour-only changes (for example a button fill) are typed `color`, including buttons with a static border and ghost-to-filled buttons.
- Self-test: `vl.py selftest` runs known-answer checks on synthetic clips.
