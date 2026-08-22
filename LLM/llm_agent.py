"""
llm_agent.py

Turns an IncidentContext digest into a grounded root-cause hypothesis using
Claude. Uses forced tool use for structured output, so the result is always
parseable — not "hope the model returns valid JSON in prose".
"""

from __future__ import annotations

from dataclasses import dataclass

import anthropic

from context_builder import IncidentContext

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You are a root-cause analysis assistant for backend incidents. \
You are given a digest of a CloudWatch alarm, its metric anomaly, and related log evidence. \
You may also be given a codebase context summary describing the service's architecture, key \
components, and known failure patterns.

Rules:
- The CloudWatch evidence (alarm, metric, logs) is the primary basis for your conclusion. \
Codebase context is background to help you interpret that evidence — it does not replace it.
- Only use the evidence provided. Do not invent log lines, timestamps, or code paths that \
are not present in the digest or the codebase context.
- Every item in supporting_evidence must point to something specific in the digest (a \
timestamp, an error message, a metric value) — not a general restatement.
- If the evidence is too thin to support a specific hypothesis, say so plainly and set \
confidence to "low" rather than guessing at a plausible-sounding cause.
- Keep the hypothesis to one or two sentences. Save detail for supporting_evidence.
"""

REPORT_TOOL = {
    "name": "report_root_cause",
    "description": "Report a root-cause hypothesis for this incident, grounded in the provided evidence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "hypothesis": {
                "type": "string",
                "description": "One or two sentence root-cause hypothesis.",
            },
            "confidence": {
                "type": "string",
                "enum": ["low", "medium", "high"],
            },
            "supporting_evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": "What in the digest this points to, e.g. 'log:14:32:04.881' or 'metric:baseline_vs_incident'.",
                        },
                        "excerpt": {
                            "type": "string",
                            "description": "The specific value, message, or timestamp from the digest.",
                        },
                    },
                    "required": ["source", "excerpt"],
                },
            },
            "suggested_action": {
                "type": "string",
                "description": "A concrete next step — what to check or do next.",
            },
        },
        "required": ["hypothesis", "confidence", "supporting_evidence", "suggested_action"],
    },
}


@dataclass
class RootCauseHypothesis:
    hypothesis: str
    confidence: str
    supporting_evidence: list[dict]
    suggested_action: str

    def to_text(self) -> str:
        lines = [f"HYPOTHESIS ({self.confidence} confidence): {self.hypothesis}", "", "Evidence:"]
        for ev in self.supporting_evidence:
            lines.append(f"  - [{ev['source']}] {ev['excerpt']}")
        lines.append("")
        lines.append(f"Suggested action: {self.suggested_action}")
        return "\n".join(lines)


def diagnose_incident(
    context: IncidentContext,
    codebase_context: str = "",
    client: anthropic.Anthropic | None = None,
) -> RootCauseHypothesis:
    client = client or anthropic.Anthropic()

    user_content = context.to_llm_context()
    if codebase_context:
        user_content = f"CODEBASE CONTEXT:\n{codebase_context}\n\n{'=' * 40}\n\n{user_content}"

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[REPORT_TOOL],
        tool_choice={"type": "tool", "name": "report_root_cause"},
        messages=[{"role": "user", "content": user_content}],
    )

    tool_use = next((block for block in response.content if block.type == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError("Model did not return a report_root_cause tool call")
    data = tool_use.input

    return RootCauseHypothesis(
        hypothesis=data["hypothesis"],
        confidence=data["confidence"],
        supporting_evidence=data["supporting_evidence"],
        suggested_action=data["suggested_action"],
    )


if __name__ == "__main__":
    from demo_local import run_scenario

    ctx = run_scenario("db_timeout")
    result = diagnose_incident(ctx)
    print(result.to_text())
