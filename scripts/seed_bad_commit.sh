#!/usr/bin/env bash
# Commits the deliberately-bad demo change to infra/demo-app/bad_app.py and
# pushes it, so the LLM review step has a real diff in a real repo to react
# to (see docs/architecture.md section 12).
#
# Decide the actual bad change in advance during rehearsal (per the brief:
# don't improvise this live) and edit infra/demo-app/bad_app.py accordingly
# before running this script — it only stages/commits/pushes whatever is
# already sitting in the working tree.
#
# Usage:
#   ./scripts/seed_bad_commit.sh "removed the retry cap on the downstream call"
set -euo pipefail
cd "$(dirname "$0")/.."

MESSAGE="${1:?Usage: seed_bad_commit.sh \"<commit message describing the bad change>\"}"

git add infra/demo-app/bad_app.py
git commit -m "${MESSAGE}"
git push
