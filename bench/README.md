# video-lens benchmark

This folder holds everything needed to rerun the benchmark behind the numbers in [`docs/data/benchmark.json`](../docs/data/benchmark.json): the tasks and prompts, the clip generators and their answer keys, the run and scoring scripts, and the per-run results.

The question it answers: given the same video question, how do accuracy, cost and time compare between a model working alone (writing its own ffmpeg and Python) and the same model using video-lens?

Published runs: 2026-09-29 to 2026-10-01, 9 tasks x 3 runs = 27 runs per condition, 8 conditions, 216 runs: Claude Opus 5.5 (alone, with video-lens, with /watch, with video-use) and Claude Sonnet 5.5 (alone, with video-lens) in Claude Code 2.1.284 and 2.1.285, Grok 4.7 (alone, with video-lens) in the Grok Build CLI 1.0.40; video-lens 1.0.0 (pre-release build); macOS 26 on Apple Silicon.

## Contents

| Path | What it is |
|---|---|
| `tasks.json` | The 9 tasks: clip path and prompt, plus the answer suffix and JSON schema appended to every prompt. |
| `evalset/gen.py` | Builds clips v1 to v4 and writes `truth.json` and `truth_frames.json` (per-frame values). |
| `hardset/gen_hard.py` | Builds clips h1 to h3 and writes `truth_hard.json`. |
| `clips.sha256` | SHA-256 of the seven clips the published runs used. |
| `run_one.sh` | Runs one cell (task, arm, repetition) with `claude -p`. |
| `run_final.sh` | Runs all cells of one group (`opus`, `sonnet`, `grok`, `skills`), one at a time: the video-lens arm first, then the arms that must not see video-lens, with it hidden. |
| `final_*.txt` | Cell lists, one `task arm rep` per line, one file per arm. |
| `hide_skill.sh` | Locks the skill folder, its symlink and its cache for the baseline arm, and restores them. |
| `paths.sh` | Paths shared by the scripts, each overridable by an environment variable. |
| `score.py` | Scores every run folder against the answer keys and prints a per-cell summary. |
| `build_benchmark.py` | Writes the scored runs of `results.csv` into `docs/data/benchmark.json` (overall means, per-task medians); `--check` only reports what would change. |
| `results.csv` | The 216 published runs (8 conditions x 27), one row per run. |

Not included: the video files (the generators rebuild them), the carousel recording used by the `orbit` and `gist` tasks (a private file), and the run logs.

## Conditions

| Arm code | Model | Condition |
|---|---|---|
| `a0f` | Opus 5.5 | Baseline: the model alone. No skills load (`--disable-slash-commands`) and video-lens is locked on disk. |
| `bf` | Opus 5.5 | Skill: the prompt starts with `/video-lens`. |
| `a0s` | Sonnet 5.5 | Baseline, as `a0f`. |
| `bs` | Sonnet 5.5 | Skill, as `bf`. |
| `g0` | Grok 4.7 | Baseline, as `a0f`, run in the Grok Build CLI (`--model grok-4.7 --reasoning-effort xhigh`). |
| `gs` | Grok 4.7 | Skill, as `bf`, in the Grok Build CLI. |
| `w` | Opus 5.5 | [/watch](https://github.com/bradautomates/claude-video) 0.3.2: the prompt starts with `/watch:watch`; video-lens is locked on disk. |
| `vu` | Opus 5.5 | [video-use](https://github.com/browser-use/video-use): the prompt starts with `/video-use`, the skill is loaded for this run only (`--add-dir`); video-lens is locked on disk. |

`run_one.sh` also accepts `b` (skills available but not named, Opus 5.5), which measured whether the skill triggers by itself: it did in 8 of 9 tasks. Those runs are not in `results.csv`. The codes `a0` and `bstar*` are earlier rounds' names for the `a0f` and `bf` conditions.

## Tasks

| Task | Clip | Answer key | Checks |
|---|---|---|---|
| `v1` | 3 s, 60 fps, 1280x800. Three cards fade in and rise 40 px, 400 ms each, staggered 100 ms, named curve. A blinking caret is a distractor. | CSS as written | 15 |
| `v2` | 3 s, variable frame rate. A modal scales and fades in (240 ms, ease-out) and out (160 ms, ease-in). | CSS as written | 8 |
| `v3` | 3 s, 60 fps. A toast slides in from off screen (300 ms, ease-out); a click sound plays 30 ms after the slide starts. A spinning loader is a distractor. | CSS and audio as written | 6 |
| `h1` | 3 s, 30 fps, 2560x1600 file of a 1280x800 page (Retina). Six rows enter 45 ms apart and overlap heavily, custom cubic-bezier. A shimmer loop is a distractor. | CSS as written | 30 |
| `h2` | 3.2 s, variable frame rate. A colour-only change with no brightness change, a badge that overshoots scale 1, a bottom drawer. | CSS as written | 12 |
| `orbit` | Real 16.5 s recording of a 3D card carousel. | Approximate, read by hand | 5 |
| `v4` | 20 s Korean lecture, 3 slides, 30 fps, 1280x720. | Slide text, cut times, macOS text-to-speech narration | 20 |
| `h3` | 10 min Korean lecture, 24 slides, irregular cuts, long silences. | Same as v4 | 167 |
| `gist` | The carousel recording again. | Keyword rubric for a two-sentence summary | 4 |

The prompts are in Korean, exactly as they were run. Every prompt ends with the same instruction to answer without asking questions and to append one `json` block that follows `answer_schema` in `tasks.json`.

## Requirements

- macOS with `say` and the Korean voice Yuna (`say -v '?' | grep Yuna` should print a line).
- Google Chrome at `/Applications/Google Chrome.app` (the generators drive it headless over the DevTools pipe).
- ffmpeg and ffprobe, Python 3 with numpy and opencv-python. `swiftc` (Xcode Command Line Tools) is optional; the generators use it for an extra OCR check of the slides.
- Claude Code (`claude`) signed in, and video-lens with its own requirements (see the main [README](../README.md)).
- For `g0` and `gs`: the Grok Build CLI signed in.
- For `w`: the /watch plugin (claude-video 0.3.2) with a Groq key set up for transcription (it can also transcribe locally with WhisperX, not installed here).
- For `vu`: a clone of video-use with its Python packages (`uv sync`) and `ELEVENLABS_API_KEY` in its `.env`.

Tested with macOS 26.3, Python 3.13, ffmpeg 8.1, Chrome 153 and 154.

## 1. Rebuild the clips

```
python3 evalset/gen.py         # v1 to v4, about 1 minute
python3 hardset/gen_hard.py    # h1 to h3, about 2.5 minutes
shasum -a 256 -c clips.sha256  # compare with the clips the published runs used
```

Both generators render real CSS animations in headless Chrome one frame at a time (every animation paused and seeked to the frame's time, then one screenshot), encode with ffmpeg, check the result numerically and rewrite their truth files. Scratch files go to `evalset/_work` and `hardset/_work`.

Rebuilt on 2026-09-30 with Chrome 154, all seven clips matched `clips.sha256` byte for byte, and the truth files differed from the published ones only in the recorded Chrome version.

For `orbit` and `gist`, bring your own real carousel recording, approximate key: put the clip at `byo/carousel.mp4`, then replace `ORBIT_STARTS` (and the count and period checks in `score_orbit`) and the `score_gist` keyword list in `score.py` with values for your clip. Without it those 6 cells per arm are skipped.

## 2. Set up

The baseline arm must not be able to find video-lens anywhere on disk, so the skill has to live in a folder the scripts can lock, and the bench must run from outside that folder. This is the setup the published runs used:

```
git clone https://github.com/Junhan2/video-lens.git ~/video-lens
ln -s ~/video-lens/skills/video-lens ~/.claude/skills/video-lens
cp -R ~/video-lens/bench ~/video-lens-bench
cd ~/video-lens-bench
export VIDEO_LENS_SKILL_DIR=~/video-lens
```

If you also installed video-lens from the plugin marketplace, remove or disable that plugin while benchmarking, so the checkout is the only copy.

| Variable | Default | Meaning |
|---|---|---|
| `VIDEO_LENS_SKILL_DIR` | none, required for the baseline | Folder holding the skill's files. `hide_skill.sh hide` sets it to mode 000. |
| `VIDEO_LENS_SKILL_LINK` | `~/.claude/skills/video-lens` | Symlink Claude Code loads the skill from. Moved aside while hidden. |
| `VLB_PARKED_LINK` | `~/.vlb-hidden-skill-link` | Where that symlink waits while hidden. |
| `VIDEO_LENS_USER_CACHE` | `~/.cache/video-lens` | The skill's shared cache (compiled Swift helpers). Also locked. |
| `VLB_RUNS` | `~/vlb-runs` | One folder per run: `work/clip.mp4`, `run.jsonl`, `stderr.txt`, `wall.json`, `vlcache/`. |
| `VIDEO_USE_DIR` | `~/Developer/video-use` | Your video-use clone, for the `vu` arm. |
| `GROK_BIN` | `~/.grok/bin/grok` | The Grok Build CLI, for the `g0` and `gs` arms. |

`hide_skill.sh` refuses to hide a folder that contains the bench. `run_one.sh` refuses to start a baseline run while the symlink exists or the skill folder or cache is readable.

## 3. Run

```
./run_final.sh opus      # 27 skill cells, then 27 baseline cells
./run_final.sh sonnet
./run_final.sh grok
./run_final.sh skills    # 27 /watch cells, then 27 video-use cells
./run_one.sh v3 bf 1     # a single cell
```

Each cell does the following:

1. Makes a fresh folder `$VLB_RUNS/<task>-<arm>-<rep>/work`, copies the clip in as `clip.mp4` and runs from there, so no answer key is reachable by a relative path.
2. Sets `CLAUDE_CODE_EFFORT_LEVEL=high` and points `VIDEO_LENS_CACHE_DIR` at an empty folder for this run only.
3. Runs:

```
claude -p "<prompt>" --model <claude-opus-5-5 | claude-sonnet-5-5> \
  --output-format stream-json --verbose --no-session-persistence \
  --permission-mode bypassPermissions --strict-mcp-config --max-budget-usd 8 \
  [--disable-slash-commands]      # baseline arms only
```

For the skill arms the prompt starts with `/video-lens `, for `w` with `/watch:watch ` and for `vu` with `/video-use `. `--strict-mcp-config` with no config file means no MCP servers. The stream is saved as `run.jsonl` and the wall-clock time as `wall.json`. A cell whose log already has a result is skipped, so an interrupted pass can be resumed.

The Grok arms run `grok --model grok-4.7 --reasoning-effort xhigh -p "<prompt>" --output-format streaming-json --disallowed-tools use_tool,search_tool` instead; the two blocked tools are its MCP gateway, to match the Claude runs.

The Grok baseline (`g0`) must load no skills, like `--disable-slash-commands` for Claude. Grok also loads Claude plugins, `~/.claude/skills`, `~/.agents/skills` and its own skills, so `run_one.sh` sets `GROK_CLAUDE_SKILLS_ENABLED=false` and writes `.grok/config.toml` into the run folder with every plugin disabled, and `run_final.sh grok` appends a `[skills]` block to `~/.grok/config.toml` for the g0 cells and restores the file afterwards (`grok_isolate.py` builds both from `grok inspect --json`). The first line of a g0 log must list no skill. An earlier g0 pass without this used /watch in 8 of 27 runs and was rerun.

`run_final.sh` locks video-lens before the cells that must not see it and restores it when it exits, whether it finishes, fails or is stopped.

The published passes took, in wall-clock time and API-equivalent cost: Opus 5.5 alone and with video-lens 5.2 hours and $41, with /watch 4.4 hours and $48, with video-use 3.7 hours and $42; Sonnet 5.5 3.1 hours and $28; Grok 4.7 13.8 hours and $21 (Grok's figure is what its CLI reports). The ElevenLabs speech-to-text that video-use called is not included.

## 4. Score

```
python3 score.py                    # per-cell summary: median score, worst run, median cost, turns, time, image reads
python3 score.py --csv mine.csv     # one row per run, same columns as results.csv
python3 score.py --detail           # every run as JSON, with a note per check
python3 build_benchmark.py --check  # what the scored runs in results.csv would change in docs/data/benchmark.json
```

`skills_touched` names the video skills whose files a run's tool calls used, and `own_analysis` marks runs that also ran their own measuring commands (OpenCV, frame differences, scene or silence filters, frame grabs, whisper) besides a skill's scripts.

The answer is the last `json` block in the run's final result. Each task yields a list of checks, and a run's score is `max(0, points - false positives) / number of checks`.

- **Motion (v1, v2, v3, h1, h2).** Each true animation is matched to the reported item starting nearest to it, within 0.1 s. Then:
  - start within 1 source frame;
  - duration within 1 frame of the CSS duration, or between the last visible change minus 1 frame and the CSS duration;
  - easing: all four control points within 0.1, or the reported curve within 0.05 of the true curve at every sampled point;
  - the right properties (translate, scale, opacity, colour and so on);
  - for staggered elements, the delay within 1 frame plus 1.5 ms.

  Every reported motion item left unmatched counts as a false positive (a distractor reported as intended motion ends up here). For v3, the sound time within 10 ms and the sound-minus-motion offset within 10 ms of 30 ms.
- **Lectures (v4, h3).** Each cut within 1 frame (false cuts subtract). Each slide's Korean title, English line and footer present in the reported text; a title within 5 % character error rate gets half a point. For each narration sentence: text (exact 1 point, within 5 % character error rate half a point), start and end each within 150 ms.
- **orbit.** Transition count 11 plus or minus 1; median interval 1.45 to 1.65 s; at least 80 % of the hand-read start times matched within 50 ms after removing a common offset; every reported easing S-shaped; horizontal direction named.
- **gist.** Four keyword checks on the summary.

`score.py` also flags two things per run. `leaked` is set when any tool call mentions the bench folder, a truth file, or another run's folder. `skill_used` is set when any tool call mentions `video-lens` or calls the Skill tool.

## results.csv

One row per published run, 216 rows, the columns `score.py --csv` writes:

| Column | Meaning |
|---|---|
| `task`, `arm`, `rep` | Cell. See the tables above for task and arm codes. |
| `status` | Result subtype from Claude Code, or from the Grok CLI's end event (`success` for all 216). |
| `score` | 0 to 1, as defined above. |
| `checks` | Points earned / number of checks, before false positives. |
| `false_pos` | Reported motion items or cuts with no true counterpart. |
| `cost_usd` | `total_cost_usd` from Claude Code: API-equivalent, see caveats. |
| `turns` | Model turns in the run. |
| `duration_s` | Run time Claude Code reports; for Grok runs the wall-clock time. |
| `wall_s` | Wall-clock time around the `claude` call. |
| `input_tokens` | Input plus cache-creation plus cache-read tokens. |
| `output_tokens` | Output tokens. |
| `tool_calls` | Tool calls made. |
| `image_reads` | Reads of PNG, JPEG or WebP files, that is frames the model looked at. |
| `skill_used` | See above. |
| `skills_touched` | Video skills whose files the run's tool calls used (`video-lens`, `watch`, `video-use`). |
| `own_analysis` | True when the run also ran its own measuring commands besides a skill's scripts. |
| `leaked` | See above (false for all 216). |
| `notes` | Per-check details from the scorer. |

The overall figures in `docs/data/benchmark.json` are means over each condition's 27 rows; the per-task figures are medians over 3 rows.

`skill_used` is true in 2 baseline rows, `h3-a0f-3` and `h3-a0s-1`. In both, the model listed `~/.cache/video-lens` while looking for speech models; that folder was locked at the time.

## Fairness measures

- Same prompt, same model, effort high, in both arms.
- No MCP servers in any run.
- One run at a time, never in parallel.
- The baseline loads no skills and cannot read the skill's folder, symlink or cache.
- Every run starts in an empty folder with only `clip.mp4`, and every skill run with an empty analysis cache.
- Every run was checked for access to the answer keys (`leaked`); none was flagged.

## Known caveats

- **The baseline once found the skill.** In an earlier round a baseline run found video-lens on disk and executed it. Since then `hide_skill.sh` locks every copy for the baseline and `run_one.sh` refuses to start while any is readable.
- **Parallel runs mixed results.** An earlier round ran cells in parallel; runs shared folders under `/tmp` and their results mixed. All published runs ran one after another.
- **Costs are API-equivalent.** The runs used a Claude subscription, not an API key. `cost_usd` is the `total_cost_usd` Claude Code reports, which prices the tokens at API rates; nothing was billed per token.
- **The author's normal Claude Code setup was loaded.** Apart from MCP servers, the runs used the author's user-level Claude Code configuration, including its plugins and a session-start hook. In the skill arms the author's other skills were available too. A different setup will shift the numbers.
- **Effort.** The published Opus runs got effort high from the author's user settings and the Sonnet runs from `CLAUDE_CODE_EFFORT_LEVEL`. The packaged `run_one.sh` sets the variable for every arm.
- **The skill arm names the skill.** `/video-lens` is in the prompt. How often the skill triggers without being named was measured separately (8 of 9 tasks, Opus 5.5).
- **Small sample.** 3 runs per task and arm on one machine.
- **Synthetic answer keys.** Clean CSS renders and text-to-speech narration. Photo or gradient backgrounds, many elements moving at once, and noisy real speech are under-tested. Only `orbit` is a real recording, and its key is approximate.
- **Fixed v3 values.** `score.py` checks the v3 sound at 1.23 s and the offset at 30 ms directly; these match what `gen.py` builds.
