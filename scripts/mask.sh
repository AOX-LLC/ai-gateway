# Sourced by the scripts that handle secrets.
#
# mask VALUE tells GitHub Actions to hide VALUE in the job's log. It prints nothing anywhere else:
# the workflow command is text, and run by hand it would put the secret on the screen.

mask() {
  if [ "${GITHUB_ACTIONS:-}" = "true" ] && [ -n "${1:-}" ]; then
    echo "::add-mask::$1"
  fi
  return 0
}
