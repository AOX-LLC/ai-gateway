#!/bin/sh
# Fail if the code phrases of the three restricted handbook documents (fictional) are
# anywhere in the servers image's filesystem. The documents are kept out of every image on
# purpose: only the setup container mounts them, read-only, at run time.
#   scripts/check_image_has_no_restricted_text.sh [image]     (default: harborline-servers)
set -eu
image="${1:-harborline-servers}"
found=$(docker run --rm --entrypoint sh "$image" -c \
  'grep -rl --exclude-dir=proc --exclude-dir=sys -e HERON-LANTERN-7 -e PELICAN-VESPER-31 -e KESTREL-TALLOW-58 / 2>/dev/null' || true)
if [ -n "$found" ]; then
  echo "restricted handbook text found in $image:" >&2
  echo "$found" >&2
  exit 1
fi
echo "no restricted handbook text in $image"
