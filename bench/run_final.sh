#!/bin/bash
# Final authoritative pass: every cell runs alone (no shared /tmp collisions, no load from neighbours).
# Skill arm first; then the skill is hidden for the other arms and always restored on exit.
#   ./run_final.sh opus     cells in final_bf.txt, then final_a0f.txt
#   ./run_final.sh sonnet   cells in final_bs.txt, then final_a0s.txt
#   ./run_final.sh grok     cells in final_gs.txt, then final_g0.txt
#   ./run_final.sh skills   cells in final_w.txt (/watch), then final_vu.txt (video-use), both with video-lens hidden
cd "$(dirname "$0")"
case "${1:-opus}" in
  opus) SKILL_ARMS=bf HIDDEN_ARMS=a0f ;;
  sonnet) SKILL_ARMS=bs HIDDEN_ARMS=a0s ;;
  grok) SKILL_ARMS=gs HIDDEN_ARMS=g0 ;;
  skills) SKILL_ARMS="" HIDDEN_ARMS="w vu" ;;
  *) echo "usage: $0 opus|sonnet|grok|skills" >&2; exit 2 ;;
esac
trap './hide_skill.sh show >/dev/null 2>&1' EXIT
for arm in $SKILL_ARMS; do
  while read -r t a r; do ./run_one.sh "$t" "$a" "$r"; done < "final_$arm.txt"
done
./hide_skill.sh hide || exit 1
for arm in $HIDDEN_ARMS; do
  while read -r t a r; do ./run_one.sh "$t" "$a" "$r"; done < "final_$arm.txt"
done
./hide_skill.sh show
echo all-done
