"""
run_manual.py

Manual/local run: fake alert (fixtures.py, via fake_alert.py) -> real
codebase context (github_context.py) -> one prompt -> Claude -> printed to
the CLI. For the real-alert equivalent (SNS-triggered), see handler.py.

Requires ANTHROPIC_API_KEY set in the environment.
"""

import argparse
import os

from fake_alert import build_fake_incident
from github_context import get_codebase_context_from_github
from llm_agent import diagnose_incident


def main():
    parser = argparse.ArgumentParser(description="Diagnose a fake CloudWatch alarm")
    parser.add_argument(
        "--scenario",
        default="db_timeout",
        choices=["db_timeout", "ambiguous_5xx"],
    )
    parser.add_argument("--github-owner", default=os.environ.get("GITHUB_OWNER"))
    parser.add_argument("--github-repo", default=os.environ.get("GITHUB_REPO"))
    parser.add_argument(
        "--github-ref",
        default=os.environ.get("GITHUB_REF"),
        help="branch/commit/tag to fetch codebase_context.md from (default: repo's default branch)",
    )
    args = parser.parse_args()

    ctx = build_fake_incident(args.scenario)
    print(ctx.to_llm_context())
    print("\n" + "=" * 60 + "\n")

    codebase_context = get_codebase_context_from_github(args.github_owner, args.github_repo, ref=args.github_ref)
    result = diagnose_incident(ctx, codebase_context=codebase_context)
    print(result.to_text())


if __name__ == "__main__":
    main()
