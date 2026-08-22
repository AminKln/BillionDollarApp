"""
Two responsibilities:

1. Publish the verdict to the notify SNS topic (email).
2. Persist the "latest verdict" as this Lambda's own environment variable,
   and render it as HTML when CloudWatch invokes this same function as a
   custom dashboard widget.

Why environment variables and not a database: DynamoDB/S3 were deliberately
dropped from the MVP (docs/architecture.md section 2), so the smallest
zero-new-service place to stash a single "latest verdict" value is the
function's own config, updated via lambda:UpdateFunctionConfiguration. This
is a single-slot, eventually-consistent store — fine for a demo, not a
substitute for a real datastore if this grows past the hackathon (see
section 7's implementation note).
"""

import json
import os

import boto3

sns = boto3.client("sns")
lambda_client = boto3.client("lambda")

_ENV_VAR_LIMIT = 3800  # stay under Lambda's 4KB total env var size limit


def store_last_verdict(alarm, verdict):
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not function_name:
        return  # local/test invocation — nothing to persist

    payload = {"alarm": alarm.get("AlarmName"), "verdict": verdict}
    env_vars = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env_vars["LAST_VERDICT_JSON"] = json.dumps(payload)[:_ENV_VAR_LIMIT]

    try:
        lambda_client.update_function_configuration(
            FunctionName=function_name,
            Environment={"Variables": env_vars},
        )
    except Exception as exc:  # notification path shouldn't crash the pipeline
        print(f"Could not persist last verdict for the widget: {exc}")


def publish_notification(alarm, verdict):
    topic_arn = os.environ.get("NOTIFY_TOPIC_ARN")
    if not topic_arn:
        return

    fix = verdict.get("suggested_fix", {})
    message = (
        f"Alarm: {alarm.get('AlarmName')}\n"
        f"Suspect commit: {verdict.get('suspect_commit')} "
        f"(confidence {verdict.get('confidence')})\n\n"
        f"{verdict.get('explanation')}\n\n"
        f"Suggested fix: {fix.get('summary')}\n{fix.get('diff')}\n\n"
        f"Residual risk: {fix.get('risk_note')}"
    )
    sns.publish(
        TopicArn=topic_arn,
        Subject=f"[Anomaly] {alarm.get('AlarmName')}",
        Message=message,
    )


def render_widget():
    """Returns an HTML string per the CloudWatch custom-widget contract —
    no JavaScript allowed, CSS/SVG are fine."""
    raw = os.environ.get("LAST_VERDICT_JSON")
    if not raw:
        return "<p>No anomaly reviewed yet.</p>"

    data = json.loads(raw)
    verdict = data.get("verdict", {})
    fix = verdict.get("suggested_fix", {})
    confidence_pct = round(float(verdict.get("confidence", 0)) * 100)

    return f"""
<h3>{data.get('alarm', 'Anomaly')}</h3>
<p><b>Suspect commit:</b> {verdict.get('suspect_commit')} ({confidence_pct}% confidence)</p>
<p>{verdict.get('explanation', '')}</p>
<p><b>Suggested fix:</b> {fix.get('summary', '')}</p>
<pre style="white-space: pre-wrap;">{fix.get('diff', '')}</pre>
<p><i>Residual risk: {fix.get('risk_note', '')}</i></p>
""".strip()
