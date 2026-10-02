#!/bin/bash
# One recreation run: an empty folder holding only clip.mp4, Opus 5.5 in headless Claude Code.
# usage: run_case.sh NAME SECONDS [lens|base]   (src/NAME.mp4 from fetch.sh)
#   lens  /video-lens named in the prompt; writes runs/NAME/
#   base  the model alone, no skills (--disable-slash-commands) and video-lens hidden (../hide_skill.sh hide first);
#         works in /private/tmp so it cannot see the other rebuilds; writes runs/NAME-base/
set -u
NAME=$1; SECONDS_LONG=$2; ARM=${3:-lens}
ROOT="$(cd "$(dirname "$0")" && pwd)"
TASK="clip.mp4 in this folder is a 15-second motion-graphics showreel posted on X. Reverse-engineer it and rebuild it as one self-contained file, recreation.html (a 1920x1080 page, no audio), that plays the same video: the same shots in the same order at the same times, with the same layout, colors, text and motion (delays, durations, easing). So that every frame can be rendered exactly, animate only with CSS animations or the Web Animations API, all of them starting at page load (page time 0 = video time 0); no requestAnimationFrame, timers, canvas, video, or external files, fonts or libraries (use installed system fonts close to the original)."
FINISH="Look at as many sheets and frames as the rebuild needs; nobody will answer questions, so do not ask. Finish with a table of the shots (start, end, what is on screen) for the original and for your recreation."
EXTRA=()
case "$ARM" in
  lens)
    RUN="$ROOT/runs/$NAME"; WORK="$RUN/work"
    PROMPT="/video-lens $TASK When it is built, render it with the skill's declared.mjs (--viewport 1920x1080 --render 60 $SECONDS_LONG) and copy the render to recreation.mp4 in this folder, compare it with the original using the skill, and fix the biggest differences, at most two fix rounds. $FINISH" ;;
  base)
    if [ -e "$HOME/.claude/skills/video-lens" ] || [ -r "$HOME/.cache/video-lens" ]; then
      echo "base needs video-lens hidden (../hide_skill.sh hide)" >&2; exit 3
    fi
    RUN="$ROOT/runs/$NAME-base"; WORK="/private/tmp/vlc-base/$NAME"
    EXTRA=(--disable-slash-commands)
    PROMPT="$TASK When it is built, render it to recreation.mp4 in this folder (1920x1080, 60 fps, $SECONDS_LONG s), compare it with the original, and fix the biggest differences, at most two fix rounds. $FINISH" ;;
  *) echo "unknown arm $ARM" >&2; exit 2 ;;
esac
rm -rf "$RUN" "$WORK"; mkdir -p "$RUN" "$WORK"
cp "$ROOT/src/$NAME.mp4" "$WORK/clip.mp4"
echo "$PROMPT" > "$RUN/prompt.txt"
export VIDEO_LENS_CACHE_DIR="$RUN/vlcache"
cd "$WORK"
START=$(date +%s)
claude -p "$PROMPT" --model claude-opus-5-5 --output-format stream-json --verbose \
  --no-session-persistence --permission-mode bypassPermissions --strict-mcp-config \
  --max-budget-usd 30 ${EXTRA[@]+"${EXTRA[@]}"} < /dev/null > "$RUN/log.jsonl" 2> "$RUN/stderr.txt" || true
echo "{\"wall_s\": $(( $(date +%s) - START ))}" > "$RUN/wall.json"
[ "$ARM" = base ] && cp -R "$WORK" "$RUN/work"
echo "done $NAME $ARM $(cat "$RUN/wall.json")"
