#!/usr/bin/env bash
# Flips a chaos toggle on the demo app.
#
# Usage:
#   ./scripts/trigger_chaos.sh latency on
#   ./scripts/trigger_chaos.sh errors off
#
# Override the target host with DEMO_APP_HOST, e.g.:
#   DEMO_APP_HOST=http://<ec2-public-ip>:8000 ./scripts/trigger_chaos.sh latency on
set -euo pipefail

KIND="${1:?Usage: trigger_chaos.sh <latency|errors> <on|off>}"
STATE="${2:?Usage: trigger_chaos.sh <latency|errors> <on|off>}"
HOST="${DEMO_APP_HOST:-http://localhost:8000}"

curl -sf "${HOST}/chaos/${KIND}/${STATE}"
echo
