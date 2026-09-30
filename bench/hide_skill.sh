#!/bin/bash
# hide: make video-lens unreachable for the no-skill arm; show: restore it. Only touches the paths named in paths.sh.
set -euo pipefail
BENCH="$(cd "$(dirname "$0")" && pwd -P)"
. "$BENCH/paths.sh"
if [ -z "$VIDEO_LENS_SKILL_DIR" ] || [ ! -d "$VIDEO_LENS_SKILL_DIR" ]; then
  echo "set VIDEO_LENS_SKILL_DIR to the folder that holds the skill's files (your video-lens checkout)" >&2; exit 2
fi
case "${1:-}" in
  hide)
    # The bench must stay readable while the skill folder is locked, so it cannot live inside that folder.
    SKILL_REAL="$(realpath "$VIDEO_LENS_SKILL_DIR")"
    case "$BENCH/" in "$SKILL_REAL"/*)
      echo "this bench folder is inside $VIDEO_LENS_SKILL_DIR; copy it elsewhere first (see README)" >&2; exit 4 ;;
    esac
    [ -L "$VIDEO_LENS_SKILL_LINK" ] && mv "$VIDEO_LENS_SKILL_LINK" "$VLB_PARKED_LINK"
    chmod 000 "$VIDEO_LENS_SKILL_DIR"
    [ -d "$VIDEO_LENS_USER_CACHE" ] && chmod 000 "$VIDEO_LENS_USER_CACHE"
    echo hidden ;;
  show)
    chmod 755 "$VIDEO_LENS_SKILL_DIR"
    [ -d "$VIDEO_LENS_USER_CACHE" ] && chmod 755 "$VIDEO_LENS_USER_CACHE"
    [ -L "$VLB_PARKED_LINK" ] && mv "$VLB_PARKED_LINK" "$VIDEO_LENS_SKILL_LINK"
    echo shown ;;
  *) echo "usage: $0 hide|show" >&2; exit 2 ;;
esac
