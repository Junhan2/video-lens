# Paths shared by run_one.sh and hide_skill.sh. Override any of them from the environment.
# One folder per run: <task>-<arm>-<rep>/{work/clip.mp4, run.jsonl, stderr.txt, wall.json, vlcache/}.
VLB_RUNS="${VLB_RUNS:-$HOME/vlb-runs}"
# Folder that holds the skill's files (your video-lens checkout). hide_skill.sh makes it unreadable for the no-skill arm.
VIDEO_LENS_SKILL_DIR="${VIDEO_LENS_SKILL_DIR:-}"
# Symlink through which Claude Code loads the skill; parked at VLB_PARKED_LINK while hidden.
VIDEO_LENS_SKILL_LINK="${VIDEO_LENS_SKILL_LINK:-$HOME/.claude/skills/video-lens}"
VLB_PARKED_LINK="${VLB_PARKED_LINK:-$HOME/.vlb-hidden-skill-link}"
# The skill's shared cache (compiled Swift helpers); also hidden for the no-skill arm.
VIDEO_LENS_USER_CACHE="${VIDEO_LENS_USER_CACHE:-$HOME/.cache/video-lens}"
# Your video-use clone (https://github.com/browser-use/video-use) with ELEVENLABS_API_KEY in its .env, for the vu arm.
VIDEO_USE_DIR="${VIDEO_USE_DIR:-$HOME/Developer/video-use}"
# The Grok Build CLI binary, for the g0 and gs arms.
GROK_BIN="${GROK_BIN:-$HOME/.grok/bin/grok}"
