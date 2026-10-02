# Use case: rebuilding viral showreels

The scripts behind the "Use case" section of the [project page](https://junhan2.github.io/video-lens/#cases). The numbers it shows are in [`docs/data/cases.json`](../../docs/data/cases.json).

The question: given only a video someone posted, can Claude with video-lens work out how it moves and rebuild it, and how close does the rebuild come? The reels are the 15-second "motion designer showreel" posts from the launch week of Claude Opus 5.5 (September 2026), picked by likes from [Revid's gallery](https://www.revid.ai/claude-motion-graphics), plus GPT-6 Astra's reel for the same prompt from a side-by-side post. `sources.tsv` lists them.

## Steps

| Script | What it does |
|---|---|
| `fetch.sh` | Downloads the originals from X with yt-dlp into `src/` and cuts GPT-6 Astra's half out of the side-by-side post. |
| `run_case.sh NAME SECONDS [lens\|base]` | One run: an empty folder with only `clip.mp4`, Claude Opus 5.5 in headless Claude Code (`claude -p`, no MCP servers). `lens` names `/video-lens` in the prompt; `base` is the same prompt without the skill, with no skills loaded (`--disable-slash-commands`), video-lens hidden by `../hide_skill.sh hide`, and a working folder in `/private/tmp` where the other rebuilds cannot be seen. The prompt asks for one `recreation.html` animated only with CSS animations or the Web Animations API, so every frame can be rendered exactly, and allows two rounds of fixes after comparing with the original. Nobody touches the run. Each `base` log was checked for any mention of video-lens, `vl.py` or `declared.mjs`: none. |
| `finish_case.py NAME` (or `NAME-base`) | Renders the final `recreation.html` again with the skill's `declared.mjs` (1920x1080, 60 fps, the length in `sources.tsv`), scores it with `compare.py`, scores the original against another reel (the `floor` column) for the floor, and writes the web copies (1280x720 video, poster, the page itself). |
| `build_cases.py` | Writes `docs/data/cases.json` from the runs' `summary.json` and `sources.tsv` (author, link, likes and views on September 28, 2026), and copies the web files into `docs/`. |
| `compare.py ORIGINAL REBUILD` | The scores. **Looks alike**: SSIM of the two frames at the same moment (grayscale, 192x108), 10 samples a second. **Cuts**: ffmpeg scene score above 0.3 in each clip, matched one to one within 0.1 s. **Words**: distinct words the video-lens OCR reads in the original (letters only, 3 or more, in the macOS word list so misreads drop out) that it reads again in the rebuild. |

Checks of the scoring itself: an original against itself scores 1.000 SSIM, 9 of 9 cuts and 39 of 39 words; two different reels score 0.344, 3 of 9 and 10 of 39.

Not included: the original videos (they stay on X), the run folders and logs. The rebuilt pages are published under [`docs/cases/`](../../docs/cases/).
