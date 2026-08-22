"""
demo_llm_reasoning.py

Full pipeline: fixture CloudWatch scenario -> IncidentContext -> LLM
root-cause call, using an offline fake Anthropic client so this runs with
no API key. Reads codebase_context.md the same way run_agent.py and
lambda_handler.py do, so this tests the real file-reading path — not a
hardcoded stand-in.

Swap build_fake_anthropic_client(...) for a real anthropic.Anthropic()
(or just omit the client= arg) once you have a key.
"""

from pathlib import Path

from demo_local import run_scenario
from llm_agent import diagnose_incident
from llm_fixtures import FAKE_DB_TIMEOUT_DIAGNOSIS, build_fake_anthropic_client

CODEBASE_CONTEXT_PATH = Path(__file__).parent / "codebase_context.md"

if __name__ == "__main__":
    ctx = run_scenario("db_timeout")
    fake_client = build_fake_anthropic_client(FAKE_DB_TIMEOUT_DIAGNOSIS)

    codebase_context = CODEBASE_CONTEXT_PATH.read_text() if CODEBASE_CONTEXT_PATH.exists() else ""

    result = diagnose_incident(ctx, codebase_context=codebase_context, client=fake_client)
    print(result.to_text())
