"""
lambda_handler.py

Entry point for running context_builder as a Lambda triggered by an SNS
topic that a CloudWatch Alarm's actions publish to.
"""

import json
import os
from pathlib import Path

from context_builder import build_incident_context
from llm_agent import diagnose_incident

LOG_GROUP = os.environ["LOG_GROUP"]  # one log group per function deployment, for now
REGION = os.environ.get("AWS_REGION", "us-east-1")
CODEBASE_CONTEXT_PATH = Path(__file__).parent / "codebase_context.md"


def handler(event, context):
    codebase_context = CODEBASE_CONTEXT_PATH.read_text() if CODEBASE_CONTEXT_PATH.exists() else ""

    for record in event["Records"]:
        message = json.loads(record["Sns"]["Message"])
        alarm_name = message["AlarmName"]

        ctx = build_incident_context(alarm_name, LOG_GROUP, region=REGION)
        result = diagnose_incident(ctx, codebase_context=codebase_context)

        # swap this for: send to Slack, write to S3, open a ticket, etc.
        print(result.to_text())

    return {"statusCode": 200}
