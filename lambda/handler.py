"""
Entry point for the anomaly-review Lambda.

Two distinct invocation shapes hit this same function (see
docs/architecture.md section 2, and the "one deployable function, modular
internally" note in section 12):

1. SNS-triggered (event["Records"][...]["Sns"]) — a CloudWatch alarm fired.
   Runs the full pipeline: git_context -> llm_review -> notify.
2. CloudWatch custom-widget invocation (event["widgetContext"]) — the
   dashboard is rendering the widget and wants HTML back. Reads the last
   verdict this same function stashed in its own environment variables (see
   notify.store_last_verdict) and renders it — no DynamoDB/S3 needed for a
   single "latest verdict" value (see docs/architecture.md section 7).
"""

import json
import logging
import os

from git_context import get_commit_diff, get_recent_prs
from llm_review import review_diff
from notify import publish_notification, render_widget, store_last_verdict

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    if "widgetContext" in event:
        return render_widget()

    results = []
    for record in event.get("Records", []):
        sns_message = record.get("Sns", {}).get("Message")
        if not sns_message:
            continue
        alarm = json.loads(sns_message)
        results.append(_handle_alarm(alarm, context))

    return {"statusCode": 200, "handled": len(results)}


def _handle_alarm(alarm, context):
    if alarm.get("NewStateValue") != "ALARM":
        logger.info("Ignoring state change to %s", alarm.get("NewStateValue"))
        return None

    logger.info("Handling alarm: %s", alarm.get("AlarmName"))

    alarm_timestamp = alarm.get("StateChangeTime")
    owner = os.environ["GITHUB_OWNER"]
    repo = os.environ["GITHUB_REPO"]

    context_bundle = get_commit_diff(owner, repo, alarm_timestamp)
    context_bundle["recent_prs"] = get_recent_prs(owner, repo, alarm_timestamp)
    verdict = review_diff(alarm, context_bundle)

    store_last_verdict(alarm, verdict)
    publish_notification(alarm, verdict)

    return verdict
