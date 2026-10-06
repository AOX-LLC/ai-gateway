#!/usr/bin/env bash
# Edit and publish the real-client clips into docs/media, but only what passes the frame check: it OCRs every
# frame it is about to publish and refuses on a hostname, an address, a token, a prompt, an internal name or
# a missing "Sample data". Run it after real-client/take.sh.
#   demo/real-client/publish.sh [--reviewed]
# --reviewed goes to the check: only for a person who has read every finding (see check-frames.ts), and it
# accepts findings in GIFs only.
set -euo pipefail
cd "$(dirname "$0")/.."
THEMES=(light dark)
KINDS=(realistic compliant)
OUT=out/real-client

candidates=()
for theme in "${THEMES[@]}"; do
  for kind in "${KINDS[@]}"; do
    nice -n 19 node edit.ts "$theme" "real-$kind"
    for file in "out/$theme/real-$kind/real-$kind".{gif,mp4,webm}; do
      [ -f "$file" ] && candidates+=("$file")
    done
  done
done

node check-frames.ts "$@" "${candidates[@]}"

mkdir -p ../docs/media
for theme in "${THEMES[@]}"; do
  for kind in "${KINDS[@]}"; do
    for file in "out/$theme/real-$kind/real-$kind".{gif,mp4,webm}; do
      [ -f "$file" ] || continue
      cp "$file" "../docs/media/real-client-$kind-$theme.${file##*.}"
    done
    node timeline-json.ts "$theme" "real-$kind" "../docs/media/real-client-$kind-$theme.timeline.json"
  done
done
for kind in "${KINDS[@]}"; do
  cp "$OUT/render-$kind.txt" "../docs/media/real-client-$kind.txt"
done
echo "published to docs/media"
