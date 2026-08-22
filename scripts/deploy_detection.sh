#!/usr/bin/env bash
# Deploy the W2 detection stack (infra/detection.yaml).
#
# Deliberately a separate stack from template.yaml. template.yaml belongs to
# another workstream; this one is additive so the two can be deployed, broken
# and rolled back independently. Nothing here touches it.
#
#   ./scripts/deploy_detection.sh                 # threshold from .run/threshold
#   LATENCY_THRESHOLD_MS=95 ./scripts/deploy_detection.sh
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
NAMESPACE=${NAMESPACE:-Culprit}
SENSITIVITY=${LATENCY_SENSITIVITY:-2}
EVENT_TYPE=${DISPATCH_EVENT_TYPE:-anomaly}
ALERT_EMAIL=${ALERT_EMAIL:-}
GITHUB_TOKEN=${GITHUB_TOKEN:-}

RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; DIM=$'\e[2m'; RST=$'\e[0m'
ok()  { echo "  ${GRN}ok${RST}   $*"; }
bad() { echo "  ${RED}FAIL${RST} $*"; }
warn(){ echo "  ${YEL}warn${RST} $*"; }

[ -f infra/detection.yaml ] || { bad "infra/detection.yaml not found"; exit 1; }

# --- owner/repo from the git remote, never hardcoded -----------------------
ORIGIN=$(git config --get remote.origin.url 2>/dev/null || echo "")
SLUG=$(printf '%s' "$ORIGIN" | sed -E 's#^.*github\.com[:/]##; s#\.git$##')
OWNER=${GITHUB_OWNER:-${SLUG%%/*}}
REPO=${GITHUB_REPO:-${SLUG##*/}}
[ -n "$OWNER" ] && [ -n "$REPO" ] || { bad "could not derive owner/repo from '$ORIGIN'"; exit 1; }

# --- threshold: calibrated, or explicit, but never silently guessed --------
if [ -n "${LATENCY_THRESHOLD_MS:-}" ]; then
  THRESHOLD=$LATENCY_THRESHOLD_MS
  SRC="LATENCY_THRESHOLD_MS env var"
elif [ -s .run/threshold ]; then
  THRESHOLD=$(cat .run/threshold)
  SRC=".run/threshold (from scripts/calibrate.sh)"
else
  warn "no calibration found — running scripts/calibrate.sh now"
  if [ -x scripts/calibrate.sh ] && scripts/calibrate.sh >/dev/null 2>&1 && [ -s .run/threshold ]; then
    THRESHOLD=$(cat .run/threshold); SRC="scripts/calibrate.sh (just run)"
  else
    THRESHOLD=120; SRC="${YEL}fallback default — CALIBRATE BEFORE THE DEMO${RST}"
  fi
fi

echo "deploying ${STACK} to ${REGION}"
echo "  namespace   $NAMESPACE"
echo "  threshold   ${THRESHOLD}ms   ${DIM}<- ${SRC}${RST}"
echo "  sensitivity $SENSITIVITY"
echo "  dispatch    ${OWNER}/${REPO}  event_type=${EVENT_TYPE}"
if [ -n "$GITHUB_TOKEN" ]; then echo "  token       ${GRN}set${RST} (${#GITHUB_TOKEN} chars)";
else echo "  token       ${YEL}absent${RST} — Lambda will log the payload instead of POSTing"; fi
[ -n "$ALERT_EMAIL" ] && echo "  email       $ALERT_EMAIL  (confirm the SNS subscription in your inbox)"
echo

# Parameters are passed on the command line, never written to a checked-in
# file, so the PAT never lands in git. CFN stores it NoEcho.
PARAMS=(
  "MetricNamespace=$NAMESPACE"
  "LatencyThresholdMs=$THRESHOLD"
  "LatencySensitivity=$SENSITIVITY"
  "GithubOwner=$OWNER"
  "GithubRepo=$REPO"
  "GithubToken=$GITHUB_TOKEN"
  "DispatchEventType=$EVENT_TYPE"
  "AlertEmail=$ALERT_EMAIL"
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
