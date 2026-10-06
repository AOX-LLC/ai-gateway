#!/usr/bin/env bash
# Copy the finished media into docs/media, but only what passes the frame check: it OCRs every frame of
# what it is about to publish and refuses on a hostname, an address, a token, a prompt, an internal name
# or a missing "Sample data". Run it after record.sh and `node edit.ts light` / `node edit.ts dark`.
#   demo/publish.sh [--reviewed]
# --reviewed goes to the check: only for a person who has read every finding (see check-frames.ts).
set -euo pipefail
cd "$(dirname "$0")"
THEMES=(light dark)
EXTENSIONS=(gif mp4 webm)

node cards.ts dark > /dev/null
candidates=(out/social-dark.png)
for theme in "${THEMES[@]}"; do
  for extension in "${EXTENSIONS[@]}"; do
    candidates+=("out/$theme/dashboard.$extension")
  done
done

node check-frames.ts "$@" "${candidates[@]}"

mkdir -p ../docs/media
for theme in "${THEMES[@]}"; do
  for extension in "${EXTENSIONS[@]}"; do
    cp "out/$theme/dashboard.$extension" "../docs/media/dashboard-$theme.$extension"
  done
done
for theme in "${THEMES[@]}"; do
  node timeline-json.ts "$theme" "" "../docs/media/dashboard-$theme.timeline.json"
done
cp out/social-dark.png ../docs/media/social-preview.png
echo "published to docs/media"
