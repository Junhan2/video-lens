# video-lens benchmark

The question: given the same video question, how do accuracy, cost and time compare between a model working alone (writing its own ffmpeg and Python) and the same model using video-lens?

Interactive charts: <https://junhan2.github.io/video-lens/#benchmarks>. Scripts, prompts, answer-key generators and per-run results for all 216 published runs (`results.csv`): [bench/](../bench/README.md).

## Contents

- [Results](#results)
- [Tasks](#tasks)
- [Conditions](#conditions)
- [Fairness measures](#fairness-measures)
- [Scoring and tolerances](#scoring-and-tolerances)
- [Per-task results](#per-task-results)
- [Other video skills](#other-video-skills)
- [Caveats](#caveats)
- [Reproduce](#reproduce)

## Results

<!-- results:start -->
<!-- Generated from docs/data/benchmark.json by tools/render_results.py. Do not edit by hand. -->

| Model | Condition | Mean score | Worst run | Runs scoring 1.0 | Avg cost per run | Avg time per run | Avg turns |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Claude Opus 5.5 | Model alone | 0.949 | 0.167 | 21/27 | $0.859 | 420.2 s | 17.5 |
| Claude Opus 5.5 | With video-lens | 0.956 | 0.800 | 15/27 | $0.663 | 265.1 s | 10.1 |
| Claude Sonnet 5.5 | Model alone | 0.970 | 0.600 | 22/27 | $0.626 | 287.9 s | 20.9 |
| Claude Sonnet 5.5 | With video-lens | 0.940 | 0.625 | 15/27 | $0.408 | 121.8 s | 10.0 |
| Grok 4.7 | Model alone | 0.819 | 0.000 | 14/27 | $0.444 | 785.0 s | 23.3 |
| Grok 4.7 | With video-lens | 0.942 | 0.667 | 14/27 | $0.324 | 1055.9 s | 17.7 |

- Claude Opus 5.5 with video-lens: cost 23% lower, time 37% lower.
- Claude Sonnet 5.5 with video-lens: cost 35% lower, time 58% lower.
- Grok 4.7 with video-lens: cost 27% lower, time 35% higher.

Measured 2026-09-29 to 2026-10-03. 9 tasks × 3 runs = 27 runs per condition, no MCP servers, one run at a time. Mean score, cost, time and turns are means over all runs; the worst run is the lowest single score.

- Claude Opus 5.5: run in Claude Code, effort high. Cost is the API-equivalent `total_cost_usd` that Claude Code reports. Time is the run duration that Claude Code reports.
- Claude Sonnet 5.5: run in Claude Code, effort high. Cost is the API-equivalent `total_cost_usd` that Claude Code reports. Time is the run duration that Claude Code reports.
- Grok 4.7: run in Grok Build CLI, effort xhigh. Cost is the API-equivalent `total_cost_usd` that Grok Build CLI reports. Time is the wall-clock duration of the run.

<!-- results:end -->

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/charts/accuracy-dark.png">
  <img alt="Dumbbell chart on a 0 to 1 axis: mean score and worst run for each model, model alone and with video-lens. Values are in the table above." src="assets/charts/accuracy-light.png" width="800">
</picture>

## Tasks

<!-- tasks:start -->
<!-- Generated from docs/data/benchmark.json by tools/render_results.py. Do not edit by hand. -->

| ID | Task |
| --- | --- |
| `v1` | Card stagger (3 cards, 60 fps, named curve) |
| `v2` | Modal enter and exit (VFR recording) |
| `v3` | Off-screen toast with a click sound |
| `h1` | Overlapping 6-row list, custom curve, 30 fps, Retina |
| `h2` | Colour change, overshoot badge, bottom drawer (VFR) |
| `orbit` | Real 3D card carousel recording, 16.5 s (approximate answer key) |
| `v4` | 20-second Korean lecture, 3 slides |
| `h3` | 10-minute Korean lecture, 24 slides |
| `gist` | Two-sentence summary of a clip |

<!-- tasks:end -->

- **Motion (v1, v2, v3, h1, h2).** The prompt asks for everything needed to rebuild the animation in CSS: which element moves, when it starts, for how long, which properties, the stagger between elements, and the easing as cubic-bezier. v3 also asks when the sound plays and how far it is from the start of the motion. Answer key: the CSS as written, rendered frame by frame in headless Chrome. v2 and h2 are variable frame rate recordings; h1 is 30 fps at twice the page resolution (Retina). Distractors such as a blinking caret, a spinner or a shimmer loop should be reported as loops, not as intended motion.
- **orbit.** A real 16.5-second screen recording of a 3D card carousel: what moves, how many transitions, when each starts, how long one takes and its easing. The key was read by hand, so it is approximate.
- **Lectures (v4, h3).** Slide change times, all text on each slide, and every narrated sentence with its start and end. Slides are rendered, the narration is Korean macOS text-to-speech.
- **gist.** A two-sentence description of the carousel clip, scored with a keyword rubric.

Clip sizes, distractors and the number of checks per task: [bench/README.md, Tasks](../bench/README.md#tasks). The prompts are in Korean, exactly as they were run.

## Conditions

- Measured from 2026-09-29 to 2026-10-01 with the video-lens 1.0.0 pre-release build, except /watch: its 27 runs were redone on 2026-10-02 and 03 with claude-video 0.3.2, replacing the 0.1.3 runs. Release 1.0.1 only changes the message printed when numpy or OpenCV is missing; 1.1.0 adds the on-request scene digest and a retry for YouTube downloads, and the tasks use neither.
- Claude Opus 5.5 and Claude Sonnet 5.5 in Claude Code, effort high. Grok 4.7 in the Grok Build CLI 1.0.40, reasoning effort xhigh.
- Two conditions per model: **model alone** (no skills load and video-lens is locked on disk) and **with video-lens** (the prompt starts with `/video-lens`). Opus 5.5 also ran with /watch and with video-use; see [Other video skills](#other-video-skills).
- 9 tasks × 3 runs = 27 runs per condition, 8 conditions, 216 runs in all.
- Cost is `total_cost_usd` as Claude Code or the Grok CLI reports it: tokens priced at API rates. Time is the run duration Claude Code reports; for Grok it is the wall-clock time of the run.
- Overall figures are means over each condition's 27 runs; per-task figures are medians over 3 runs, with the lowest of the 3 as the worst run.

## Fairness measures

- Same prompt and model in every condition of a model; effort high for Claude, xhigh for Grok.
- No MCP servers in any run.
- One run at a time, never in parallel.
- The model alone loads no skills and cannot read the skill's folder, symlink or cache. This was added after an early baseline run found video-lens on disk and ran it.
- For Grok, "no skills" needs more than hiding video-lens: Grok also loads Claude plugins, `~/.claude/skills`, `~/.agents/skills` and its own skills. A first Grok baseline pass that hid only video-lens used /watch in 8 of 27 runs, so it was discarded and rerun with all of them turned off; the first line of every rerun log lists no skill.
- /watch runs wrote their frames to a fixed folder they chose; it is cleared before each /watch run. video-use is loaded only for its own runs (`--add-dir`) and never installed globally.
- Every run starts in an empty folder holding only the clip, and every skill run with an empty analysis cache.
- Every run was checked for access to the answer keys; none was flagged. Three baseline runs (one Sonnet, two Grok) reused a whisper model file that an earlier Opus run had downloaded into its own folder; no run read another run's answer.

## Scoring and tolerances

The answer is the JSON block at the end of each run. A script compares it with the key item by item. A run's score is the checks passed minus false positives, divided by the number of checks, never below 0. A false positive is a reported motion item or cut with no true counterpart, for example a distractor reported as intended motion.

| Item | Tolerance |
|---|---|
| Animation start | ±1 frame |
| Animation duration | ±1 frame of the CSS duration, or between the last visible change minus 1 frame and the CSS duration |
| Easing | all four control points within 0.1, or the curve within 0.05 of the true curve at every sampled point |
| Stagger delay | ±1 frame plus 1.5 ms |
| Sound time and sound-to-motion offset | ±10 ms |
| Slide cut | ±1 frame |
| Speech sentence start and end | ±150 ms each |

The full rules, including half points for near-miss text, are in [bench/README.md, Score](../bench/README.md#4-score).

## Per-task results

<!-- per-task:start -->
<!-- Generated from docs/data/benchmark.json by tools/render_results.py. Do not edit by hand. -->

### Claude Opus 5.5

| Task | Condition | Median score | Worst run | Median cost | Median time |
| --- | --- | ---: | ---: | ---: | ---: |
| Card stagger (3 cards, 60 fps, named curve) | Model alone | 1.000 | 1.000 | $0.758 | 316.1 s |
|  | With video-lens | 1.000 | 1.000 | $0.544 | 453.2 s |
| Modal enter and exit (VFR recording) | Model alone | 1.000 | 0.875 | $0.835 | 353.2 s |
|  | With video-lens | 1.000 | 1.000 | $0.795 | 302.4 s |
| Off-screen toast with a click sound | Model alone | 1.000 | 1.000 | $0.640 | 131.4 s |
|  | With video-lens | 1.000 | 1.000 | $0.510 | 63.2 s |
| Overlapping 6-row list, custom curve, 30 fps, Retina | Model alone | 1.000 | 0.167 | $1.450 | 732.7 s |
|  | With video-lens | 0.967 | 0.933 | $0.702 | 193.9 s |
| Colour change, overshoot badge, bottom drawer (VFR) | Model alone | 1.000 | 0.833 | $1.097 | 735.5 s |
|  | With video-lens | 0.917 | 0.917 | $0.980 | 258.7 s |
| Real 3D card carousel recording, 16.5 s (approximate answer key) | Model alone | 1.000 | 1.000 | $1.310 | 486.5 s |
|  | With video-lens | 0.800 | 0.800 | $0.571 | 102.5 s |
| 20-second Korean lecture, 3 slides | Model alone | 1.000 | 1.000 | $0.431 | 191.2 s |
|  | With video-lens | 1.000 | 0.975 | $0.515 | 54.2 s |
| 10-minute Korean lecture, 24 slides | Model alone | 0.922 | 0.910 | $1.170 | 607.9 s |
|  | With video-lens | 0.922 | 0.904 | $0.817 | 158.4 s |
| Two-sentence summary of a clip | Model alone | 1.000 | 1.000 | $0.327 | 25.5 s |
|  | With video-lens | 1.000 | 1.000 | $0.419 | 28.5 s |

### Claude Sonnet 5.5

| Task | Condition | Median score | Worst run | Median cost | Median time |
| --- | --- | ---: | ---: | ---: | ---: |
| Card stagger (3 cards, 60 fps, named curve) | Model alone | 1.000 | 1.000 | $0.610 | 200.5 s |
|  | With video-lens | 1.000 | 1.000 | $0.358 | 58.2 s |
| Modal enter and exit (VFR recording) | Model alone | 1.000 | 0.875 | $0.778 | 213.5 s |
|  | With video-lens | 1.000 | 0.625 | $0.489 | 284.2 s |
| Off-screen toast with a click sound | Model alone | 1.000 | 1.000 | $0.416 | 166.2 s |
|  | With video-lens | 1.000 | 1.000 | $0.274 | 56.2 s |
| Overlapping 6-row list, custom curve, 30 fps, Retina | Model alone | 1.000 | 0.600 | $0.949 | 418.0 s |
|  | With video-lens | 0.967 | 0.800 | $0.419 | 166.7 s |
| Colour change, overshoot badge, bottom drawer (VFR) | Model alone | 1.000 | 1.000 | $0.900 | 259.9 s |
|  | With video-lens | 1.000 | 0.833 | $0.711 | 171.0 s |
| Real 3D card carousel recording, 16.5 s (approximate answer key) | Model alone | 1.000 | 1.000 | $0.731 | 213.1 s |
|  | With video-lens | 0.800 | 0.800 | $0.360 | 93.7 s |
| 20-second Korean lecture, 3 slides | Model alone | 1.000 | 1.000 | $0.405 | 410.8 s |
|  | With video-lens | 0.975 | 0.925 | $0.252 | 35.4 s |
| 10-minute Korean lecture, 24 slides | Model alone | 0.910 | 0.892 | $0.901 | 533.8 s |
|  | With video-lens | 0.895 | 0.883 | $0.505 | 124.4 s |
| Two-sentence summary of a clip | Model alone | 1.000 | 1.000 | $0.176 | 29.6 s |
|  | With video-lens | 1.000 | 1.000 | $0.200 | 16.9 s |

### Grok 4.7

| Task | Condition | Median score | Worst run | Median cost | Median time |
| --- | --- | ---: | ---: | ---: | ---: |
| Card stagger (3 cards, 60 fps, named curve) | Model alone | 1.000 | 0.800 | $0.402 | 793.0 s |
|  | With video-lens | 1.000 | 1.000 | $0.271 | 397.0 s |
| Modal enter and exit (VFR recording) | Model alone | 0.875 | 0.000 | $0.448 | 885.0 s |
|  | With video-lens | 1.000 | 1.000 | $0.393 | 949.0 s |
| Off-screen toast with a click sound | Model alone | 0.833 | 0.667 | $0.373 | 750.0 s |
|  | With video-lens | 1.000 | 1.000 | $0.239 | 401.0 s |
| Overlapping 6-row list, custom curve, 30 fps, Retina | Model alone | 0.600 | 0.200 | $0.577 | 899.0 s |
|  | With video-lens | 0.967 | 0.933 | $0.507 | 924.0 s |
| Colour change, overshoot badge, bottom drawer (VFR) | Model alone | 0.917 | 0.000 | $0.341 | 655.0 s |
|  | With video-lens | 0.833 | 0.667 | $0.299 | 558.0 s |
| Real 3D card carousel recording, 16.5 s (approximate answer key) | Model alone | 1.000 | 1.000 | $0.664 | 992.0 s |
|  | With video-lens | 0.800 | 0.800 | $0.245 | 422.0 s |
| 20-second Korean lecture, 3 slides | Model alone | 1.000 | 1.000 | $0.249 | 458.0 s |
|  | With video-lens | 0.975 | 0.975 | $0.310 | 288.0 s |
| 10-minute Korean lecture, 24 slides | Model alone | 0.850 | 0.838 | $0.815 | 1559.0 s |
|  | With video-lens | 0.898 | 0.898 | $0.582 | 746.0 s |
| Two-sentence summary of a clip | Model alone | 1.000 | 1.000 | $0.076 | 97.0 s |
|  | With video-lens | 1.000 | 1.000 | $0.072 | 77.0 s |

Median of 3 runs per cell; worst run is the lowest of the 3.

<!-- per-task:end -->

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/charts/tasks-dark.png">
  <img alt="Bar chart: median run time per task for Claude Opus 5.5, model alone and with video-lens. Values are in the table above." src="assets/charts/tasks-light.png" width="800">
</picture>

## Other video skills

The same 9 tasks and prompts with Claude Opus 5.5 using [/watch](https://github.com/bradautomates/claude-video) or [video-use](https://github.com/browser-use/video-use) instead of video-lens. The prompt starts with `/watch:watch` or `/video-use`, video-lens is locked on disk as for the model alone, and video-use is loaded only for its own runs (`--add-dir`), never installed globally.

<!-- skills:start -->
<!-- Generated from docs/data/benchmark.json by tools/render_results.py. Do not edit by hand. -->

Claude Opus 5.5 with each video skill on the same 9 tasks and prompts, 27 runs per condition, one run at a time. Each run named its skill in the prompt, and video-lens was hidden from the runs of the other skills.

| Condition | Mean score | Worst run | Runs scoring 1.0 | Avg cost per run | Avg time per run | Avg turns | Runs with own analysis code |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Model alone | 0.949 | 0.167 | 21/27 | $0.859 | 420.2 s | 17.5 | 26/27 |
| With video-lens | 0.956 | 0.800 | 15/27 | $0.663 | 265.1 s | 10.1 | 2/27 |
| With /watch | 0.956 | 0.467 | 22/27 | $1.198 | 505.0 s | 22.3 | 23/27 |
| With video-use | 0.969 | 0.467 | 23/27 | $1.569 | 487.2 s | 22.1 | 27/27 |

- /watch (claude-video 0.3.2) picks frames at scene changes, at most 100, and without captions transcribes with WhisperX on the Mac or the Groq or OpenAI Whisper API. These runs used Groq, the key already set up.
- video-use (browser-use/video-use b877063) is built to edit videos, not to measure them. It sends speech to ElevenLabs Scribe and looks at filmstrips of frames. These tasks test only how well it reads a video.
- Own analysis code: runs in which Opus also wrote and ran its own ffmpeg, OpenCV or whisper commands besides the skill's tools. Most /watch and video-use runs did, which is where their extra cost and time went. With video-lens the skill's measurements were usually enough.
- Cost counts only what Claude Code reports for the model. The speech APIs that /watch (Groq or OpenAI) and video-use (ElevenLabs) call are billed to their own keys and are not included.

<!-- skills:end -->

<!-- skills-per-task:start -->
<!-- Generated from docs/data/benchmark.json by tools/render_results.py. Do not edit by hand. -->

### Claude Opus 5.5 with other video skills

| Task | Condition | Median score | Worst run | Median cost | Median time |
| --- | --- | ---: | ---: | ---: | ---: |
| Card stagger (3 cards, 60 fps, named curve) | Model alone | 1.000 | 1.000 | $0.758 | 316.1 s |
|  | With video-lens | 1.000 | 1.000 | $0.544 | 453.2 s |
|  | With /watch | 1.000 | 1.000 | $1.216 | 603.2 s |
|  | With video-use | 1.000 | 1.000 | $1.390 | 546.3 s |
| Modal enter and exit (VFR recording) | Model alone | 1.000 | 0.875 | $0.835 | 353.2 s |
|  | With video-lens | 1.000 | 1.000 | $0.795 | 302.4 s |
|  | With /watch | 1.000 | 0.625 | $1.173 | 944.1 s |
|  | With video-use | 1.000 | 1.000 | $1.452 | 614.7 s |
| Off-screen toast with a click sound | Model alone | 1.000 | 1.000 | $0.640 | 131.4 s |
|  | With video-lens | 1.000 | 1.000 | $0.510 | 63.2 s |
|  | With /watch | 1.000 | 1.000 | $1.151 | 303.4 s |
|  | With video-use | 1.000 | 1.000 | $1.337 | 325.7 s |
| Overlapping 6-row list, custom curve, 30 fps, Retina | Model alone | 1.000 | 0.167 | $1.450 | 732.7 s |
|  | With video-lens | 0.967 | 0.933 | $0.702 | 193.9 s |
|  | With /watch | 1.000 | 0.467 | $1.364 | 666.3 s |
|  | With video-use | 1.000 | 0.467 | $1.927 | 463.0 s |
| Colour change, overshoot badge, bottom drawer (VFR) | Model alone | 1.000 | 0.833 | $1.097 | 735.5 s |
|  | With video-lens | 0.917 | 0.917 | $0.980 | 258.7 s |
|  | With /watch | 1.000 | 1.000 | $1.188 | 646.1 s |
|  | With video-use | 1.000 | 1.000 | $1.796 | 518.8 s |
| Real 3D card carousel recording, 16.5 s (approximate answer key) | Model alone | 1.000 | 1.000 | $1.310 | 486.5 s |
|  | With video-lens | 0.800 | 0.800 | $0.571 | 102.5 s |
|  | With /watch | 1.000 | 1.000 | $2.086 | 727.4 s |
|  | With video-use | 1.000 | 1.000 | $3.306 | 969.5 s |
| 20-second Korean lecture, 3 slides | Model alone | 1.000 | 1.000 | $0.431 | 191.2 s |
|  | With video-lens | 1.000 | 0.975 | $0.515 | 54.2 s |
|  | With /watch | 1.000 | 1.000 | $0.669 | 78.9 s |
|  | With video-use | 1.000 | 1.000 | $0.834 | 113.2 s |
| 10-minute Korean lecture, 24 slides | Model alone | 0.922 | 0.910 | $1.170 | 607.9 s |
|  | With video-lens | 0.922 | 0.904 | $0.817 | 158.4 s |
|  | With /watch | 0.898 | 0.898 | $1.263 | 259.2 s |
|  | With video-use | 0.898 | 0.898 | $1.276 | 243.3 s |
| Two-sentence summary of a clip | Model alone | 1.000 | 1.000 | $0.327 | 25.5 s |
|  | With video-lens | 1.000 | 1.000 | $0.419 | 28.5 s |
|  | With /watch | 1.000 | 1.000 | $0.506 | 34.9 s |
|  | With video-use | 1.000 | 1.000 | $0.694 | 54.8 s |

Median of 3 runs per cell; worst run is the lowest of the 3.

<!-- skills-per-task:end -->

## Caveats

- **Small sample.** 3 runs per task and condition on one Mac (macOS 26, Apple Silicon). Medians of 3 are coarse, which is why the worst run is shown next to them.
- **The model alone is strong on these tasks.** Mean scores are close in both conditions and can go either way; read the worst run and the per-task table before drawing conclusions about accuracy.
- **Synthetic answer keys.** Clean CSS renders and text-to-speech narration. Photo or gradient backgrounds, many elements moving at once, and noisy real speech are under-tested. Only `orbit` is a real recording, and its key is approximate, so its score difference says little about accuracy.
- **Short tasks can cost more with the skill.** Loading the skill has a fixed cost that a short task does not win back.
- **Costs are API-equivalent.** The runs used Claude and Grok subscriptions; nothing was billed per token. video-use's ElevenLabs speech-to-text ran on free-plan credits and is not in the cost figures.
- **Grok's times are rough.** The Grok runs with video-lens shared the Mac with other heavy work for part of the pass, and one of them took 4.3 hours (27 turns, $0.46). Their mean time is 1,055.9 s against 785.0 s alone; the medians are 446 s and 778 s.
- **The other video skills mostly measured with Opus's own code.** With /watch and with video-use, Opus also ran its own ffmpeg, OpenCV or whisper commands in 24 and 27 of 27 runs (26 alone, 2 with video-lens), so their scores reflect that combined work and its cost.
- **The skill runs name the skill.** How often it triggers without being named was measured separately: 8 of 9 tasks on Opus 5.5.
- **The author's setup was loaded.** Apart from MCP servers, the Claude runs used the author's user-level configuration and plugins, and the Grok runs read the same `CLAUDE.md` instructions. A different setup will shift the numbers.

## Reproduce

- Run the benchmark: [bench/README.md](../bench/README.md) (clip generators, `run_one.sh`, `run_final.sh`, `score.py`).
- The published numbers live in [`data/benchmark.json`](data/benchmark.json). After changing it, `python3 tools/render_results.py` rewrites the generated tables in this file and in every README, and `node tools/render_charts.mjs` re-renders the chart images.
