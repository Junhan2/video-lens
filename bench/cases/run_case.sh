#!/bin/bash
# One recreation run: an empty folder holding only clip.mp4, Opus 5.5 in headless Claude Code, /video-lens named.
# usage: run_case.sh NAME SECONDS   (src/NAME.mp4 from fetch.sh; writes runs/NAME/)
set -u
NAME=$1; SECONDS_LONG=$2
ROOT="$(cd "$(dirname "$0")" && pwd)"
RUN="$ROOT/runs/$NAME"; WORK="$RUN/work"
rm -rf "$RUN"; mkdir -p "$WORK"
cp "$ROOT/src/$NAME.mp4" "$WORK/clip.mp4"
PROMPT="/video-lens clip.mp4 in this folder is a 15-second motion-graphics showreel posted on X. Reverse-engineer it and rebuild it as one self-contained file, recreation.html (a 1920x1080 page, no audio), that plays the same video: the same shots in the same order at the same times, with the same layout, colors, text and motion (delays, durations, easing). So that every frame can be rendered exactly, animate only with CSS animations or the Web Animations API, all of them starting at page load (page time 0 = video time 0); no requestAnimationFrame, timers, canvas, video, or external files, fonts or libraries (use installed system fonts close to the original). When it is built, render it with the skill's declared.mjs (--viewport 1920x1080 --render 60 $SECONDS_LONG) and copy the render to recreation.mp4 in this folder, compare it with the original using the skill, and fix the biggest differences, at most two fix rounds. Look at as many sheets and frames as the rebuild needs; nobody will answer questions, so do not ask. Finish with a table of the shots (start, end, what is on screen) for the original and for your recreation."
echo "$PROMPT" > "$RUN/prompt.txt"
export VIDEO_LENS_CACHE_DIR="$RUN/vlcache"
cd "$WORK"
START=$(date +%s)
claude -p "$PROMPT" --model claude-opus-5-5 --output-format stream-json --verbose \
  --no-session-persistence --permission-mode bypassPermissions --strict-mcp-config \
  --max-budget-usd 30 < /dev/null > "$RUN/log.jsonl" 2> "$RUN/stderr.txt" || true
echo "{\"wall_s\": $(( $(date +%s) - START ))}" > "$RUN/wall.json"
echo "done $NAME $(cat "$RUN/wall.json")"
