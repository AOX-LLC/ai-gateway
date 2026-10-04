#!/usr/bin/env bash
# Smoke test of a release: build the stack from a CLEAN CLONE of a commit (nothing from this working
# tree, no .env, no cache of untracked files), bring it up, and run the checks. Run it for the
# commit that will be tagged, before the tag is pushed.
#
#   scripts/release_smoke.sh [REF]        # REF: a commit, branch or tag; default HEAD
#
# It uses its own Compose project (ai-gateway-smoke) on 04's ports, so the real stack must be down
# (`docker compose ps` shows nothing), takes the shared Docker lock if you run it under `flock`, and
# removes the stack, its volume and the clone when it ends. The checks play the approver, which the
# caller must allow by running this with LAB_AUTO_APPROVE=yes in front (the fictional demo stack only;
# no script sets that switch itself). Prints no secret.
set -euo pipefail
[ "${LAB_AUTO_APPROVE:-}" = "yes" ] || { echo "release_smoke: run it with LAB_AUTO_APPROVE=yes in front" >&2; exit 2; }
ROOT=$(git rev-parse --show-toplevel)
SHA=$(git -C "$ROOT" rev-parse --verify "${1:-HEAD}^{commit}")
WORK=$(mktemp -d)
export COMPOSE_PROJECT_NAME=${SMOKE_PROJECT:-ai-gateway-smoke}
case "$COMPOSE_PROJECT_NAME" in
  ai-gateway|ai-gateway-demo|ai-gateway-verify) echo "SMOKE_PROJECT must not be a project that holds data: cleanup removes its volumes" >&2; exit 1 ;;
esac
cleanup() {
  (cd "$WORK/clone" 2>/dev/null && docker compose down -v --remove-orphans >/dev/null 2>&1) || true
  rm -rf "$WORK"
}
trap cleanup EXIT
if [ -n "$(docker ps -q --filter "publish=4401" --filter "publish=4402")" ]; then
  echo "something already publishes 4401 or 4402: stop that stack first" >&2
  exit 1
fi
echo "== clean clone of ${SHA:0:12}"
git clone -q --no-hardlinks "$ROOT" "$WORK/clone"
git -C "$WORK/clone" checkout -q --detach "$SHA"
cd "$WORK/clone"
test -z "$(git status --porcelain --ignored)" || { echo "the clone is not clean" >&2; exit 1; }
python3 scripts/init_env.py > /dev/null
echo "== build and start"
if ! docker compose up -d --build --wait; then
  echo "== the stack did not come up; the one-shots' logs:" >&2
  docker compose logs --no-color --tail 40 telemetry-setup policy-setup servers-setup migrate >&2 || true
  exit 1
fi
scripts/check_image_has_no_restricted_text.sh harborline-servers
echo "== the Harborline scenarios and the simulator"
RUNNER_TEMP="$WORK" scripts/run_harborline_check.sh
echo "== the export-every-customer acceptance test"
scripts/run_redteam_check.sh
echo "PASS  the release smoke test of ${SHA:0:12}"
