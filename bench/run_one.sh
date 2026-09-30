#!/bin/bash
# Run one benchmark cell: ./run_one.sh TASK ARM REP
#   Final arms: a0f / a0s = model alone (--disable-slash-commands, skill hidden on disk), Opus 5.5 / Sonnet 5.5
#               bf / bs   = /video-lens invoked explicitly, Opus 5.5 / Sonnet 5.5
#               g0 / gs   = the same two conditions on Grok 4.7 through the Grok Build CLI (grok-4.7, reasoning effort xhigh)
#               w / vu    = Opus 5.5 with /watch (claude-video) or /video-use (browser-use) invoked explicitly,
#                           video-lens hidden on disk as for the model alone
#   Also: b = skills available but not named (Opus 5.5, the auto-trigger check); a0 and bstar* are earlier rounds'
#   names for the a0f and bf conditions.
# Every run: fresh working folder under $VLB_RUNS (no truth files reachable by relative path), the input copied as
# clip.mp4, no MCP servers, effort high, stream-json log for cost and tool audit.
set -euo pipefail
TASK=$1 ARM=$2 REP=$3
BENCH="$(cd "$(dirname "$0")" && pwd)"
. "$BENCH/paths.sh"
RUN_ROOT="$VLB_RUNS"
ID="$TASK-$ARM-$REP"
WORK="$RUN_ROOT/$ID/work"
LOG="$RUN_ROOT/$ID/run.jsonl"

if [ -s "$LOG" ] && grep -q -E '"type":"(result|end)"' "$LOG"; then
  echo "skip $ID (done)"; exit 0
fi

read -r VIDEO PROMPT < <(python3 - "$BENCH/tasks.json" "$TASK" <<'EOF'
import json, sys
spec = json.load(open(sys.argv[1]))
task = spec["tasks"][sys.argv[2]]
prompt = task["prompt"] + spec["suffix"] + spec["answer_schema"]
print(task["video"], json.dumps(prompt, ensure_ascii=False))
EOF
)
case "$VIDEO" in /*) SRC="$VIDEO" ;; *) SRC="$BENCH/$VIDEO" ;; esac
if [ ! -f "$SRC" ]; then
  echo "skip $ID: $SRC is missing (evalset/gen.py and hardset/gen_hard.py build the clips; orbit and gist need your own, see tasks.json)" >&2
  exit 4
fi
rm -rf "$RUN_ROOT/$ID"
mkdir -p "$WORK"
cp "$SRC" "$WORK/clip.mp4"
PROMPT=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]))' "$PROMPT")

EXTRA=()
case "$ARM" in
  a0|a0f|a0s|g0|w|vu)
    # These arms must not be able to find video-lens on disk (a no-skill run found and executed it once).
    if [ -z "$VIDEO_LENS_SKILL_DIR" ]; then
      echo "$ARM needs VIDEO_LENS_SKILL_DIR set so the skill can be hidden (see paths.sh)" >&2; exit 3
    fi
    if [ -e "$VIDEO_LENS_SKILL_LINK" ] || [ -r "$VIDEO_LENS_SKILL_DIR" ] || [ -r "$VIDEO_LENS_USER_CACHE" ]; then
      echo "$ARM needs video-lens hidden (./hide_skill.sh hide)" >&2; exit 3
    fi ;;
esac
case "$ARM" in
  a0|a0f|a0s|g0) EXTRA=(--disable-slash-commands) ;;
  bstar|bstar2|bstar3|bstar4|bf|bs|gs) PROMPT="/video-lens $PROMPT" ;;
  w) PROMPT="/watch:watch $PROMPT" ;;
  vu)
    # video-use loads only for this arm (--add-dir), never globally, and transcribes through its own ElevenLabs key.
    grep -q '^ELEVENLABS_API_KEY=.\{20,\}' "$VIDEO_USE_DIR/.env" || { echo "vu needs ELEVENLABS_API_KEY in $VIDEO_USE_DIR/.env" >&2; exit 3; }
    mkdir -p "$BENCH/arms/video-use/.claude/skills"
    ln -sfn "$VIDEO_USE_DIR" "$BENCH/arms/video-use/.claude/skills/video-use"
    EXTRA=(--add-dir "$BENCH/arms/video-use"); PROMPT="/video-use $PROMPT" ;;
  b) ;;
  *) echo "unknown arm $ARM" >&2; exit 2 ;;
esac

# Each skill run starts with an empty analysis cache so no run profits from another's work.
export VIDEO_LENS_CACHE_DIR="$RUN_ROOT/$ID/vlcache"
# Effort high for every arm. The published Opus runs got it from the author's user settings, the Sonnet runs from
# this variable; setting it here for both keeps other setups on the measured condition.
export CLAUDE_CODE_EFFORT_LEVEL=high
cd "$WORK"
START=$(date +%s)
if [ "$ARM" = g0 ] || [ "$ARM" = gs ]; then
  # Grok 4.7 through the Grok Build CLI; its MCP gateway tools are blocked to match the Claude runs.
  "$GROK_BIN" --model grok-4.7 --reasoning-effort xhigh -p "$PROMPT" --output-format streaming-json \
    --disallowed-tools use_tool,search_tool < /dev/null > "$LOG" 2> "$RUN_ROOT/$ID/stderr.txt" || true
  echo "{\"wall_s\": $(( $(date +%s) - START ))}" > "$RUN_ROOT/$ID/wall.json"
  echo "done $ID"; exit 0
fi
MODEL=claude-opus-5-5
case "$ARM" in a0s|bs) MODEL=claude-sonnet-5-5 ;; esac
claude -p "$PROMPT" --model "$MODEL" --output-format stream-json --verbose \
  --no-session-persistence --permission-mode bypassPermissions --strict-mcp-config \
  --max-budget-usd 8 ${EXTRA[@]+"${EXTRA[@]}"} < /dev/null > "$LOG" 2> "$RUN_ROOT/$ID/stderr.txt" || true
echo "{\"wall_s\": $(( $(date +%s) - START ))}" > "$RUN_ROOT/$ID/wall.json"
echo "done $ID"
