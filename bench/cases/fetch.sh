#!/bin/bash
# Downloads the three originals from X into src/ (yt-dlp), and cuts GPT-6 Astra's half out of the side-by-side post.
set -eu
cd "$(dirname "$0")"
mkdir -p src
get() { yt-dlp --no-warnings -q -f "bv*+ba/b" --merge-output-format mp4 -o "src/$1.%(ext)s" "$2"; }
get stephanlivera https://x.com/stephanlivera/status/2103315922098470926
get ajith https://x.com/ajith_io/status/2103449416325890146
get shneural https://x.com/shneural/status/2103151003272962130
# The post shows Opus 5.5's reel, a title card, then GPT-6 Astra's reel from 16.8 s (the cut ffmpeg's scene score finds).
ffmpeg -v error -y -ss 16.8 -i src/shneural.mp4 -t 15.09 -fps_mode cfr -r 60 -c:v libx264 -crf 14 -preset slow \
  -pix_fmt yuv420p -c:a aac -b:a 192k src/astra.mp4
