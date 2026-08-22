"""
cloudwatch_context.py

Pulls REAL diagnostic context from CloudWatch for a fired alarm: the alarm's
own definition, metric datapoints around the trigger, EC2 instance metadata
(if the alarm concerns an EC2-hosted app), and recent log lines (if a log
group is configured and reachable).

Every field is either real AWS data or an explicit note saying why it
couldn't be fetched (missing permission, no log group configured, alarm
doesn't reference EC2, etc.) -- never a fabricated placeholder. Callers
should surface those notes to the LLM rather than silently dropping them.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

DEFAULT_LOOKBACK_MINUTES = 30
DEFAULT_LOOKAHEAD_MINUTES = 5
DEFAULT_LOG_SAMPLE_LIMIT = 25


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------

@dataclass
class AlarmDefinition:
    name: str
    description: Optional[str]
    namespace: str
    metric_name: str
    dimensions: dict[str, str]
    statistic: str
    comparison_operator: str
    threshold: Optional[float]
    is_anomaly_detection: bool
    state_value: str
    state_reason: str
    trigger_time: datetime
    period_seconds: int


@dataclass
class MetricWindow:
    namespace: str
    metric_name: str
    dimensions: dict[str, str]
    statistic: str
    window_start: datetime
    window_end: datetime
    datapoints: list[dict]  # sorted [{"timestamp": iso, "value": float}, ...]
    note: Optional[str] = None


@dataclass
class Ec2InstanceInfo:
    instance_id: str
    instance_type: Optional[str] = None
    ami_id: Optional[str] = None
    launch_time: Optional[datetime] = None
    uptime: Optional[str] = None
    state: Optional[str] = None
    availability_zone: Optional[str] = None
    name_tag: Optional[str] = None
    note: Optional[str] = None


@dataclass
class LogEvidence:
    log_group: Optional[str]
    entries: list[dict]  # [{"timestamp": ..., "message": ...}, ...]
    note: Optional[str] = None


@dataclass
class CloudWatchContext:
    alarm: AlarmDefinition
    metric: MetricWindow
    instance: Optional[Ec2InstanceInfo]
    logs: LogEvidence
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# SNS / alarm-name input handling
# ---------------------------------------------------------------------------

def _normalize_offset(iso_ts: str) -> str:
    """CloudWatch's StateChangeTime uses a +HHMM offset (no colon), e.g.
    '...788+0000' -- portable across Python versions older than 3.11's more
    lenient fromisoformat() by inserting the colon it expects."""
    if len(iso_ts) >= 5 and iso_ts[-5] in "+-" and iso_ts[-4:].isdigit():
        return f"{iso_ts[:-2]}:{iso_ts[-2:]}"
    return iso_ts


def parse_alarm_message(raw) -> dict:
    """Accepts a raw SNS envelope, a bare alarm-state-change message (dict or
    JSON string), or a plain alarm name string -- returns the alarm-message
    dict shape (docs/contracts/alarm-sns-message.json)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {"AlarmName": raw}

    if "Records" in raw:  # full SNS event envelope
        message = raw["Records"][0]["Sns"]["Message"]
        return json.loads(message) if isinstance(message, str) else message

    return raw


# ---------------------------------------------------------------------------
# collectors
# ---------------------------------------------------------------------------

def get_alarm_definition(alarm_name: str, region: str, client=None, message: Optional[dict] = None) -> AlarmDefinition:
    """`message`, if given, is the parsed alarm-state-change payload (see
    parse_alarm_message). Its NewStateValue/NewStateReason/StateChangeTime
    win over the live describe_alarms state -- describe_alarms only ever
    reflects the *current* state, which can already have moved on (this
    alarm flaps around its anomaly band in practice) from the incident the
    SNS message actually describes. Everything else (namespace, metric,
    dimensions, threshold shape) still comes from the live definition."""
    cw = client or boto3.client("cloudwatch", region_name=region)
    resp = cw.describe_alarms(AlarmNames=[alarm_name])
    alarms = resp.get("MetricAlarms", [])
    if not alarms:
        raise ValueError(f"No CloudWatch alarm found named {alarm_name!r} in {region}")
    a = alarms[0]

    is_anomaly = "Metrics" in a and a["Metrics"]
    if is_anomaly:
        metric_stat_entry = next((m for m in a["Metrics"] if "MetricStat" in m), None)
        if metric_stat_entry is None:
            raise ValueError(f"Alarm {alarm_name!r} uses a metric-math expression with no MetricStat component")
        stat = metric_stat_entry["MetricStat"]
        namespace = stat["Metric"]["Namespace"]
        metric_name = stat["Metric"]["MetricName"]
        dimensions = {d["Name"]: d["Value"] for d in stat["Metric"].get("Dimensions", [])}
        statistic = stat["Stat"]
        period = stat["Period"]
        threshold = _latest_dynamic_threshold(a.get("StateReasonData"))
    else:
        namespace = a["Namespace"]
        metric_name = a["MetricName"]
        dimensions = {d["Name"]: d["Value"] for d in a.get("Dimensions", [])}
        statistic = a.get("Statistic", "Average")
        period = a["Period"]
        threshold = a.get("Threshold")

    state_value = a["StateValue"]
    state_reason = a.get("StateReason", "")
    trigger_time = a["StateUpdatedTimestamp"]
    if message and message.get("NewStateValue"):
        state_value = message["NewStateValue"]
        state_reason = message.get("NewStateReason", state_reason)
        trigger_time = datetime.fromisoformat(_normalize_offset(message["StateChangeTime"]))

    return AlarmDefinition(
        name=a["AlarmName"],
        description=a.get("AlarmDescription"),
        namespace=namespace,
        metric_name=metric_name,
        dimensions=dimensions,
        statistic=statistic,
        comparison_operator=a["ComparisonOperator"],
        threshold=threshold,
        is_anomaly_detection=bool(is_anomaly),
        state_value=state_value,
        state_reason=state_reason,
        trigger_time=trigger_time,
        period_seconds=period,
    )


def _latest_dynamic_threshold(state_reason_data: Optional[str]) -> Optional[float]:
    """For anomaly-detection alarms there's no fixed Threshold -- the band
    moves per-datapoint. Best-effort: pull the threshold CloudWatch actually
    evaluated against for the most recent datapoint out of StateReasonData."""
    if not state_reason_data:
        return None
    try:
        data = json.loads(state_reason_data)
        evaluated = data.get("evaluatedDatapoints") or []
        return evaluated[0]["threshold"] if evaluated else None
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None


def get_metric_window(
    alarm: AlarmDefinition,
    region: str,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    lookahead_minutes: int = DEFAULT_LOOKAHEAD_MINUTES,
    client=None,
) -> MetricWindow:
    cw = client or boto3.client("cloudwatch", region_name=region)

    window_start = alarm.trigger_time - timedelta(minutes=lookback_minutes)
    window_end = alarm.trigger_time + timedelta(minutes=lookahead_minutes)
    dims = [{"Name": k, "Value": v} for k, v in alarm.dimensions.items()]

    note = None
    datapoints: list[dict] = []
    try:
        resp = cw.get_metric_statistics(
            Namespace=alarm.namespace,
            MetricName=alarm.metric_name,
            Dimensions=dims,
            StartTime=window_start,
            EndTime=window_end,
            Period=max(alarm.period_seconds, 60),
            Statistics=[alarm.statistic, "Maximum"] if alarm.statistic != "Maximum" else ["Maximum"],
        )
        points = sorted(resp["Datapoints"], key=lambda d: d["Timestamp"])
        datapoints = [
            {
                "timestamp": p["Timestamp"].isoformat(),
                "value": p.get(alarm.statistic, p.get("Maximum")),
                "max": p.get("Maximum"),
                "unit": p.get("Unit"),
            }
            for p in points
        ]
        if not datapoints:
            note = "get_metric_statistics returned no datapoints for this window"
    except ClientError as exc:
        note = f"could not fetch metric datapoints: {exc.response['Error']['Code']}: {exc.response['Error']['Message']}"

    return MetricWindow(
        namespace=alarm.namespace,
        metric_name=alarm.metric_name,
        dimensions=alarm.dimensions,
        statistic=alarm.statistic,
        window_start=window_start,
        window_end=window_end,
        datapoints=datapoints,
        note=note,
    )


def resolve_ec2_instance_id(alarm: AlarmDefinition) -> Optional[str]:
    """Prefer an explicit INSTANCE_ID env var (the alarm's metric dimensions
    rarely include one -- e.g. this project's alarms key off an `App`
    dimension, not `InstanceId`); fall back to a literal InstanceId
    dimension if the alarm happens to have one."""
    explicit = os.environ.get("INSTANCE_ID")
    if explicit:
        return explicit
    return alarm.dimensions.get("InstanceId")


def get_ec2_instance_info(instance_id: str, region: str, client=None) -> Ec2InstanceInfo:
    ec2 = client or boto3.client("ec2", region_name=region)
    try:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        reservations = resp.get("Reservations", [])
        if not reservations or not reservations[0]["Instances"]:
            return Ec2InstanceInfo(instance_id=instance_id, note="instance ID not found in this account/region")
        inst = reservations[0]["Instances"][0]
        launch_time = inst["LaunchTime"]
        uptime = str(datetime.now(timezone.utc) - launch_time)
        name_tag = next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), None)
        return Ec2InstanceInfo(
            instance_id=instance_id,
            instance_type=inst.get("InstanceType"),
            ami_id=inst.get("ImageId"),
            launch_time=launch_time,
            uptime=uptime,
            state=inst.get("State", {}).get("Name"),
            availability_zone=inst.get("Placement", {}).get("AvailabilityZone"),
            name_tag=name_tag,
        )
    except ClientError as exc:
        return Ec2InstanceInfo(
            instance_id=instance_id,
            note=f"could not fetch instance metadata: {exc.response['Error']['Code']}: {exc.response['Error']['Message']}",
        )


def _run_logs_insights_query(logs_client, log_group, query, start, end, timeout_s=30) -> list[dict]:
    start_resp = logs_client.start_query(
        logGroupName=log_group,
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()),
        queryString=query,
    )
    query_id = start_resp["queryId"]

    elapsed = 0
    result = None
    while elapsed < timeout_s:
        result = logs_client.get_query_results(queryId=query_id)
        if result["status"] in ("Complete", "Failed", "Cancelled"):
            break
        time.sleep(1)
        elapsed += 1
    else:
        logs_client.stop_query(queryId=query_id)
        raise TimeoutError(f"Logs Insights query timed out after {timeout_s}s")

    if result is None or result["status"] != "Complete":
        raise RuntimeError(f"Logs Insights query ended with status {result['status'] if result else 'Unknown'}")

    return [{col["field"]: col["value"] for col in row} for row in result["results"]]


def get_log_evidence(
    log_group: Optional[str],
    window_start: datetime,
    window_end: datetime,
    region: str,
    sample_limit: int = DEFAULT_LOG_SAMPLE_LIMIT,
    client=None,
) -> LogEvidence:
    if not log_group:
        return LogEvidence(
            log_group=None,
            entries=[],
            note="no LOG_GROUP configured -- skipping log evidence (set the LOG_GROUP env var if the app logs to CloudWatch Logs)",
        )

    logs_client = client or boto3.client("logs", region_name=region)
    query = (
        "fields @timestamp, @message "
        "| sort @timestamp desc "
        f"| limit {sample_limit}"
    )
    try:
        rows = _run_logs_insights_query(logs_client, log_group, query, window_start, window_end)
        entries = [{"timestamp": r.get("@timestamp"), "message": r.get("@message")} for r in rows]
        note = None if entries else f"query returned no log lines in the window for {log_group!r}"
        return LogEvidence(log_group=log_group, entries=entries, note=note)
    except ClientError as exc:
        return LogEvidence(
            log_group=log_group,
            entries=[],
            note=f"could not query {log_group!r}: {exc.response['Error']['Code']}: {exc.response['Error']['Message']}",
        )
    except (TimeoutError, RuntimeError) as exc:
        return LogEvidence(log_group=log_group, entries=[], note=str(exc))


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def build_cloudwatch_context(
    alarm_message,
    region: Optional[str] = None,
    lookback_minutes: Optional[int] = None,
    lookahead_minutes: Optional[int] = None,
) -> CloudWatchContext:
    """Single entrypoint: alarm name / SNS message -> CloudWatchContext.

    `alarm_message` may be an alarm name string, a parsed alarm-message dict,
    a JSON string of one, or a full SNS event envelope.
    """
    message = parse_alarm_message(alarm_message)
    alarm_name = message["AlarmName"]

    region = region or os.environ.get("AWS_REGION", "us-east-1")
    lookback_minutes = lookback_minutes or int(os.environ.get("LOOKBACK_MINUTES", DEFAULT_LOOKBACK_MINUTES))
    lookahead_minutes = lookahead_minutes or int(os.environ.get("LOOKAHEAD_MINUTES", DEFAULT_LOOKAHEAD_MINUTES))

    alarm = get_alarm_definition(alarm_name, region=region, message=message)
    metric = get_metric_window(alarm, region=region, lookback_minutes=lookback_minutes, lookahead_minutes=lookahead_minutes)

    instance_id = resolve_ec2_instance_id(alarm)
    instance = get_ec2_instance_info(instance_id, region=region) if instance_id else None

    log_group = os.environ.get("LOG_GROUP")
    logs = get_log_evidence(log_group, metric.window_start, metric.window_end, region=region)

    return CloudWatchContext(alarm=alarm, metric=metric, instance=instance, logs=logs)


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Fetch real CloudWatch context for a fired alarm")
    parser.add_argument("alarm_name")
    parser.add_argument("--region", default=None)
    args = parser.parse_args()

    ctx = build_cloudwatch_context(args.alarm_name, region=args.region)
    json.dump(
        ctx,
        sys.stdout,
        default=lambda o: o.isoformat() if isinstance(o, datetime) else o.__dict__,
        indent=2,
    )
