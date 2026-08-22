#!/usr/bin/env bash
# Runs agent/run_manual.py with .env loaded. Usage:
#   ./scripts/run_manual.sh [--scenario db_timeout|ambiguous_5xx]
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env
set +a

cd agent && python3 run_manual.py "$@"
