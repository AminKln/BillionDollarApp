#!/usr/bin/env bash
# Deploy the W2 detection stack (infra/detection.yaml).
#
# Deliberately a separate stack from template.yaml. template.yaml belongs to
# another workstream; this one is additive so the two can be deployed, broken
# and rolled back independently. Nothing here touches it.
#
#   ./scripts/deploy_detection.sh
#   APP_LATENCY_THRESHOLD_SECONDS=0.5 ./scripts/deploy_detection.sh
#   GITHUB_TOKEN=ghp_xxx ./scripts/deploy_detection.sh
#   ALERT_EMAIL=you@example.com ./scripts/deploy_detection.sh
#
# GITHUB_TOKEN is optional. Without it the stack still deploys and the whole
# AWS half of the chain is still provable — the Lambda logs the payload it
# would have sent instead of POSTing it. W3 owns minting the real PAT.
set -uo pipefail
cd "$(dirname "$0")/.."

STACK=${STACK:-culprit-detection}
REGION=${REGION:-us-east-1}
NAMESPACE=${APP_NAMESPACE:-HackathonDemo}
SYSTEM_NAMESPACE=${APP_SYSTEM_NAMESPACE:-HackathonDemo/System}
DIM_NAME=${APP_DIMENSION_NAME:-App}
DIM_VALUE=${APP_DIMENSION_VALUE:-bad-app-ec2}
# 8, not 2. This default USED to be 2 and it silently overrode the template's
# own default on every deploy -- including the deploy that was supposed to fix
# the false positives. At 2 the anomaly band top sits at 0.052s, below live
# healthy traffic (0.051-0.054s), and culprit-App-Anomaly flaps permanently.
# See infra/detection.yaml's LatencySensitivity and docs/decisions.md 8.
SENSITIVITY=${LATENCY_SENSITIVITY:-8}
EVENT_TYPE=${DISPATCH_EVENT_TYPE:-anomaly}
ALERT_EMAIL=${ALERT_EMAIL:-}
GITHUB_TOKEN=${GITHUB_TOKEN:-}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}

# Secrets may be handed over in a file under $HOME instead of the environment,
# which keeps them out of shell history as well as out of git. Env wins if set.
# These paths are deliberately outside the repo -- nothing here is ever staged.
[ -z "$GITHUB_TOKEN" ] && [ -r "$HOME/.culprit-gh-token" ] && \
  GITHUB_TOKEN=$(tr -d '\r\n' < "$HOME/.culprit-gh-token")
[ -z "$ANTHROPIC_API_KEY" ] && [ -r "$HOME/.culprit-anthropic-key" ] && \
  ANTHROPIC_API_KEY=$(tr -d '\r\n' < "$HOME/.culprit-anthropic-key")

RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; DIM=$'\e[2m'; RST=$'\e[0m'
ok()  { echo "  ${GRN}ok${RST}   $*"; }
bad() { echo "  ${RED}FAIL${RST} $*"; }
warn(){ echo "  ${YEL}warn${RST} $*"; }

[ -f infra/detection.yaml ] || { bad "infra/detection.yaml not found"; exit 1; }

# --- dispatch target -------------------------------------------------------
# NOT this repo. The repository_dispatch has to land where the culprit commit
# and the responding workflow live, which is the bad app's own repo -- that is
# what lets the workflow's built-in secrets.GITHUB_TOKEN open the PR with no
# cross-repo PAT. This used to derive owner/repo from THIS repo's git remote,
# which pointed every dispatch at BillionDollarApp, where there is no workflow
# to receive it. See docs/plan.md 0.
OWNER=${GITHUB_OWNER:-Tehreem404}
REPO=${GITHUB_REPO:-bad_app_demo}

# --- threshold -------------------------------------------------------------
# SECONDS, not milliseconds: the app publishes RequestLatency with
# Unit=Seconds. Healthy is ~0.05s and the quadratic regression is ~1.1-2.1s,
# so 0.5 sits an order of magnitude clear of both. ./scripts/calibrate.sh
# re-derives it from live data and prints the override to paste here.
if [ -n "${APP_LATENCY_THRESHOLD_SECONDS:-}" ]; then
  THRESHOLD=$APP_LATENCY_THRESHOLD_SECONDS
  SRC="APP_LATENCY_THRESHOLD_SECONDS env var"
else
  # 0.34, not 0.5: this is what ./scripts/calibrate.sh derived from live
  # traffic and what the deployed culprit-App-High actually carries. Leaving
  # the old 0.5 here meant any redeploy silently walked the live alarm back.
  THRESHOLD=0.34
  SRC="calibrated against live data; matches the deployed alarm"
fi

echo "deploying ${STACK} to ${REGION}"
echo "  namespace   $NAMESPACE  ($DIM_NAME=$DIM_VALUE) + $SYSTEM_NAMESPACE"
echo "  threshold   ${THRESHOLD}s   ${DIM}<- ${SRC}${RST}"
echo "  sensitivity $SENSITIVITY"
echo "  dispatch    ${OWNER}/${REPO}  event_type=${EVENT_TYPE}"
if [ -n "$GITHUB_TOKEN" ]; then echo "  token       ${GRN}set${RST} (${#GITHUB_TOKEN} chars)";
else echo "  token       ${YEL}absent${RST} — Lambda will log the payload instead of POSTing"; fi
if [ -n "$ANTHROPIC_API_KEY" ]; then echo "  claude key  ${GRN}set${RST} (${#ANTHROPIC_API_KEY} chars) — Lambda opens the Issue if no workflow is listening";
else echo "  claude key  ${YEL}absent${RST} — no fallback agent; the response depends entirely on GitHub Actions"; fi
[ -n "$ALERT_EMAIL" ] && echo "  email       $ALERT_EMAIL  (confirm the SNS subscription in your inbox)"
echo

# Parameters are passed on the command line, never written to a checked-in
# file, so the PAT never lands in git. CFN stores it NoEcho.
PARAMS=(
  "AppNamespace=$NAMESPACE"
  "AppSystemNamespace=$SYSTEM_NAMESPACE"
  "AppDimensionName=$DIM_NAME"
  "AppDimensionValue=$DIM_VALUE"
  "AppLatencyThresholdSeconds=$THRESHOLD"
  "LatencySensitivity=$SENSITIVITY"
  "GithubOwner=$OWNER"
  "GithubRepo=$REPO"
  "GithubToken=$GITHUB_TOKEN"
  "DispatchEventType=$EVENT_TYPE"
  "AlertEmail=$ALERT_EMAIL"
  "AnthropicApiKey=$ANTHROPIC_API_KEY"
)

aws cloudformation deploy \
  --template-file infra/detection.yaml \
  --stack-name "$STACK" \
  --region "$REGION" \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides "${PARAMS[@]}"
RC=$?

if [ $RC -ne 0 ]; then
  bad "deploy failed (rc=$RC). Most recent failure events:"
  aws cloudformation describe-stack-events --stack-name "$STACK" --region "$REGION" \
    --query 'StackEvents[?contains(ResourceStatus,`FAILED`)].[LogicalResourceId,ResourceStatusReason]' \
    --output text 2>/dev/null | head -20
  exit $RC
fi

ok "stack deployed"
echo
echo "outputs:"
aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output text \
  | while IFS=$'\t' read -r k v; do printf '  %-22s %s\n' "$k" "$v"; done

echo
echo "next: ./scripts/verify_chain.sh   ${DIM}# proves alarm -> EventBridge -> Lambda -> GitHub${RST}"
