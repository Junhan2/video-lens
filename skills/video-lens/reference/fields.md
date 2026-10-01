# video-lens reference: fields, thresholds, confidence, failure modes

Read on demand. Times are seconds on ffmpeg's input timeline (0 = `format.start_time`); motion times have 4
decimals, content times 3. Paths inside `analysis.json` are relative to OUT, except `input.path` and `cache.dir`.

## 1. What OUT holds

| File | Level | What |
|---|---|---|
| stdout = `report.md` | L0 | at most `--budget-chars` (6,000). Line 4 is `OUT = <path>`; `OUT` in every command means that path |
| `timeline.md` | L1 text | content and both modes: blocks of rows, capped at `--timeline-chars` (40,000) |
| `transcript.txt`, `transcript.srt` | L1 text | `[mm:ss.s] sentence` lines; SRT cues of at most 7 s |
| `overview.jpg` | L1 | content cells (shot starts first, then the largest changes, at most 40) or, in motion mode, 3 cells per motion event (rest before, 50 %, rest after, at most 8 events) |
| `overview_motion.jpg` | L1 | mode both only: the motion cells, since event crops and full frames do not share a cell shape |
| `sheets/kf_NN.jpg` | L2 | keyframes that are not `same_as` repeats, 4x4, tile 384 px wide |
| `motion/Mxx_sheet.png` | L2 | one event: before/after with numbered boxes and delay arrows, timing map, change heatmap, progress plot, key numbers, 5-frame filmstrip. At most `--sheets` (6), the largest by `peak_blob` |
| `motion/css.txt` | text | CSS per event (one rule per repeat group; none for instant changes); every number labelled measured, fitted or suggested |
| `zoom/*.jpg`, `zoom/Mxx_<el>_<pct>.png` | L2, L3 | written by `vl.py zoom`: the grid (`Read this`), then optional native element crops (none for elements wider than half the frame) |
| `frames/f_<t>_<roi>.png` | L3 | written by `vl.py frame`: native pixels, downscaled only above 1932 px |
| `keyframes/Knnn.jpg` | source | keyframe exports (1280 px wide at most); read the sheets instead |
| `digest/` | on request | written by `vl.py digest` (never by analyze): `digest.json` (per scene: span, chapter, `frame` (the keyframe state on screen longest in the scene; among those shown at least half as long, the one with the most text; a black frame without text only as a last resort), `headline`, `ocr` lines tallest first, the first `speech` sentences, YouTube `link`; `chrome` counts the lines left out as screen furniture: wholly in the top or bottom 5 %, text in a quarter of the scenes (at least 3) or persistent, and misreads in the same place, with the recurring texts), `frames/Sxx.jpg` (one per scene at source size, at most 1932 px per side), `sheet_NN.jpg` (4x4 scenes, id and mm:ss range under each cell); with `--captions`: `digest.md` (frames linked) and `digest.html` (frames embedded); a rebuild whose scene spans changed moves `captions.json` to `captions.stale.json` |
| `analysis.json` | data | everything below; do not Read it whole, use `vl.py rows` and `vl.py text` |
| `status.json` | progress | `{stage, done, total, eta_s, started_at}` while a run is going |

Every image is at most 1932 px per side and 4,761 visual tokens (`ceil(w/28) * ceil(h/28)`), so nothing is
rescaled on the way to the model. Each view line prints its real size and token cost.

## 2. report.md

Header: file name; `WxH · duration · frames · fps CFR|VFR · audio`; mode and why, cache hit/miss, wall time;
`OUT = ...`; then Audio, Speech and Sync lines when they exist.

- **Content:** counts (shots, keyframes, repeats, visual events, text events, OCR samples), persistent text, and one
  line per timeline block: `[mm:ss-mm:ss] K012-K018 · "first new on-screen text" · first 60 chars of speech`.
- **Motion:** header `N motion events · N subtle · not fitted: N noise, N micro`. A `Repeats` line per group of events
  repeating one transition (loop, or >= 3 in a row with the same types and travel within 15 %, judged by each event's
  main element): period, the ONE curve and duration fitted across the repeats with its range and RMSE, every repeat's
  start, median travel and the outliers; the table then keeps the group's lead row and its outliers and points to `vl.py
  rows OUT --kind elements --from N` (the first hidden row) for the rest. One row per element: `el | types | start |
  delay | duration (range) | easing (rmse) | travel css px | conf`; the element's channels (types) share that row's one
  timing and curve. `delay` is the element's timing-group delay inside its event. `easing` shows `name ~ ties` (up to 4)
  when near ties exist, `cubic-bezier(...)` for a custom curve (with `~ names` when named curves of different shapes
  tie), `linear() (measured)` for overshooting motion, `? (...)` when the start state was never seen, `instant (<= 2
  frames)` for a swap. `travel` is the fitted translate in CSS px, `(at least)` when the element crossed a clip line and
  its hidden rest state was taken just beyond it, plus `scale a>b` and `rot N deg` when present. A split element
  (channels with clearly different timings) gets one row per channel, `el` = `Mxx.n channel`, each with its own start,
  duration and curve; the first says why (`split: one timing misses opacity, rmse shared vs own`). `Curves:` gives each
  named curve's bezier. In mode both, events that are scene changes (a content cut or dissolve, or an instant change the
  content survey also saw) are listed on one `Scene changes` line instead of the table. Below: overlaps with content
  events, stagger, scroll travel with the share of unreliable steps, stalls and notes, then the CSS (inline when <=
  1,000 chars, else a pointer).
- **Views:** every image with its tokens, then the zoom and frame commands for the next level down with their
  estimated cost. Motion sheets and the motion overview say which events they leave out.
- A table that does not fit ends in `+N rows: vl.py rows OUT --kind K --from N`; blocks end in
  `+N blocks from [mm:ss]: Read timeline.md or vl.py text OUT --from T`.

## 3. timeline.md

A block starts at a shot change, at a keyframe that changes >= 30 % of the frame, or at a keyframe or speech that
follows >= 2 s without speech (in a silent video, any keyframe); blocks are merged until they span >= 20 s
(>= 60 s for videos over 10 min). Rows:

- `K031 S` keyframe at the time its screen state appeared (S = settled: held >= 0.2 s; m = moving), `same as K012`
  for a repeat (TEXT+ only for text its original lacks), `TEXT+ "..."` for on-screen text that is new at that
  keyframe. A block opened by speech starts at the first keyframe shown in the pause before it.
- `SAY ...` consecutive transcript sentences until the screen changes or 2 s of silence.
- `TEXT "..." (conf)` text that no keyframe showed; `EVENT kind (e2e)` (transition, transient, update, and an
  activity event a keyframe's state begins with); `BLIP 110 ms`, `SOUND 2.4 s`; `MOTION M02 3 el · easing duration`
  (mode both, motion that no content event explains).
- Above `--timeline-chars`, SAY rows shrink to their first sentence plus `(transcript.txt Lx-Ly)`; above that,
  trailing blocks are cut with a `vl.py text OUT --from T` pointer.

## 4. analysis.json (`video-lens/1`)

Top level: `schema`, `tool_version`, `generated_at`, `input {path, url, key, size_bytes}`, `video` (probe:
`display_width/height`, `rotation`, `duration_s`, `start_time_s`, `video_start_offset_s`, `frames`, `avg_fps`,
`is_vfr` = max dt > 1.5 x median dt, `median_dt_ms` = resolution, `min/max_dt_ms`, `pts_count_check` ok|mismatch),
`audio_stream`, `subtitle_streams`, `mode`, `mode_reason`, `range {start_s, end_s, is_full}`, `params` (every
`auto` resolved), `timing_s`, `cache {dir, hits, misses}`, the sections below, `views`, `suggested_next`, `warnings`.

**Mode auto:** range <= 120 s and (no audio or audio activity < 0.2) -> motion; range > 120 s -> content;
otherwise both. Audio runs whenever there is an audio stream; speech only in content and both.

### audio
`loudness {integrated_lufs, lra_lu, true_peak_dbfs}` (ebur128), `noise_floor_db` (p10 of 30 ms RMS),
`activity_ratio`, `level_db_1s` (one value per second), `segments [{start_s, end_s, kind speech|sound|blip,
mean_db}]` (active = 12 dB above the floor and above -60 dBFS; gaps < 250 ms merged; >= 30 ms kept; blip <= 150 ms;
speech = overlaps a transcript segment), `onsets [{t, strength}]` (spectral flux, median + 4 MAD, refined to the
level midpoint; every onset is listed).

### speech
`source` subtitle-stream | sidecar | apple-speechtranscriber | whisper.cpp | none; `locale`; `word_start_clamped`;
`chars`; `segments [{start_s, end_s, text, words [[s, e, word]]}]` (word text stripped); `sentences [[start, end,
text]]` (line n = transcript.txt line n); `files {txt, srt}`.
Auto order: first subtitle stream > `<stem>.srt` > `<stem>.vtt` > Apple SpeechTranscriber (on device, `--lang`)
> whisper.cpp with the largest real ggml model in `~/.local/share/whisper` (installed: large-v3-turbo q5_0; or
`--whisper-model`, `$VIDEO_LENS_WHISPER_MODEL`; a `for-tests` model is refused) > none. ASR word starts are
clamped to the voiced onset. whisper runs with DTW word timing (`-nfa -dtw <preset>`, flash attention off).
whisper segments with no letter or digit, or with < 50 % of their time over audio activity, are dropped with a
warning quoting the text (whisper invents subtitles over music and beeps).

### sync
`{status ok, pairs, offset_ms_median, offset_ms_iqr, xcorr_lag_ms, frame_interval_ms, convention "positive =
audio later"}` or `{status insufficient, pairs}` below 3 pairs. Visual onsets (cuts, transitions, transients,
updates, flash peaks, motion starts) and non-speech audio onsets are paired as mutual nearest neighbours within
0.25 s; `xcorr` correlates 10 ms Gaussians over the pairs.

### content
- `shots [{id S01, start_s, end_s, keyframes}]`: spans between cuts.
- `events [{id V001, kind, start_s, end_s, duration_ms, e2e_frac, peak_frac}]`: runs of active frames. cut = one
  frame with e2e >= 0.30; transition = e2e >= 0.30 over more frames; update = 0.04 to 0.30; activity < 0.04;
  transient = followed within 0.6 s by an event that restores the earlier state (a flash). `end_s` is the display
  end of the last changing frame.
- `keyframes [{id, t, shown_s, frame_index, shot, settled, hold_s, change_frac, reason first|shot|change|peak|detail,
  same_as, file, sheet, cell, text_ids}]`. `t` is the sampled frame; `shown_s` is when its screen state appeared
  (the change before it, to the frame); `hold_s` is how long that state stayed (its still span, cut where the next
  keyframe's state began). `detail`: a held state (>= 2 s after a mad peak or a >= 3 px step) that differs from
  the keyframe before by >= 0.2 % of the pixels at 640 px although the 160 px survey barely changed (slides that
  share a template and differ in a word); its `change_frac` is measured at 640 px. `same_as` = dHash distance <= 4
  AND pixel change < `--key-low` on non-busy 160 px pixels AND < 0.2 % at 640 px.
- `text {persistent [{text, share, first_s, last_s}], events [{id T001, text, conf, first_s, last_s, end_s,
  samples, box [x, y, w, h] normalised to the frame, px_h, legible_in_sheet}]}`. Persistent = seen in >= 50 % of
  at least 3 OCR samples (logos, watermarks). Lines need conf >= 0.3 and >= 2 characters. Two readings are the same
  text when they match exactly or by difflib >= 0.8 with equal digit sequences and an unchanged line box. `first_s`
  is the first sample that saw the text, moved back to the keyframe's `shown_s` when that sample is a keyframe;
  `last_s` is the last sample that saw it; `end_s` is the first later sample without it (the next keyframe's
  `shown_s` when that is earlier), or the range end: the text is on screen from `first_s` until `end_s`.
- `ocr {engine apple-vision-accurate, langs, samples, frame_width}` or null with `--ocr off`.

**Keyframe budget:** soft budget `min(150, 12 + 2 x minutes)` drops the smallest-change keyframes that are neither
shot starts nor screen states held >= 2 s. Held states (slides) are dropped only above the hard cap
`min(600, 12 + 12 x minutes)`. `--max-keyframes N` sets both to N (shot starts are never dropped).

### motion
- `summary {motion_events, subtle_events, micro_events, noise_events, noise_times_s, micro_times_s, loop
  {period_s, cv} | null, listed_events, detect_size, repeats [{events, lead, lead_element, count, shared, outliers, period_s,
  starts_s, duration_ms, duration_ms_range, easing, cubic_bezier, fit_rmse, near_ties, shape_tie,
  travel_px_median}]}` and `work {crop_src, size, scale}`. A repeat group's curve and duration are fitted ONCE over
  all its repeats (their progress merged, each timed from its own start), then every repeat's start is refitted
  under them (twice). `shared` repeats take that fit (their main element's record is rebuilt under it, note "timing
  and curve from the shared fit"); `outliers` are repeats the shared fit misses by more than 1.5 x their own fit +
  0.01 RMSE, which keep their own fits and are refitted out of the group. `period_s` is the median spacing of the
  shared starts.
- `events [{id M01, class, first_change_s, last_change_s, features {peak_maxdiff, peak_blob_frac,
  changed_frames}, box_src, window_s, analysed, mode elements|scroll|activity-bbox, segmentation
  rest-frame|activity-bbox, elements, timing_groups [{start_s, duration_ms, elements, delay_ms}], stagger
  {step_ms_median, step_ms, linearity_ms, method t50-diff, order, order_corr_x, order_corr_y} | null, scroll
  {element, travel_px, peak_speed_px_s, peak_step_speed_px_s, response_min, unreliable_frac} | null, css, evidence
  {sheet, sheet_tokens, crops}, notes}]`.
- element: `id M02.1, bbox_src, bbox_css, origin end|start, appears, disappears, types (translate scale rotate fade
  color scroll other), geometry {translate_px (fitted), translate_css_px, translate_observed_px, translate_observed,
  scale_from, scale_to, rotate_deg, shear_max, peak_speed_px_s}, timing {start_s, start_s_range, start_visible_s,
  end_s, end_visible_s, duration_ms, duration_visible_ms, duration_ms_range, t50_s, resolution_ms, stalled_frames_s,
  dropped_frame_ratio, instant}, easing {name, aliases, cubic_bezier, source named|custom|measured-linear, fit_rmse,
  free_fit, near_ties, shape_tie, overshoot, css_linear, css_linear_timing}, clip, channels {<name>: {rmse,
  amplitude_fixed, amplitude_px|from/to, own_fit (split elements only)}}, property_timing_differs, split, tracked_frac,
  confidence, notes`.
- `channels.*.rmse` is each channel's error under the element's one shared fit. `split {channel, shared_rmse,
  own_rmse}` (and `property_timing_differs` true) only when the data clearly rejects one timing: a channel's own fit
  (named-first plus the free curve, like the shared one) starts more than a frame or lasts more than 10 % apart, and
  the shared fit misses it by more than 1.5 x its own RMSE + 0.01. Only then do channels carry `own_fit {name,
  cubic_bezier, start_s, duration_ms, rmse}`, and the CSS gives each property its own transition. A split element's
  notes leave out the shared fit's tie, rival and start-range notes: its rows show each channel's own fit.
- `clip {edge left|right|top|bottom, container frame|inner, line_src_px, seen_s [first, last], hidden_until_s |
  hidden_from_s, shown_px, travel_min_px, visible_travel_px, travel_fitted_px, reading at-edge|beyond-edge|seen}` for an
  element that slides into or out of view across a clip line: the frame (or ROI) edge, or a container that clips it
  (`inner`; the line is where its swept path ends; a box already at the frame edge, a drawer at translateX(100%),
  crosses that edge). Such an element is tracked translation-only, matching only the template part on the visible side
  (ECC inputMask, NCC seed of the part surely visible, which must hold >= 10 % of the template's contrast: a shadow
  fringe alone places nothing), so the cut is not read as scale or fade. Its box is its visible extent, a box-shadow and
  a slow tail's creep included. `hidden_until_s` is the last frame before it showed (`hidden_from_s` the first after it
  left); those frames keep the model out of view. `travel_min_px` = the seen travel + the hidden part from its full
  size. Reading `at-edge` (kept unless the free travel fits more than 2 x better + 0.0002 RMSE): the hidden rest state
  lies just beyond the line, travel = `travel_min_px`, easing quoted (confidence at most medium); `beyond-edge`: the
  travel is fitted (>= the minimum) and the start state counts as not observed (`?`); `seen`: a strip of it (a
  box-shadow's fringe, or its own edge) still showed inside the line at that rest state, `shown_px` wide, found by the
  walk or among that rest frame's elements. It places the rest state: travel measured (`travel_min_px` = the fitted
  travel), easing quoted, no `(at least)`; the strip is not listed as an element of its own.
- `start_s`/`duration_ms` are the fitted curve's; `*_visible_*` are the first and last detected change (ease-in and
  ease-out hide frames). `duration_ms_range` = every duration whose RMSE <= 1.25 x best + 0.002; for a custom curve
  also every duration its free curve, refitted with that duration pinned (5 % steps outward, each from the last one,
  until one fits worse or leaves the search box), fits within that limit: a long flat tail trades against the duration.
  null for an `other` element: change energy is no progress, so no fit bounds its duration (the report says `no range`). `start_s_range`
  (custom curves, and named curves kept only by the 0.004 margin): starts 1 to 3 frames either side whose refitted
  free curve fits as well; null otherwise. `instant`: the primary channel went from rest to rest within 2 frame
  intervals, or from 10 % to 90 % within one (a swap): no easing is measurable and no CSS rule is written.
- Event notes: `instant: one frame changes` (not fitted), `motion was already running at the first frame of the
  range` / `still running at the last frame` (confidence low: widen `--start/--end`).

**Classes** (640 px detection scale, A = frame area; `peak_maxd` = largest per-channel difference):
noise `peak_maxd < 16`, or `< 32` with the largest blob < 0.002 A (counted only) · subtle `16..31` with blob >=
0.002 A (fitted, noted) · micro `>= 32` with blob < 0.0005 A (counted; fitted with `--micro`) · motion otherwise.
Frames with >= 5 changed px (diff > `--pix-threshold` 8) join an event within `--gap-ms` 150; faint "speck" frames
(max diff < 32 and blob < 0.2 % of the frame) never bridge that gap on their own.

**Easing:** 35 named curves (`reference/easings.json`: CSS, easings.net, Material 2 and 3; aliases such as `tailwind
ease-in-out` = md2-standard). The named curve wins when its RMSE <= 1.25 x free, or <= free + 0.004 and it agrees with
the free curve (shape within 0.05, or start within half a frame); otherwise the free curve is reported as custom with a
note naming the displaced curve. The free curve's y control points stay in [0, 1] unless the data under- or overshoots;
it is seeded from the best named curve and the next best ones that differ from every seed by more than 0.1 (3 seeds),
and its first simplex steps inward at a bound. `near_ties` = named curves within 0.05 (max |dy|) of the chosen one with
RMSE <= 1.25 x best: they cannot be told apart in this recording. **Shape tie** (`easing.shape_tie`): when the named
curves within 1.25 x the best RMSE include one more than 0.05 from the best, and the named-first rule would pick a named
curve over the free one, it would pick among different shapes by noise (short ease-in exits). A free curve that fits
clearly better than every named one is reported as custom without a tie. A curve does not tie when a channel fitted
alone on its own timing clearly prefers the best one (RMSE > 1.25 x + 0.0005) and no channel prefers it: a precise
translate outvotes a gain-biased opacity channel. On a tie the free curve is reported instead, `near_ties` lists every
tied name, the start and duration ranges span the tied readings, and confidence is at most medium. A free curve whose y2
lies within 0.15 of 1 is refitted with y2 = 1 and kept so when that fits within 1.25 x: a tail creeping below a pixel
otherwise trades an end slope nobody sees for a shorter duration (CSS curves end on the end level unless they
accelerate into it). A named curve that fits within the range limit (1.25 x the custom RMSE + 0.002) of a custom reading
is named in a note ("fits almost as well"), and the start and duration ranges hold its reading. Overshoot (progress >
1.02) gives `css_linear`, a 21-stop `linear()`.

**Confidence:** high = tracked >= 80 %, RMSE < 0.01, the duration range within ±10 % and a start range of at most
one frame. medium = one of those fails, a single fixed-amplitude channel with near ties, a shape tie, a start
assumed just beyond a clip line (`clip.reading` at-edge), or a scale assumed to start (end) at 0 (note "grew from
nothing"). low = activity-box
fallback, type `other`, scroll response < 0.3 on > 20 % of steps, unknown start state without a fixed channel, an
instant change, or motion cut by the range.

**Scroll:** chosen automatically when an end element keeps its outline (IoU >= 0.9) while its content moves >= 3
px with median phase-correlation response >= 0.3 and the outline centre moves <= 1/4 of that; or with
`--motion-mode scroll`. Travel is integrated phase correlation (event mode `scroll`, segmentation `rest-frame`).
On 3D carousels this is not a pure translation: quote travel with `unreliable_frac`.

**Timing groups:** starts within 1 frame and durations within 2 frames. Stagger (>= 3 start clusters: starts within 1
frame whatever their durations, split elements left out while others remain) = differences of the clusters' median
t50: the parts of a list row without a background start together but fit different durations.
**CSS px** = source px / `--dpr`; a warning appears when dpr is 1 and the source is >= 2000 px wide.

### views
`[{file, level L1|L2|L3, kind overview|keyframes|event|segment|frame|crop, covers, cells, cell_px, w, h,
visual_tokens}]`: the actual written pixels.

## 5. declared.mjs (Chrome, optional)

stdout: `declared.json: <abs path> · N animation(s)` and one line per animation (at most 30): `target · kind name ·
properties · delay N ms · N ms · curve`. `DIR/declared.json`: `schema video-lens-declared/1, url, viewport, wait_ms,
trigger, chrome, render {file
render.mp4, fps, seconds, frames, origin_ms, animations_driven} | null, animations [{target (tag.class x3), name,
kind CSSAnimation|CSSTransition|Animation, play_state, properties, delay_ms, duration_ms, effect_easing,
keyframe_easings, iterations ("infinite"), fill, start_ms, clip_start_ms}]`, sorted by start + delay. For CSS
animations the curve is in `keyframe_easings` (the effect easing reads linear). Render clip time 0 is the earliest
start among animations that began after the trigger (else the earliest start); each animation is seeked at its
own offset, so JS staggers survive. Load-time transitions and finished CSS animations are recorded by listeners
installed before navigation. Exit 5 without Node or Chrome, 3 unreadable page, 2 bad arguments or unmatched
`--trigger` selector. `$VIDEO_LENS_CHROME` overrides the Chrome binary. Not covered: rAF/JS-driven motion (cannot
be seeked), iframes, WAAPI animations that finish before the read.

## 6. Environment

`$VIDEO_LENS_CACHE_DIR` (default `~/.cache/video-lens`, 2 GB LRU, `vl.py cache --purge-older DAYS`),
`$VIDEO_LENS_WHISPER_MODEL`, `$VIDEO_LENS_CHROME`. Swift helpers build once into `~/.cache/video-lens/bin`. Python
warnings go to `$TMPDIR/video-lens/python-warnings.log`; an internal error writes `$TMPDIR/video-lens/last-error.txt`.

## 7. Failure modes

| Symptom | Cause | Do |
|---|---|---|
| exit 2 / 3 / 4 / 5 | bad arguments / unreadable input / empty range / missing tool or helper build | quote the one-line error |
| 0 motion events, motion was visible | change below 8 levels, or motion drowned in a large region | `--roi x,y,w,h` or `--pix-threshold 5` |
| one box, confidence low, note "rerun with --roi" | photo or gradient background: rest segmentation failed | `--roi` around one element |
| box covers two cards or half a card | segmentation merged or split elements | `--roi`, then check the sheet again |
| easing is a near tie | fade-only or strongly decelerating curve | say the names cannot be told apart |
| mode both: event on the `Scene changes` line | a cut, dissolve or slide swap, not UI motion | ignore its element fit |
| `?` easing, note "start state not observed" or "farther out" | the element's start state was never seen | the easing is unknown; use declared.mjs or a wider `--roi` |
| note "entered from beyond the ... edge" / "left past", travel `(at least)` | a slide across the frame edge or a clipping container | quote timing and curve with the stated assumption (started just beyond the edge) |
| the same note naming a strip "still inside that edge" | its box-shadow (or edge) showed at the hidden rest state | quote the travel as measured; no assumption |
| one row per avatar, text, badge and separator of a list | its rows have no background of their own | quote the Stagger line; `--roi` around one row; do not quote a split 1-2 px separator's travel |
| rows `Mxx.n translate` / `Mxx.n opacity` | the channels have clearly different timings (split) | give each channel its own timing; the first row says why |
| `cubic-bezier(...) ~ a, b, c`, note "named curves tie" | named curves of different shapes fit equally well (short ease-in exits) | quote the bezier, name a, b, c as equally likely |
| note "X fits almost as well ... the start and duration ranges hold its reading" | a noisy recording (small element, codec damage beside a neighbour) cannot separate the custom curve from X | quote the bezier and X as equally likely, with the ranges |
| note "grew from nothing" | the element was missing from one rest frame and opaque whenever seen: it scaled from (to) 0 | quote scale(0) as assumed, not seen |
| a `fade` row over the whole crop, note "backdrop (scrim)" | the page behind a sheet or dialog dimmed (or cleared) under an overlay | quote it as the overlay's opacity timing, beside the sheet's row |
| one box over two moving elements, split channels | they touch only through static content (a chip beside a badge over one text line) | `--roi` around each |
| note "already running at the first frame" / "still running at the last frame" | `--start/--end` cut the motion | widen the range |
| note "start uncertain" | a custom-looking curve's flat start trades against its start | quote the start range and t50 |
| `pts_count_mismatch` warning | container packets differ from decoded frames | times still use showinfo pts |
| no transcript | no locale asset, no audio, or every source failed (warnings say which) | `--asr whisper`; never upload audio |
| OCR misses a line | Vision can drop a whole line over busy backgrounds; small text < ~16 px tall | `vl.py frame --t T --roi ...` and read it |
| `--fast` (auto above 20 min) | `-skip_frame noref` can skip flashes shorter than a GOP | `--fast off` for flashes |
| OCR "could not read N frames" warning | a helper stalled or crashed | rerun the same command; only those frames retry |
| killed or timed-out run | | rerun the same command: finished stages and OCR rows are cached |

## 8. Deviations from the spec (measured reasons in the build reports)

- **Core:** the parser lives in `vl/cli.py`; range decodes use `-t` as an input option (output `-t` logged one extra
  frame); `frame` snaps T to the frame on screen; probe skips cover-art streams and discarded packets;
  `VIDEO_LENS_CACHE_DIR`; `content.ocr` is resolved after mode selection.
- **Motion:** speck rule for grouping (orbit chained 11 transitions into 9 events under the literal rule); scroll
  auto-selection by outline and content travel (event mode `scroll`); phase correlation crops to even DFT sizes
  (odd sizes read +0.5 px per step); a start element that tracks onto an end element is a duplicate, not an exit;
  a rejected tracker frame keeps the last accepted warp; `--no-joint` solves amplitudes under each channel's own
  fit; continuous motion over 240 frames is listed, not fitted; stagger needs >= 3 groups; an overshoot whose
  bezier RMSE >= 0.01 takes its timing from the measured `linear()`; one-frame events are not fitted (instant);
  ECC stops after 50 iterations at 1e-5 on templates over 250,000 px; repeat groups; start ranges and the agreement
  rule for named curves (Easing above). Since the D1-D4 fixes: clip-line elements (`clip` above); the ECC walk
  rejects a match brighter than the last accepted frame by > 0.15 gain or off the constant-velocity prediction by
  > half the template (look-alike list rows 116 px apart took over a fading row); a free amplitude is capped at
  1.5 x the range of every accepted sample (faint ones too), not only the well-weighted ones; per-channel fits get
  the free curve and the split rule above; free-fit seeds and bounds (Easing); shape ties; one shared fit per repeat
  group, grouped by each event's main element (scroll element, else the largest), done before the cache; sheets
  are drawn after the repeat fit from a second decode of their windows. Since the verification pass: clip reading
  `seen` and flush edges (`clip` above); scale counts per axis where it changes the element's size by >= 1 px and the
  element is >= 6 px thick along it, rotation where the corners move >= 1 px (a 10 px bar read 3 % scale, a 20 px glyph
  0.48 deg), and the ECC warp keeps no scale along an axis thinner than 6 px; duration probes of custom curves; shape
  ties only where named-first applies; start-cluster stagger; the sheet's key numbers show split elements per channel
  and its filmstrip labels sit in bars below the cells; `vl.py rows --from` defaults to 1. Since the W1/W2 fixes: the
  background is the median of the border-ring pixels that stayed still (a drawer over most of the ring was the
  background); static content (the same non-background pixels in both rest frames, their neighbourhood mostly still, plus
  a 1 px fringe) is left out of element boxes and a component that is almost all of it is no element (a same-coloured
  top bar merged with a drawer, a nav icon beside a badge, its edge flickered by the codec); a box is the element's own
  changes (per-frame, or between the rest frames by >= half its typical change) and a clipped element's is its visible
  extent; nesting is judged on whole components; what shows in the template's pad and is not the element is masked and
  held at the background in the template (static pixels joined to the element without a gap stay, its own border or
  edge ring: a colour change in place shows them alike at both rests); the work crop keeps >= 24 px around the activity; types are decided where the
  element is >= half its rest size, rotation only where the template holds an angle (a disc does not) and on the corners
  at that size, and a centre shift that a scale about a point within 10 % of the box centre explains is no translation;
  an element missing from the other rest frame that is opaque whenever seen at >= half its size and smaller toward that
  side grew from (shrank to) scale 0, not faded; the flat-end refit and the in-range rival (Easing). Since the weakness
  follow-ups: when the border ring hardly stayed still (per frame or between the rest frames), a backdrop is looked for,
  one gain and per-channel offset mapping the first rest frame onto the last over >= 30 % of the page's structure (a
  vaul-style 40 % black overlay changed 99.8 % of the ring and the sheet's white became the background); activity and
  the rest frames are then compared under each frame's share of it, the window is cut to what moved beyond it, and it is
  listed as a `fade` element of its own; page content another element covers or uncovers (its extent reaches the content
  at one rest frame and holds every change in its box beyond codec noise, 32 levels under a backdrop) is no element; a
  component that is almost all static content keeps what changed in it when that stands out from it (a dot on a card, a
  badge on an avatar that touches text lines); content an element sits on is left out of its template, and a small
  element missing from the other rest frame that covers content there is tracked composited over that frame (shift and
  scale per axis, no rotation); a clipped walk that loses its prediction reseeds from the phase-correlation shift since
  its last accepted frame, and its ECC stays within the seed's search across the motion axis (a 7 px strip slid 19 px);
  a moving span counts the samples between hidden ones as moving (a shade's first strips hold only a grab handle); a
  VFR file's last frame that changes by < 5 % of the event's peak is the motion settling, not a range cut. Since the
  colour follow-ups: a colour change is measured from a frame up to 100 ms before its event's window (never within 100
  ms after an earlier change), not from the window's first frame, which an ease colour change had already left at 6 %
  (`channels.color.from_hex` is that frame's colour); an end element whose outline a start element shares (IoU >= 0.9),
  whose walk lost it before that rest frame and whose box the blend of the two rest frames explains is one colour
  element measured over its box (a ghost button filling read as a stretch, one row per rest frame).
- **Content:** stillness decided while streaming (1 s lag, running p10); `same_as` also needs a low pixel change,
  at 160 px and at 640 px; held states the 160 px survey cannot tell apart are checked at 640 px (`detail`
  keyframes); `hold_s` is cut at the next keyframe's state change; keyframes carry `shown_s`; keyframe frames are
  decoded once at the OCR width and OCR reuses them; fuzzy text matches need equal digits and an unchanged line box;
  persistent text needs >= 3 samples; transient merge only after cut, transition or update; the keyframe budget
  protects held screen states (section 4).
- **Audio and speech:** activity edges refined to 10 ms hops; onset floor clamped at -60 dBFS; a speech segment ends
  at its last word; transcript sentences run across ASR segment breaks; whisper uses DTW word times; failed speech
  sources are cached so a hit stays a hit.
- **Integration:** `overview_motion.jpg` in mode both (scene changes left out); overview label bars are 22 px (one
  tiler for every sheet, `views.tile_sheet`); motion events overlapping a content event are flagged in mode both,
  scene changes are left out of the table, timeline MOTION rows, Next and the motion overview; `vl.py text` keeps
  rows whose on-screen or spoken span overlaps `--from/--to`; a rerun into the same OUT removes a `timeline.md`,
  overview or `css.txt` it no longer writes.

## 9. Ship gates

Every gated feature ships: its KAT passed in the final `vl.py selftest --chrome` run (subtle M5, micro M13, exit
elements M12, overshoot and css_linear M14, grouping M15, `--trigger` M16, scroll auto-selection M11b, same_as C1,
speech clamp A4, subtitle sources A6, range/window timeline C7, same-template slides C10). O1 to O5 pass.
