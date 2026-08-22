"""
handler.py

SNS-triggered entry point: a real CloudWatch alarm fires -> real evidence
(context_builder) + real codebase context (github_context) -> one prompt ->
Claude. No notification/dashboard step -- the verdict is logged and
returned as the invocation result. For the fake-alert equivalent, see
run_manual.py.
"""

import json
import logging
import os

from context_builder import build_incident_context
from github_context import get_codebase_context_from_github
from llm_agent import diagnose_incident

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    results = []
    for record in event.get("Records", []):
        sns_message = record.get("Sns", {}).get("Message")
        if not sns_message:
            continue
        alarm = json.loads(sns_message)
        results.append(_handle_alarm(alarm))

    return {"statusCode": 200, "handled": len(results)}


def _handle_alarm(alarm):
    if alarm.get("NewStateValue") != "ALARM":
        logger.info("Ignoring state change to %s", alarm.get("NewStateValue"))
        return None

    alarm_name = alarm.get("AlarmName")
    logger.info("Handling alarm: %s", alarm_name)

    log_group = os.environ.get("LOG_GROUP")  # optional -- app doesn't ship logs yet
    region = os.environ.get("AWS_REGION", "us-east-1")

    ctx = build_incident_context(alarm_name, log_group, region=region)

    codebase_context = get_codebase_context_from_github(
        owner=os.environ.get("GITHUB_OWNER", ""),
        repo=os.environ.get("GITHUB_REPO", ""),
        ref=os.environ.get("GITHUB_REF"),
    )

    result = diagnose_incident(ctx, codebase_context=codebase_context)
    verdict = {
        "hypothesis": result.hypothesis,
        "confidence": result.confidence,
        "supporting_evidence": result.supporting_evidence,
        "suggested_action": result.suggested_action,
    }

    logger.info("Verdict for %s: %s", alarm_name, json.dumps(verdict))
    return verdict
