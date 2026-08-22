#!/usr/bin/env bash
# Runs demo_compare_context.py with .env loaded and the LLM/cloudWatch
# modules on the path. Usage: ./scripts/run_demo.sh [--scenario db_timeout]
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env
set +a

PYTHONPATH="LLM:cloudWatch" python3 LLM/demo_compare_context.py "$@"
