#!/usr/bin/env bash
# Wraps `sam build && sam deploy`, sourcing secrets from .env so nothing
# sensitive ends up in samconfig.toml.
#
# Usage:
#   ./scripts/deploy.sh
#
# Requires .env (copy .env.example) with at least ANTHROPIC_API_KEY,
# ALARM_TOPIC_ARN set. LOG_GROUP is optional (empty = skip log evidence --
# the app doesn't ship logs yet). GITHUB_* vars are currently unused
# (get_codebase_context_from_github() is stubbed) but still passed through
# for whenever either is re-enabled.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY in .env first}"
: "${ALARM_TOPIC_ARN:?Set ALARM_TOPIC_ARN (ARN of the existing SNS topic, e.g. culprit-alerts)}"

sam build

# --parameter-overrides rejects an explicitly empty Key= value, so only pass
# the optional ones when actually set -- the template's Default: "" covers
# the rest.
overrides=("AnthropicApiKey=${ANTHROPIC_API_KEY}" "AlarmTopicArn=${ALARM_TOPIC_ARN}")
[ -n "${LOG_GROUP:-}" ] && overrides+=("LogGroupName=${LOG_GROUP}")
[ -n "${GITHUB_OWNER:-}" ] && overrides+=("GithubOwner=${GITHUB_OWNER}")
[ -n "${GITHUB_REPO:-}" ] && overrides+=("GithubRepo=${GITHUB_REPO}")
[ -n "${GITHUB_REF:-}" ] && overrides+=("GithubRef=${GITHUB_REF}")
[ -n "${GITHUB_TOKEN:-}" ] && overrides+=("GithubToken=${GITHUB_TOKEN}")

sam deploy --parameter-overrides "${overrides[@]}"
