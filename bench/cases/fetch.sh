#!/bin/bash
# Downloads the originals from X into src/ (yt-dlp).
set -eu
cd "$(dirname "$0")"
mkdir -p src
get() { yt-dlp --no-warnings -q -f "bv*+ba/b" --merge-output-format mp4 -o "src/$1.%(ext)s" "$2"; }
get stephanlivera https://x.com/stephanlivera/status/2103315922098470926
get ajith https://x.com/ajith_io/status/2103449416325890146
