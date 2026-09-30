# video-lens build interfaces (core contract for the parallel builders)

This file is the contract between core and the four module builders (motion, content, audio, declared) and the
integrator. Everything named here exists in core today and was exercised by `selftest/kat_core.py`
(C5 to C9, X1 to X4 PASS). Read this file for how the modules plug in.

## 1. Rules

- Put your code only in the files you own (section 2). Do not edit core files. If you need a core change,
  say so in your report; do not work around core with a private copy.
- Import core modules only: `vl.context`, `vl.decode`, `vl.cache`, `vl.views`, `vl.pixels`, `vl.swiftbuild`,
  `vl.errors`, `vl.probe`, `vl.cli` (tests only). Never import another builder's module. Inside `scripts/vl/` use relative
  imports (`from . import decode, views`, `from .errors import VlError, EXIT_DEPENDENCY`).
- Modules never print. stdout is only `report.md` (or `analysis.json` with `--json`); stderr stays empty on
  success (O3). Use `run.warn(msg)` for anything the user should know, `run.progress(...)` for progress.
  Python warnings are routed to `$TMPDIR/video-lens/python-warnings.log` by `vl.py`.
- User-facing failure: `raise VlError(code, "what failed", "what to do")` (`vl/errors.py`): 3 input unreadable,
  4 empty range, 5 missing dependency or helper build failed. ASR problems are warnings, never exits.
- Times: seconds on the ffmpeg timeline (0 = `format.start_time`). Round motion times with
  `context.motion_time` (4 decimals), content times with `context.content_time` (3). `context.clock(t)`
  gives `03:12.4`; `context.parse_time(text)` parses `12.5` or `[hh:]mm:ss[.s]`.
- File paths written into `analysis.json` are relative to OUT (`"motion/M02_sheet.png"`,
  `"keyframes/K001.jpg"`, `"transcript.txt"`). Only `input.path` and `cache.dir` are absolute.
- No em dashes in any text you write (user rule). Hershey labels are ASCII only (`views.ascii_label`).

## 2. File ownership

| Owner | Files |
|---|---|
| core (done) | `scripts/vl.py`, `scripts/vl/{__init__,errors,cli,context,probe,decode,cache,views,pixels,swiftbuild}.py`, `selftest/{kat.py,katlib.py,kat_core.py,fixtures_core.py}`, this file |
| motion | `vl/easing.py`, `vl/detect.py`, `vl/elements.py`, `vl/track.py`, `vl/css.py`, `vl/sheets_motion.py`, `reference/easings.json`, `selftest/kat_motion.py`, `selftest/fixtures_motion.py` |
| content | `vl/survey.py`, `vl/ocr.py`, `swift/ocr.swift`, `swift/render.swift`, `vl/sheets_content.py`, `selftest/kat_content.py`, `selftest/fixtures_content.py` |
| audio | `vl/audio.py`, `vl/speech.py`, `vl/sync.py`, `swift/transcribe.swift`, `selftest/kat_audio.py`, `selftest/fixtures_audio.py` |
| declared | `scripts/declared.mjs`, `selftest/kat_declared.py` |
| integrator (later) | `vl/report.py`, `vl/sheets.py`, `SKILL.md`, `reference/fields.md`, wiring in `scripts/vl.py`, `selftest/kat_output.py` (O1, O2, O4, O5) |

## 3. The Run object (`vl/context.py`)

`vl.py analyze` builds one `Run` and passes it to every stage. Build the same object in tests with
`vl.cli.prepare_run(["analyze", video, "--out", out_dir, *flags])`: it parses with the real parser (same
defaults), probes (cached), opens the cache, creates OUT and resolves the range and params, exactly as
`analyze` does before its stages. It does not run any stage.

| Attribute / method | Type | Meaning |
|---|---|---|
| `run.args` | argparse.Namespace | raw CLI args (prefer `run.params`, where every `auto` is resolved) |
| `run.input_path` | Path | absolute input file (a URL is already downloaded) |
| `run.url` | str or None | the URL the user gave |
| `run.probe` | dict | `probe.probe()` result, see section 4 |
| `run.packet_times` | np.ndarray float64 | sorted frame times of the whole file (timeline seconds) |
| `run.cache` | `cache.Cache` | this input's cache folder, section 6 |
| `run.out` | Path | OUT folder (absolute, exists) |
| `run.range` | `context.Range` | `start_s`, `end_s`, `is_full`, `duration_s`, `contains(t)`; `[start_s, end_s)` |
| `run.params` | dict | resolved params, section 5 (also `analysis.json.params`) |
| `run.analysis` | dict | the `analysis.json` document under construction (spec 8 keys, sections start as None) |
| `run.warnings` | list[str] | same list as `analysis["warnings"]`; append with `run.warn(msg)` (deduplicated) |
| `run.views` | list[dict] | same list as `analysis["views"]`; filled by `views.write_view` |
| `run.timing` | dict | same dict as `analysis["timing_s"]`; add with `with run.timer("key"):` |
| `run.visual_onsets` | list[(float, str)] | `(t, kind)` appended by content and motion, read by sync (section 7) |
| `run.frame_interval_s` | float or None | median frame interval (`median_dt_ms / 1000`) |
| `run.display_size` | (int, int) | display width, height (rotation applied) |
| `run.warn(msg)` | | add a warning once |
| `run.timer(key)` | context manager | adds wall seconds to `timing_s[key]` |
| `run.progress(stage, done, total)` | | writes `OUT/status.json` (throttled); stderr too with `--progress` |
| `run.check_frame_count(n)` | | called for you by full-range `decode.stream_run`; sets `video.pts_count_check` |
| `run.record_cache()` | | fills `analysis["cache"]` from the hits and misses so far (the report reads it) |
| `run.snap_time(t)` | float | time of the frame on screen at t (last frame <= t + 0.5 ms); exit 4 outside the video |
| `run.out_path(rel)` | Path | `OUT/rel` with parent folders created |
| `run.rel(path)` | str | POSIX path relative to OUT |

View commands (`zoom`, `frame`, `text`, `rows`) get `Run.open_from_out(out, args)`: the same object with
`run.analysis` loaded from `OUT/analysis.json` (sections filled), probe from the cache, `run.range` from the
analysis. It never re-runs analysis; `run.views` changes there are not saved.

## 4. Probe result (`run.probe`)

```
{"path", "size_bytes", "duration_s", "start_time_s",
 "video": {"index", "codec", "width", "height", "display_width", "display_height", "rotation",
           "duration_s", "start_time_s", "video_start_offset_s", "frames", "avg_fps", "is_vfr",
           "median_dt_ms", "min_dt_ms", "max_dt_ms", "pts_source", "pts_count_check"},
 "audio_stream": null | {"index", "codec", "sample_rate", "channels", "lang", "start_time_s"},
 "subtitle_streams": [{"index", "lang", "codec"}],
 "sidecar_subtitles": ["/abs/<stem>.srt", "/abs/<stem>.vtt"]}
```
`video.index` and `audio_stream.index` are absolute stream indexes: map them as `-map 0:<index>` (cover art
is skipped). `audio_stream.start_time_s` is relative to `format.start_time`. `frames` counts packets not
flagged discard. `run.analysis["video"]` is a copy of `probe["video"]`.

## 5. Resolved params (`run.params`)

```
roi:     [x, y, w, h] display px, validated inside the frame, or None
content: pix, cut_frac, cut_ratio, key_frac, key_low, ocr_frac,
         cell_s (0.5; 1.0 when the range > 30 min), fast (bool; auto = range > 20 min),
         ocr (bool; set by run.set_mode before your stage: auto = on in content/both),
         ocr_langs (list), ocr_max, jobs, max_keyframes (int; auto = min(150, 12 + int(2 x minutes))),
         keyframe_cap (int; auto = min(600, 12 + int(12 x minutes)); = max_keyframes when that is given),
         timeline_chars
motion:  det_side, work_side (0 = native), pix_threshold, gap_ms, noise_maxd, joint (bool), dpr,
         motion_mode, max_elements, micro (bool), sheets,
         events: None | [{"id": "M01"} | {"range": [a, b]}], easings: abs path or None
speech:  asr, lang ("none" = off), whisper_model (abs path: flag > $VIDEO_LENS_WHISPER_MODEL > largest
         ggml-*.bin in ~/.local/share/whisper without "for-tests"; None when none). Refusing a for-tests
         model is speech.py's job.
report:  budget_chars
```
`run.analysis["mode"]` (`content|motion|both`) is set before any stage after audio.

## 6. Cache (`vl/cache.py`)

```python
entry = run.cache.entry("survey", params, "npz")   # file <stage>.<sha1(params)[:8]>.v1.<ext> in the key folder
if entry.is_hit:
    data = entry.load_npz()             # also load_json(), read_jsonl()
else:
    ...compute...
    entry.save_npz(times=..., frac=...) # also save_json(obj), save_jsonl(rows); all atomic (temp + rename)
```
- Put in `params` everything that changes the result, including `[run.range.start_s, run.range.end_s]` and
  `run.params["roi"]`. Creating the entry records the stage name as a hit or a miss for `analysis.cache`
  (O4 reads it). `--refresh` makes every entry a miss.
- Stage names used in `analysis.cache`: `audio`, `speech`, `survey`, `ocr`, `detect`, `motion`.
- Resumable stages (OCR, O5): `entry.read_partial()` returns rows already appended by a killed run (a torn
  last line is dropped), `entry.append_partial(rows)` appends and fsyncs, `entry.finish_partial()` promotes
  `.part` to the final file. With `--refresh` the `.part` is deleted when the entry is created.
- Other files in the key folder: `run.cache.dir / "audio16k.wav"` style names are yours to choose, but write
  them atomically (`cache.write_atomic(path, bytes)`) so a killed run never leaves half a file.
- `run.cache.frames_dir` holds `frames/<t_ms>_<w>.jpg` written by `decode.cached_frames`.
- `cache.json_default` serialises numpy scalars/arrays; `run.write_analysis()` already uses it.
- Selftest sets `VIDEO_LENS_CACHE_DIR` to its scratch folder; never hardcode `~/.cache/video-lens`.

## 7. Decode API (`vl/decode.py`)

### Streams
```python
from . import decode
stream = decode.stream_run(run, vf, pix_fmt="bgr24", windows=None, fast=False, noref=False)
for frame in stream:              # Frame(index, t, image, window)
    ...                           # frame.image is read-only uint8 HxWx3 (HxW for "gray"); copy to keep or edit
stream.times, stream.size, stream.seconds
```
- `stream_run` decodes `run.input_path`, maps `run.probe["video"]["index"]` and applies `run.range`
  (`-ss start -t duration` as INPUT options; frames with `start <= t < end`) unless you pass `start_s`/`end_s`.
- The filter chain is `[select] , vf , format=<pix_fmt> , showinfo=checksum=0`, with
  `-fps_mode passthrough -f rawvideo`. The output size is read from showinfo, so `vf` may use any scale
  expression. `pix_fmt` is `bgr24`, `rgb24` or `gray`.
- `t` = showinfo `pts_time` + S (S = the input `-ss`). C7 verified range and window times equal the full decode
  within 0.1 ms (CFR and VFR); C5 verified showinfo = cv2 on the VFR fixture and orbit (947 frames).
- `windows=[(a, b), ...]` in absolute seconds adds `select='between(t,a-S,b-S)+...'` as the first filter;
  windows are inclusive and widened by 0.1 ms; `frame.window` is the window index. Pass exact frame times
  from an earlier decode as a and b.
- `fast=True` adds `-skip_loop_filter all -flags2 fast` (content survey only). `noref=True` adds
  `-skip_frame noref` (survey with `params.content.fast`). Motion decodes use neither.
- A full-file decode (no range, no windows, no noref) calls `run.check_frame_count(n)` when it finishes; a
  mismatch adds the warning `pts_count_mismatch: ...`.
- Stopping iteration early kills ffmpeg. ffmpeg failure raises VlError(3) at the end of iteration.
- `decode.FrameStream(path, vf, pix_fmt=..., start_s=None, end_s=None, windows=None, fast=False,
  noref=False, stream_index=None)` is the same without a Run (fixtures, tests).

Examples (spec 7.5 and 7.8):
```python
width, height = run.display_size
survey = decode.stream_run(run, f"scale=160:{max(2, round(160 * height / width / 2) * 2)}:flags=area",
                           pix_fmt="rgb24", fast=True, noref=run.params["content"]["fast"])
side = run.params["motion"]["det_side"]
detect = decode.stream_run(run, f"scale=w={side}:h={side}:force_original_aspect_ratio=decrease:"
                                "force_divisible_by=2:flags=area")
work = decode.stream_run(run, f"crop={w}:{h}:{x}:{y},scale={sw}:{sh}:flags=area", windows=windows)
```

### Single frames (`-ss (t - 0.0005) -i V -frames:v 1`, 8 parallel workers)
```python
decode.export_frames(path, [decode.ExportJob(t, out_path, width=None, roi=None)], stream_index=None)
decode.read_frames(path, times, width=None, roi=None, stream_index=None) -> list[BGR ndarray]
decode.read_frame(path, t, width=None, roi=None, stream_index=None) -> BGR ndarray
decode.cached_frames(run, times, width=None) -> list[Path]   # cache frames/<t_ms>_<w>.jpg, only missing ones decoded
```
- `t` must be a frame time from a decode (or `run.snap_time(t)`); C5/C6 measured the export bit-identical to
  the streamed frame, 8/8 on VFR, and also on a file with `start_time` 5 s.
- `roi` is `(x, y, w, h)` in display px, applied before `width`; `width` only downsizes
  (`scale='min(W,iw)':-2:flags=area`). `.jpg` exports use `-q:v 2`; `.png` otherwise.
- Pass `stream_index=run.probe["video"]["index"]` when you have a Run.

### Other ffmpeg calls
`decode.run_ffmpeg(args, "what") -> stdout bytes` runs `ffmpeg -nostdin -v error ARGS` and raises VlError(3)
with ffmpeg's last line. Audio PCM example:
```python
audio = run.probe["audio_stream"]
pre = [] if run.range.is_full else ["-ss", f"{run.range.start_s:.6f}", "-t", f"{run.range.duration_s:.6f}"]
pcm = decode.run_ffmpeg(pre + ["-i", str(run.input_path), "-map", f"0:{audio['index']}", "-ac", "1",
                              "-ar", "16000", "-f", "f32le", "-"], "audio decode")
```
Audio timeline rule (measured today with the audio starting 0.1 s after the video, mov/pcm and mp4/aac,
within 1 ms): sample k of such a decode is at `t = max(S, audio_stream.start_time_s) + k / rate`, where S is
the `-ss` (0 without one).

## 8. Stage functions (called by `vl.py analyze`)

Order in `vl.py:run_stages`: audio, mode selection, speech, content, motion, sync, views; then
`report.render_report`, `run.write_analysis()`, `report.md`, stdout. A missing module is exit 5, so each
module must exist before `analyze` works end to end; test your stage directly on a `prepare_run` Run.

| Called as | When | Returns (analysis.json section, spec 8) | Side effects |
|---|---|---|---|
| `audio.analyze_audio(run)` | audio stream exists (all modes) | `audio` dict; `activity_ratio` is over `run.range` and drives auto mode (< 0.2 and <= 120 s: motion) | cache `audio`; may write the 16 kHz wav for ASR in the cache folder |
| `speech.analyze_speech(run)` | mode content/both and lang, asr != none (also without audio: subtitle stream or sidecar) | `speech` dict or None | sets `run.analysis["audio"]["segments"][i]["kind"]` (speech/sound/blip); writes `OUT/transcript.txt`, `OUT/transcript.srt`; ASR trouble = `run.warn` |
| `survey.analyze_content(run)` | mode content/both | `content` dict | times itself with `run.timer("survey")` and `run.timer("ocr")`; appends `(t, kind)` to `run.visual_onsets` for cut/transition/transient/update starts and flash peaks (kind `flash`); writes `OUT/keyframes/Knnn.jpg` and the L2 sheets `OUT/sheets/kf_NN.jpg` via `views.write_view` |
| `detect.analyze_motion(run)` | mode motion/both | `motion` dict | times itself with `run.timer("motion_detect")` and `run.timer("motion_fit")`; appends `(first_change_s, "motion")` per motion event; writes `OUT/motion/css.txt` and `OUT/motion/Mxx_sheet.png` (at most `params.motion.sheets`, via `views.write_view`) |
| `sync.analyze_sync(run)` | audio section is not None | `sync` dict (`{"status": "insufficient", "pairs": n}` below 3 pairs) | reads `run.visual_onsets`, `run.analysis["audio"]["onsets"]`, speech segments |
| `sheets.render_views(run)` (integrator) | always | None | `OUT/overview.jpg` from the builders' overview cells (below); in mode both the motion cells go to `OUT/overview_motion.jpg` |
| `report.render_report(run)` (integrator) | always | report.md text | may write `OUT/timeline.md`; fills `run.analysis["suggested_next"]` |

`vl.py` times `probe`, `audio`, `speech`, `sync`, `views` and `total` itself; content and motion time their own
keys as listed. `timing_s` starts with every key at 0.0.

Testing across builders without each other: sync's KATs may fill `run.visual_onsets` from fixture truth (the
known flash times) instead of running the content survey; the end-to-end A2 through `analyze` runs after
integration. Speech's clamp uses its own `audio.analyze_audio` output.

## 9. Subcommand handlers (view commands)

`vl.py` dispatches these by name; implement exactly this signature in the owning module.

| Command | Function | Returns |
|---|---|---|
| `zoom OUT --event Mxx [--cells N]` | `sheets_motion.zoom_event(run, event_id, cells)` | list of view dicts from `views.write_view` (`zoom/Mxx.jpg` plus element crops `zoom/Mxx_<el>_<pct>.png`, none for boxes wider than half the frame); `vl.py` labels the grid `Read this` and crops `optional native crop` |
| `zoom OUT --segment Sxx` | `sheets_content.zoom_segment(run, segment_id, cells)` | list of view dicts (`zoom/Sxx.jpg`) |
| `zoom OUT --range A:B` | `sheets_content.zoom_range(run, start_s, end_s, cells)` | list of view dicts (`zoom/r_<A>-<B>.jpg`); must work in motion-only runs too |
| `frame OUT --t ...` | core, done (`vl.py:cmd_frame`) | `OUT/frames/f_<t>_<roi>[_w<W>].png` |
| `text OUT ...` | `report.text_view(run, start_s, end_s, grep, kind, max_lines)` | str for stdout |
| `rows OUT ...` | `report.rows_view(run, kind, start, count)` | str for stdout |

`vl.py` prints one `views.view_line(run, view)` per returned view: `<abs path> · WxH · N tok`.
Event ids are `M01`..., segment ids `S01`... (the parser normalises `m1` to `M01`).

Overview cells (for the integrator's `overview.jpg`):
- `sheets_content.overview_cells(run) -> list[(BGR ndarray, ascii label)]`: at most 40, shot starts first then
  the keyframes with the largest change, returned in time order; label like `K012 03:12.4 S`.
- `sheets_motion.overview_cells(run) -> list[(BGR ndarray, ascii label)]`: 3 per motion event (rest before,
  50 %, rest after), cropped to the event box + 24 px, at most 8 events.
The integrator sizes and labels the grid; cell images may be any size.

## 10. Images (`vl/views.py`, spec 5.4)

```python
view = views.write_view(run, image, "motion/M02_sheet.png", level="L2", kind="event", covers="M02",
                        cells=None, cell_px=None)
```
- Raises AssertionError if a side exceeds 1932 px or tokens exceed 4,761 (`views.check_limits`); shrink first
  with `views.fit_to_limits(image)`.
- `.jpg` = JPEG q90 (frame sheets), anything else PNG (motion sheets, crops). Records
  `{"file", "level", "kind", "covers", "cells", "cell_px", "w", "h", "visual_tokens"}` into `run.views`,
  replacing an earlier record of the same file. w/h/tokens are those of the written pixels (O1).
- `views.visual_tokens(w, h) = ceil(w/28) * ceil(h/28)`; `views.draw_label(img, text, x, y, scale, color)`
  draws Hershey text through `ascii_label` (non-ASCII becomes `?`). Korean goes into text files.
- Levels and kinds: L1 `overview`; L2 `keyframes`, `event`, `segment`; L3 `frame`, `crop`.
- Grids: `views.tile_size(shape, rows, max_tile_w)` and `views.tile_sheet(images, labels, cols, tile_w, tile_h)` (label
  bar of `views.LABEL_BAR_PX` under each tile, cells at most `views.ZOOM_CELL_W` wide) tile every sheet and zoom;
  `views.grid_tokens(shape, count, cols, max_tile_w)` gives a grid's cost before it is written.
- `pixels.channel_max(cv2.absdiff(a, b))`: per-pixel largest channel difference (13 to 17 times faster than numpy).
- `run.frame_index(t)` (the frame on screen at t, as `snap_time`), `run.frames_in_range()`, and
  `context.FRAME_MATCH_S` (0.05 ms: a 3-decimal content time names its frame).

## 11. Swift helpers (`vl/swiftbuild.py`)

```python
from .swiftbuild import helper_path
exe = helper_path("ocr")      # compiles skill/swift/ocr.swift on first use
```
- Output: `~/.cache/video-lens/bin/<name>-<sha256(source)[:8]>`, built with `/usr/bin/swiftc -O
  -parse-as-library`, so sources must use `@main`. Parallel callers wait on a lock; a changed source builds a
  new binary and removes the old one. Build failure or no swiftc: VlError(5). This folder is never moved by
  `VIDEO_LENS_CACHE_DIR` and never purged.
- Names: `ocr` and `render` (content), `transcribe` (audio). `build_swift(source, name, bin_dir)` exists for
  tests. Compile once before starting parallel OCR processes.

## 12. Selftest (`selftest/`)

```python
# selftest/kat_motion.py
import fixtures_motion as fm
from katlib import FAIL, PASS, SKIP, kat, verdict
from vl.cli import prepare_run

@kat("M1", "motion", quick=True)          # quick = row marked Q; chrome=True (declared); manual=True (A7)
def m1_translate(ctx):
    video = ctx.fixture("motion_translate_cfr.mp4", fm.build_translate_cfr)   # built once per selftest run
    result = ctx.vl("analyze", video, "--out", ctx.out_dir("M1"), "--mode", "motion", "--json")
    ...
    return verdict(failures, "name easeOutCubic, start +0.4 ms, duration 299.9 ms")  # or (SKIP, "why")
```
- `kat.py` imports every `selftest/kat_*.py`, runs the chosen KATs, prints `ID AREA STAT SECS DETAIL`, exits 1
  on any FAIL. Flags: `--quick`, `--only ID,ID`, `--chrome`, `--keep` (prints the scratch folder). Same via
  `vl.py selftest ...`. With `--only`, another module's import error is shown as SKIP, not FAIL.
- `ctx.fixture(name, build)`: `build(tmp_path)` writes one file (tmp keeps the extension); prefix names with
  your area (`motion_`, `content_`, `audio_`, `declared_`) because the fixture folder is shared.
- `ctx.out_dir(name)`, `ctx.work`, `ctx.vl(*args)` (subprocess `python3 vl.py ...`, text mode),
  `ctx.is_quick`, `ctx.is_chrome`. The runner points `VIDEO_LENS_CACHE_DIR` into its scratch folder.
- Shared encoders in `fixtures_core`: `encode_bgr(frames, path, (w, h), fps)` (libx264 crf 18 yuv420p),
  `make_vfr(src, dst)` (mpdecimate hi=64 lo=32 frac=0.33, `-fps_mode vfr`), `ffmpeg(*args)`.
- A KAT that exercises `analyze` end to end needs modules from other owners; until they exist, return SKIP
  naming the missing modules (see `kat_core.o3_stdout_is_report`, which checks with
  `importlib.util.find_spec("vl.<name>")`). Test your own stage function directly meanwhile.
- KAT ids: motion M0 to M8, M10 to M15, M17 to M27; content C1 to C4, C10; audio A1 to A8; declared M9, M16; core
  C5 to C9, O3, X1 to X4; integrator O1, O2, O4, O5. Real-media KATs return SKIP when the file is absent.

## 13. declared.mjs (standalone)

`node scripts/declared.mjs URL --out DIR [--viewport 1280x800] [--wait-ms 0] [--render FPS SECONDS]
[--trigger "click|hover SELECTOR"]` per spec 6 and 7.9. It shares no Python interface: it writes
`DIR/declared.json` (and `DIR/render.mp4` with `--render`), prints on success `declared.json: <abs path> · N
animation(s)` plus one line per animation (at most 30: target, kind, properties, delay, duration, curve), prints one line
`video-lens: <what failed>. <what to do>` on stderr and exits 5 when Node or Chrome is missing, 2 on bad
arguments. `kat_declared.py` marks M9 and M16 `chrome=True`; M9 measures `render.mp4` with
`ctx.vl("analyze", ...)` once the motion module exists (SKIP naming the missing modules before that).
