"""
demo_compare_context.py

Runs the real LLM reasoning step twice against the same fixture CloudWatch
scenario — once with codebase_context.md included, once without — so you
can see whether it actually changes the model's output before deciding
it's worth maintaining.

Defaults to the "ambiguous_5xx" scenario, not "db_timeout". db_timeout's
log line already names the exact component and exception type, so
codebase context can't add anything there. ambiguous_5xx's log line is
generic (a 502 with no component-specific detail) — that's the case
codebase context is meant to help with: disambiguating using the known
failure patterns in codebase_context.md when the raw evidence alone
doesn't spell out the answer.

Requires a real ANTHROPIC_API_KEY in the environment. Uses the real
anthropic.Anthropic() client — no fake client here. The CloudWatch side is
still the offline fixture, since there's no real AWS app yet.
"""

import argparse
from pathlib import Path

from demo_local import run_scenario
from llm_agent import diagnose_incident

CODEBASE_CONTEXT_PATH = Path(__file__).parent / "codebase_context.md"

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="ambiguous_5xx", choices=["db_timeout", "ambiguous_5xx"])
    args = parser.parse_args()

    ctx = run_scenario(args.scenario)
    codebase_context = CODEBASE_CONTEXT_PATH.read_text() if CODEBASE_CONTEXT_PATH.exists() else ""

    print(f"Scenario: {args.scenario}\n")
    print("WITHOUT codebase context:\n")
    without = diagnose_incident(ctx)
    print(without.to_text())

    print("\n" + "=" * 60 + "\n")

    print("WITH codebase context:\n")
    with_ctx = diagnose_incident(ctx, codebase_context=codebase_context)
    print(with_ctx.to_text())
