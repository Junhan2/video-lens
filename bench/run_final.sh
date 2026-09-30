#!/bin/bash
# Final authoritative pass for one model: every cell runs alone (no shared /tmp collisions, no load from neighbours).
# Skill arm first; then the skill is hidden for the no-skill arm and always restored on exit.
#   ./run_final.sh opus     cells in final_bf.txt, then final_a0f.txt
#   ./run_final.sh sonnet   cells in final_bs.txt, then final_a0s.txt
cd "$(dirname "$0")"
case "${1:-opus}" in
  opus) SKILL_ARM=bf BASE_ARM=a0f ;;
  sonnet) SKILL_ARM=bs BASE_ARM=a0s ;;
  *) echo "usage: $0 opus|sonnet" >&2; exit 2 ;;
esac
trap './hide_skill.sh show >/dev/null 2>&1' EXIT
while read -r t a r; do ./run_one.sh "$t" "$a" "$r"; done < "final_$SKILL_ARM.txt"
./hide_skill.sh hide || exit 1
while read -r t a r; do ./run_one.sh "$t" "$a" "$r"; done < "final_$BASE_ARM.txt"
./hide_skill.sh show
echo all-done
