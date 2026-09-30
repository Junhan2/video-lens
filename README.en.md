# video-lens

A Claude Code skill that measures video instead of eyeballing it. Everything runs on your Mac.

Claude cannot watch video. video-lens measures it frame by frame, hands Claude numbers and text first, and shows images only for the moments that need a look.

- **UI motion:** start, duration, easing (cubic-bezier) and stagger of each animation, frame-accurate, written out as CSS.
- **Talks and demos:** scene cuts, on-screen text (Korean included), a speech transcript and audio/video sync, all with timestamps.
- **Nothing uploaded:** ffmpeg, OpenCV, macOS Vision, macOS on-device speech recognition and whisper.cpp.

[한국어](README.md)

## How it differs

### Versus the /watch skill

| | /watch (claude-video 0.1.3) | video-lens |
|---|---|---|
| Frames seen | at most 2 per second, 100 in total | every frame |
| Time precision | whole seconds | one frame (±16.7 ms at 60 fps) |
| A 300 ms animation | lands on 0 or 1 frame | duration, easing and CSS measured |
| Speech | uploads the audio to Groq/OpenAI unless English captions exist | transcribed on device (Korean included) |
| Scene cuts, on-screen text | no | cut times, text per slide |

/watch is good for a quick gist. video-lens is for when you need to know when, for how long and how.

### Versus the model alone (measured)

Given a video without the skill, the model writes throwaway ffmpeg and Python code to measure it. We ran 9 tasks with known answers, 27 runs per condition.

| Model | Condition | Mean score | Worst run | Cost per run | Time per run |
|---|---|---|---|---|---|
| Opus 5.5 | no skill | 0.949 | 0.17 | $0.86 | 420 s |
| Opus 5.5 | **video-lens** | **0.956** | **0.80** | **$0.66** | **265 s** |
| Sonnet 5.5 | no skill | 0.970 | 0.60 | $0.63 | 288 s |
| Sonnet 5.5 | **video-lens** | 0.940 | 0.63 | **$0.41** | **122 s** |

- **Accuracy is about the same:** slightly higher with the skill on Opus, slightly lower on Sonnet.
- **Cost and time drop:** 23 % cheaper and 37 % faster on Opus, 35 % cheaper and 58 % faster on Sonnet.
- **Fewer bad misses:** without the skill the model rewrites its measuring code every time, and one task swung between a perfect score and 0.17.

## Where it shines

- **Long videos:** a 10-minute Korean lecture cost 30 % less and took 74 % less time on Opus. The longer the video, the bigger the gap.
- **Repeated motion:** a carousel's repeated transition is measured once as a group; cost fell by more than half and time to about a fifth (a real recording, so its answer key is approximate and the accuracy comparison is indicative only).
- **Private talks and meetings:** speech is transcribed without leaving the machine, and slide text comes with timestamps.
- **Design QA:** questions that need a number, such as "is this transition really 400 ms ease-out?".

For a one-line summary of a short clip you do not need it; loading the skill costs a little extra.

## Install

In Claude Code:

```
/plugin marketplace add Junhan2/video-lens
/plugin install video-lens@video-lens
```

Then in a terminal:

```
brew install ffmpeg
pip3 install opencv-python numpy
xcode-select --install   # builds the on-screen text and speech helpers
```

- Tested with Python 3.13. If a package is missing, the skill prints the exact install command in one line.

- macOS only; tested on macOS 26 (Apple Silicon). On-device speech recognition (SpeechTranscriber) needs macOS 26.
- Optional: `brew install whisper-cpp` and a ggml model (for example `ggml-large-v3-turbo-q5_0.bin`) in `~/.local/share/whisper/` to transcribe with whisper as well.
- Optional: Node 24 and Google Chrome let it read a web page's declared CSS animations and compare them with the measurement.
- Optional: yt-dlp lets it download a video from a URL first.

## Use

Ask as usual and Claude picks the skill. In a test where the skill was not named, Opus 5.5 picked it by itself in 8 of 9 tasks.

```
Analyse the animation in this screen recording so I can rebuild it in CSS
List when each slide appears in this lecture and what it says
What was said around 12:00?
```

To call it directly, start with `/video-lens`.

When several elements move at once (for example a list whose rows enter one after another), naming the area ("only the list on the left") makes the result more precise.

## How it was measured

- Answer keys come from real CSS animations rendered frame by frame in headless Chrome, so the truth is the CSS as written. The lectures use macOS text-to-speech narration.
- 9 tasks: card stagger, a VFR modal, an off-screen toast with a click sound, an overlapping list with a custom curve (30 fps, Retina), a colour change with an overshoot badge and a drawer, a real 3D carousel recording, 20-second and 10-minute Korean lectures, and a one-line summary.
- On 2026-09-30 each condition got the same prompt and model (effort high), no MCP servers, one run at a time. In the no-skill condition the skill was hidden so the model could not find it on disk.
- Tolerances: start and duration ±1 frame, easing within 0.05 of the true curve, sounds ±10 ms, speech timing ±150 ms. Costs are the API-equivalent figures Claude Code reports.

## Limits

- UIs where several elements move at once can come out as one merged element or many fragments in the first pass. Narrowing the area and measuring again fixes it; Opus tends to do that on its own, Sonnet less so.
- Motion over photo or gradient backgrounds and noisy real-world speech are not well tested yet.
- No speaker labels, language auto-detection or music tempo.

## License

MIT
