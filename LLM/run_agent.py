"""
run_agent.py

The real pipeline. No fixtures, no fakes. Pulls a live CloudWatch alarm,
builds its incident context, and gets a grounded root-cause hypothesis
from Claude.

Requires:
  - AWS credentials configured (aws configure, or an IAM role) with access
    to the target alarm and log group
  - ANTHROPIC_API_KEY set in the environment
"""

import argparse
from pathlib import Path

from context_builder import build_incident_context
from llm_agent import diagnose_incident

CODEBASE_CONTEXT_PATH = Path(__file__).parent / "codebase_context.md"


def main():
    parser = argparse.ArgumentParser(description="Diagnose a CloudWatch alarm")
    parser.add_argument("alarm_name")
    parser.add_argument("log_group")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    ctx = build_incident_context(args.alarm_name, args.log_group, region=args.region)
    print(ctx.to_llm_context())
    print("\n" + "=" * 60 + "\n")

    codebase_context = CODEBASE_CONTEXT_PATH.read_text() if CODEBASE_CONTEXT_PATH.exists() else ""
    result = diagnose_incident(ctx, codebase_context=codebase_context)
    print(result.to_text())


if __name__ == "__main__":
    main()
