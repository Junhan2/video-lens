# Changelog

## 1.0.1 (2026-09-30)

- A first run without numpy or OpenCV prints one line with the exact `pip install` command (exit 5) instead of a traceback.
- Colour-only changes (for example a button fill) are typed `color` again, including buttons with a static border and ghost-to-filled buttons.

## 1.0.0 (2026-09-30)

First public release.

- Frame-accurate UI motion: start, duration, easing (named curves or a fitted cubic-bezier with near ties and ranges), stagger, edge-clipped entrances, overshoot, repeats, CSS output.
- Content: scene cuts, keyframes, Korean and English on-screen text (macOS Vision), speech transcripts (Apple SpeechTranscriber on device, or whisper.cpp), audio onsets and audio/video sync.
- Output folders record which video they belong to, so parallel sessions never mix results.
- Self-test: `vl.py selftest` runs known-answer checks on synthetic clips.
