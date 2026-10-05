# Reproducible scorecard targets. Harborline Supply Co. is fictional.
#
#   LAB_AUTO_APPROVE=yes LAB_MUTABLE_UPSTREAM=yes make scorecard
#                          run every attack against a fresh lab stack (its own Compose project, removed
#                          at the end), replay mode, no API key; rewrite docs/scorecard.{json,md} and
#                          docs/images/scorecard.svg. Needs Docker and ports 4400-4402 free.
#   make scorecard-check   the static check (no stack): the committed scorecard, its rendered files
#                          and the generated lab configuration agree. Runs on every pull request.
#   LAB_AUTO_APPROVE=yes LAB_MUTABLE_UPSTREAM=yes make scorecard-verify
#                          regenerate on a stack and fail if the committed deterministic part differs.
#   make lab-config        rewrite the generated lab configuration (config/lab/).
#
# The lab approver and the lab upstream are test tooling that defeat or abuse a control on purpose.
# The two switches are the caller's to turn on (nothing here sets them): the runner refuses to start
# without them, and starts the tools only in the `lab` Compose profile, never against anything real.

.PHONY: scorecard scorecard-check scorecard-verify lab-config

scorecard:
	nice -n 19 uv run scripts/redteam/scorecard_run.py

scorecard-verify:
	nice -n 19 uv run scripts/redteam/scorecard_run.py --check

scorecard-check:
	uv run scripts/redteam/scorecard_check.py
	uv run scripts/generate_lab_config.py --check

lab-config:
	uv run scripts/generate_lab_config.py
