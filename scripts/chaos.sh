#!/usr/bin/env bash
# Flip a chaos knob on the REAL bad app (Tehreem404/bad_app_demo, running on
# i-091814f7a41456cb0).
#
# The app does NOT expose /chaos/<kind>/<state> -- that route does not exist.
# Its only HTTP surface is /, /healthz and /chaos/status. Chaos state lives in
# SSM Parameter Store and the app re-reads it on every request, so flipping a
# parameter takes effect on the very next request with no redeploy and no SSH.
#
#   ./scripts/chaos.sh cpu on        <- USE THIS ONE for the demo. see below.
#   ./scripts/chaos.sh latency on
#   ./scripts/chaos.sh errors on
#   ./scripts/chaos.sh status
#   ./scripts/chaos.sh off           <- all three off. always finish with this.
#
# WHICH KNOB, AND WHY IT MATTERS:
#
#   latency=on  -> time.sleep(random.uniform(1.5, 3.0))
#                 Pure sleep. Latency rises ~40x but the box does no work, so
#                 cpu_usage_active does not move. The evidence then reads
#                 "requests got slower" and nothing else -- indistinguishable
#                 from a slow downstream dependency. That is an alert, not a
#                 diagnosis.
#
#   cpu=on      -> _burn_cpu(2.0): a real sha256 loop for two seconds.
#                 Latency rises the same ~40x AND cpu_usage_active climbs on
#                 HackathonDemo/System. Same endpoint, same request, now burns
#                 CPU it did not burn before. That is a code regression with
#                 host-level corroboration, which is the story the demo is
#                 actually about.
#
# So the demo trigger is cpu, not latency, even though the alarm that fires is
# a latency alarm. cpu=on moves latency too -- it just also leaves fingerprints.
set -euo pipefail

PREFIX="${CHAOS_PARAM_PREFIX:-/hackathon-demo/chaos}"
HOST="${APP_HOST:-http://54.205.9.164:8000}"

flip() {
  aws ssm put-parameter --name "${PREFIX}/$1" --value "$2" --type String --overwrite >/dev/null
  echo "  ${PREFIX}/$1 = $2"
}

case "${1:-status}" in
  status)
    echo "SSM (source of truth):"
    aws ssm get-parameters --names "${PREFIX}/latency" "${PREFIX}/cpu" "${PREFIX}/errors" \
      --query 'Parameters[].[Name,Value]' --output text 2>/dev/null | sed 's/^/  /' || true
    missing=$(aws ssm get-parameters --names "${PREFIX}/latency" "${PREFIX}/cpu" "${PREFIX}/errors" \
      --query 'InvalidParameters' --output text 2>/dev/null || true)
    if [ -n "$missing" ] && [ "$missing" != "None" ]; then
      echo "  !! MISSING: $missing"
      echo "  !! the app falls back to 'off' for any missing parameter, which means"
      echo "  !! chaos CANNOT BE TURNED ON until these exist. run: make app-bootstrap"
    fi
    echo "app (what it actually sees):"
    curl -sf --max-time 8 "${HOST}/chaos/status" | sed 's/^/  /' && echo || echo "  unreachable"
    ;;
  off)
    flip latency off; flip cpu off; flip errors off
    echo "all chaos off."
    ;;
  latency|cpu|errors)
    state="${2:?usage: chaos.sh <latency|cpu|errors> <on|off>}"
    flip "$1" "$state"
    ;;
  *)
    echo "usage: chaos.sh <latency|cpu|errors> <on|off> | status | off" >&2
    exit 1
    ;;
esac
