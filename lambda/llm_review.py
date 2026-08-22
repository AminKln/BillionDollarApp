"""
Sends the anomaly + diff to Bedrock (Claude) and returns a structured
verdict matching docs/contracts/review-result.json — diagnosis (Tier 0) plus
a suggested fix (Tier 1, see docs/architecture.md section 6).
"""

import json
import os

import boto3

# NOTE: confirm this against whatever Bedrock model access the team's
# request (docs/architecture.md section 9, bottleneck #1) actually grants —
# this is a placeholder until that's confirmed. Overridable via the
# BEDROCK_MODEL_ID env var / template.yaml parameter without a code change.
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0")

SYSTEM_PROMPT = """You are an SRE reviewing a code diff against a production anomaly.
Diagnose the most likely culprit commit, then propose the smallest safe fix.

If a full fix isn't confidently inferable from the diff alone, suggest the
smallest safe mitigation instead (e.g. add a timeout, add a guard clause,
revert the specific hunk) and say why. Note any residual risk the fix
doesn't cover.

Respond with ONLY a JSON object matching this shape, no other text:
{
  "suspect_commit": "<sha>",
  "confidence": <0.0-1.0>,
  "explanation": "<one paragraph>",
  "suggested_fix": {
    "summary": "<one line>",
    "diff": "<unified diff or mitigation description>",
    "risk_note": "<residual risk, or empty string>"
  }
}"""


def _build_user_message(alarm, context_bundle):
    trigger = alarm.get("Trigger", {})
    anomaly_summary = {
        "alarm_name": alarm.get("AlarmName"),
        "metric": trigger.get("MetricName"),
        "namespace": trigger.get("Namespace"),
        "state_change_time": alarm.get("StateChangeTime"),
        "reason": alarm.get("NewStateReason"),
    }
    return (
        f"Anomaly:\n{json.dumps(anomaly_summary, indent=2)}\n\n"
        f"Commits since last known-good ({context_bundle['good_commit_sha']}): "
        f"{context_bundle['commits']}\n\n"
        f"Diff{' (truncated)' if context_bundle['diff_truncated'] else ''}:\n"
        f"{context_bundle['diff']}"
    )


def review_diff(alarm, context_bundle):
    client = boto3.client("bedrock-runtime")

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1500,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": _build_user_message(alarm, context_bundle)}
        ],
    }

    response = client.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps(body))
    payload = json.loads(response["body"].read())
    text = payload["content"][0]["text"]

    try:
        verdict = json.loads(text)
    except json.JSONDecodeError:
        # Model didn't return clean JSON — fall back to a minimal, honest
        # verdict rather than crashing the pipeline mid-demo.
        verdict = {
            "suspect_commit": context_bundle["commits"][-1] if context_bundle["commits"] else "unknown",
            "confidence": 0.0,
            "explanation": f"Model response was not valid JSON: {text[:500]}",
            "suggested_fix": {"summary": "", "diff": "", "risk_note": "Review manually."},
        }

    return verdict
