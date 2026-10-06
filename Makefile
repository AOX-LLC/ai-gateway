# Reproducible scorecard targets. Harborline Supply Co. is fictional.
#
#   LAB_AUTO_APPROVE=yes LAB_MUTABLE_UPSTREAM=yes LAB_FLOOR_OVERRIDE=yes make scorecard
#                          run every attack against a fresh lab stack (its own Compose project, removed
#                          at the end), replay mode, no API key; rewrite docs/scorecard.{json,md} and
#                          docs/images/scorecard.svg. Needs Docker and ports 4400-4402 free.
#   make scorecard-render  re-render docs/scorecard.md, the chart and the README section from the
#                          committed JSON (no stack): for a change to how they read.
#   make scorecard-check   the static check (no stack): the committed scorecard, its rendered files
#                          and the generated lab configuration agree. Runs on every pull request.
#   LAB_AUTO_APPROVE=yes LAB_MUTABLE_UPSTREAM=yes LAB_FLOOR_OVERRIDE=yes make scorecard-verify
#                          regenerate on a stack and fail if the committed deterministic part differs.
#   make lab-config        rewrite the generated lab configuration (config/lab/).
#   make case-study        re-render docs/case-study.md from docs/case-study.template.md and the
#                          committed docs/scorecard.json (no stack): for a change to the prose, or
#                          after `make scorecard` has changed a figure.
#   make case-study-check  the static check (no stack): docs/case-study.md is what its template and
#                          the committed scorecard render to. Runs on every pull request.
#
# The lab approver and the lab upstream are test tooling that defeat or abuse a control on purpose.
# The two switches are the caller's to turn on (nothing here sets them): the runner refuses to start
# without them, and starts the tools only in the `lab` Compose profile, never against anything real.

.PHONY: scorecard scorecard-render scorecard-check scorecard-verify lab-config case-study case-study-check

scorecard:
	nice -n 19 uv run scripts/redteam/scorecard_run.py

scorecard-verify:
	nice -n 19 uv run scripts/redteam/scorecard_run.py --check

scorecard-render:
	uv run scripts/redteam/scorecard_render.py

scorecard-check:
	uv run scripts/redteam/scorecard_check.py
	uv run scripts/generate_lab_config.py --check

lab-config:
	uv run scripts/generate_lab_config.py

case-study:
	uv run scripts/case_study.py

case-study-check:
	uv run scripts/case_study.py --check
