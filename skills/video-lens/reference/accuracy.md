# video-lens accuracy (measured)

Read this only when you need the exact bound for a case the summary in SKILL.md section 7 does not cover.

Measured by `vl.py selftest` on synthetic fixtures (CFR and VFR) unless noted; quote the bounds, not the best case.
- Motion at 60 fps, named curves: start within ±1 ms, duration within 3.5 %, stagger step within
  0.6 ms, travel within 1 px of the truth; the fitter named 105/105 curves right (timing pinned, 1 %
  noise). Fade-only and strongly decelerating curves can be near ties: say so. 30 fps: duration
  within one frame (33 ms). A Chrome-rendered CSS animation measured 0.2000 s + 299.9 ms for 200 +
  300 ms. Real 3D carousel (orbit): 11/11 transitions and the 1.55 s loop found; travel is approximate.
- Custom (non-named) curves, 8 elements on review fixtures (not in selftest): t50 within 2.3 ms, but
  start only within 30 ms and duration within 13 %; the reported start and duration ranges held the
  truth 8/8. Quote t50 and the ranges. 6-row stagger, one custom curve, 30 fps, DPR 2: t50 within 1 ms, start
  within 16 ms (half a frame), duration -1 to -3.4 % with the truth inside every range, one row per element. A list
  without row backgrounds (4 rows of 5 parts, 1 CSS px separators, DPR 2): stagger steps within 1.1 ms.
- Slides across an edge (frame edge CFR and VFR, a clipping panel, a flush drawer; started just beyond it, or with its
  box-shadow still showing inside the edge): start within 0.3 ms, duration within 0.4 % (a custom curve with a long flat
  tail included), the curve right (the name, or a custom curve within 0.05), travel within 1 px. Long-tail sheets
  (cubic-bezier(0.32, 0.72, 0, 1), easeOutExpo; the last 10 to 20 % below a pixel) across the bottom and right edges,
  CFR and VFR, one taller than half the frame, one beside a white top bar: start within 0.1 ms, duration within 0.1 %,
  travel exact; the same drawers rendered by Chrome (review fixtures, not in selftest): duration within 0.3 %. Short
  accelerate exits (160 to 200 ms, scale + opacity): t50 within 1 ms; ease-in curves up to 0.09 apart can tie, then all
  are listed.
- Overshoot pops (easeOutBack, the peak between frames), CFR and VFR: badges growing from scale 0 (one 11 px from nav
  icons) and a chip from 0.6: t50 within 0.4 ms, start within 1 ms, duration within 0.6 %, easeOutBack by name or a
  custom curve within 0.011 of it with the truth inside its range. A 40 px badge 5 px from an icon, codec-damaged
  (review fixtures): custom curves within 0.06, t50 within 1 ms, durations -5 to +10 %, the truth inside every range.
- Motion over page content (text lines under it), CFR and VFR: a sheet taller than half the frame and top shades whose
  leading 76 px hold only a grab handle (easeOutQuart, easeOutQuint, easeOutExpo): start within 0.1 ms, duration within
  0.1 %, travel exact, the covered lines not listed. A sheet over a page dimming to 45 % black on its curve: the sheet
  plus one backdrop row, start within 1.1 ms, duration within 0.2 %, the custom curve within 0.01, travel within 2 px;
  the backdrop's start within 1.1 ms but its duration short under a long tail (324 ms for 460). A drawer closing across
  the bottom edge (cubic-bezier(0.32, 0.72, 0, 1)): start within 0.1 ms, t50 within 3 ms; its last 40 px are too thin to
  track, so quote the duration range (one fit read 263 ms for 400, range 250-434). Pops covering content (badges on an
  avatar's and a nav icon's corner, a dot on a card): one scale from (to) 0, start within 3 ms, t50 within 0.3 ms,
  durations -3.7 to +4.2 %, the truth inside every range; an 18 px badge on an avatar is a micro event (rerun with
  `--roi`), t50 within 1 ms but its curve loose (0.1). A scale + fade tooltip over a text line: scale from within 0.006,
  t50 within 1.3 ms, duration +7 ms, ease-out read as easeOutSine (0.017 apart).
- A colour change in place (a button's fill to a colour of the same BT.709 luma, linear), CFR and VFR, alone and with
  a badge popping beside it later: typed color, linear, confidence high, start within 2.2 ms, duration -1.0 to -1.5 %.
  Alone, CFR: such a fill inside a static 6 px border (linear), a ghost button filling and a dark button lightening
  (both ease, starting below the detection threshold): one color row each, the curve by name, confidence high, start
  within 1.2 ms, duration within 1.3 %.
- Repeats: 9 identical carousel transitions (custom curve, pixel noise): one shared fit, the curve exact, every start
  within 0.6 ms, duration +0.1 %.
- Cuts within one frame. Every slide of a 10 min deck got a keyframe (40/40) and its OCR code;
  slides sharing one template that differ only in a title word and a code (0.01 to 0.17 % of the
  160 px pixels): 8/8 keyframed, codes read, the one repeat marked. Text new at a keyframe is dated to
  the frame its slide appeared; other text is first seen up to one 0.5 s cell late. Korean OCR read
  rendered slides exactly, numbers included; subtitles were exact at >= 16 px glyph height, mixed
  at 13 to 14 px, lost at <= 10 px. Busy backgrounds can lose a whole line: check the frame.
- Speech: on-device Apple CER 3.3 to 3.7 % on clean Korean TTS, whisper large-v3-turbo 2.4 %; real
  noisy talks unmeasured; numbers get normalized ("천이백팔십사" > "1284"). Word starts are
  clamped to the voiced onset (within 2 ms in the test).
- Audio: activity edges within 9 ms, onsets within 2 ms. Sync: +38.4 ms measured for +40 ms
  (median of 3 paired onsets); positive = audio later.
