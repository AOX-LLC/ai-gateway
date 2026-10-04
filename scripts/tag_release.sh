#!/usr/bin/env bash
# Create the annotated release tag LOCALLY at a commit. It never pushes: the push is a person's
# decision, and the command to run is printed at the end.
#
#   scripts/tag_release.sh <merge commit> [VERSION]      # VERSION defaults to v0.1.0
#
# The message is docs/release-notes/<VERSION>.md. It refuses a tag that already exists, a commit
# that is not on origin/main (a release is a merged commit), and a message that is missing.
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
VERSION=${2:-v0.1.0}
[ -n "${1:-}" ] || { echo "usage: $0 <merge commit> [VERSION]" >&2; exit 2; }
SHA=$(git -C "$ROOT" rev-parse --verify "$1^{commit}")
NOTES="$ROOT/docs/release-notes/$VERSION.md"
[ -f "$NOTES" ] || { echo "no release notes at $NOTES" >&2; exit 1; }
if git -C "$ROOT" rev-parse -q --verify "refs/tags/$VERSION" > /dev/null; then
  echo "$VERSION already exists" >&2
  exit 1
fi
git -C "$ROOT" fetch -q origin main
git -C "$ROOT" merge-base --is-ancestor "$SHA" origin/main \
  || { echo "${SHA:0:12} is not on origin/main: merge first" >&2; exit 1; }
git -C "$ROOT" tag -a "$VERSION" "$SHA" -F "$NOTES"
echo "created $VERSION at ${SHA:0:12} (not pushed). To publish it:"
echo "  git push origin $VERSION"
