#!/usr/bin/env bash
# Wraps `sam build && sam deploy`, sourcing secrets from .env so nothing
# sensitive ends up in samconfig.toml.
#
# Usage:
#   ./scripts/deploy.sh
#
# Requires .env (copy .env.example) with at least GITHUB_TOKEN set, plus
# GITHUB_OWNER, GITHUB_REPO, NOTIFY_EMAIL as either env vars or .env entries.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${GITHUB_TOKEN:?Set GITHUB_TOKEN in .env first}"
: "${GITHUB_OWNER:?Set GITHUB_OWNER (org/user for the demo repo)}"
: "${GITHUB_REPO:?Set GITHUB_REPO (demo repo name)}"
: "${NOTIFY_EMAIL:?Set NOTIFY_EMAIL (address for the SNS verdict notifications)}"

sam build

sam deploy \
  --parameter-overrides \
    "GithubOwner=${GITHUB_OWNER}" \
    "GithubRepo=${GITHUB_REPO}" \
    "GithubToken=${GITHUB_TOKEN}" \
    "NotifyEmail=${NOTIFY_EMAIL}"
