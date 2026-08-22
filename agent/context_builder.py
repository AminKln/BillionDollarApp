"""
context_builder.py

Builds a structured incident context from CloudWatch (alarm + metric + logs)
for the root-cause agent's LLM step.

PR/diff correlation is intentionally left out for now — see the docstring on
build_incident_context() for where it slots back in later without changing
this module's entry point.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3


# ---------- data model ----------

@dataclass
class AlarmDetails:
    name: str
    description: Optional[str]
    state_reason: str
    state_updated: datetime
    namespace: str
    metric_name: str
    dimensions: dict[str, str]
    comparison_operator: str
    threshold: Optional[float]  # None for metric-math/anomaly-band alarms -- no fixed threshold, the band is dynamic
    period_seconds: int


@dataclass
class MetricAnomaly:
    metric_name: str
    namespace: str
    baseline_avg: Optional[float]
    anomaly_value: Optional[float]
    unit: Optional[str]
    window_start: datetime
    window_end: datetime
    datapoints: list[dict]  # raw [{timestamp, value}] for the incident window


@dataclass
class LogEvidence:
    log_group: str
    query: str
    window_start: datetime
    window_end: datetime
    error_events: list[dict]        # top N raw error/exception lines
    error_rate_by_bin: list[dict]   # count(*) by bin(1m) — shows the spike shape


@dataclass
class IncidentContext:
    alarm: AlarmDetails
    metric: MetricAnomaly
    logs: Optional[LogEvidence]  # None when no log group is configured (app doesn't ship logs yet)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_llm_context(self) -> str:
        """Render a compact, LLM-ready digest — facts only, no raw dumps."""
        lines = []
        lines.append(f"ALARM: {self.alarm.name}")
        lines.append(f"  Reason: {self.alarm.state_reason}")
        lines.append(f"  Triggered: {self.alarm.state_updated.isoformat()}")
        threshold_desc = self.alarm.threshold if self.alarm.threshold is not None else "dynamic (anomaly-detection band, no fixed value)"
        lines.append(
            f"  Metric: {self.alarm.namespace}/{self.alarm.metric_name} "
            f"{self.alarm.comparison_operator} {threshold_desc}"
        )
        if self.alarm.dimensions:
            dims = ", ".join(f"{k}={v}" for k, v in self.alarm.dimensions.items())
            lines.append(f"  Dimensions: {dims}")

        lines.append("")
        lines.append("METRIC ANOMALY:")
        lines.append(f"  Baseline avg (prior hour): {self.metric.baseline_avg}")
        lines.append(f"  Value at incident: {self.metric.anomaly_value} {self.metric.unit or ''}")
        lines.append(
            f"  Window: {self.metric.window_start.isoformat()} -> {self.metric.window_end.isoformat()}"
        )

        lines.append("")
        if self.logs is None:
            lines.append("LOG EVIDENCE: not available (no log group configured yet — base the hypothesis on the alarm and metric evidence only).")
        else:
            lines.append(f"LOG EVIDENCE ({self.logs.log_group}):")
            lines.append("  Error rate by minute:")
            for row in self.logs.error_rate_by_bin:
                lines.append(f"    {row['bin']}: {row['count']}")
            lines.append(f"  Top {len(self.logs.error_events)} error samples:")
            for ev in self.logs.error_events:
                ts = ev.get("@timestamp", "?")
                msg = ev.get("@message", "").strip().replace("\n", " ")[:300]
                lines.append(f"    [{ts}] {msg}")

        return "\n".join(lines)


# ---------- collectors ----------

def get_alarm_details(alarm_name: str, region: str = "us-east-1", client=None) -> AlarmDetails:
    cw = client or boto3.client("cloudwatch", region_name=region)
    resp = cw.describe_alarms(AlarmNames=[alarm_name])
    alarms = resp.get("MetricAlarms", [])
    if not alarms:
        raise ValueError(f"No alarm found named {alarm_name!r}")
    a = alarms[0]

    if "Namespace" in a:
        # Simple, single-metric alarm -- Namespace/MetricName/Dimensions/
        # Period/Threshold all live at the top level.
        namespace = a["Namespace"]
        metric_name = a["MetricName"]
        dimensions = {d["Name"]: d["Value"] for d in a.get("Dimensions", [])}
        period_seconds = a["Period"]
        threshold = a.get("Threshold")
    else:
        # Metric-math alarm (e.g. ANOMALY_DETECTION_BAND) -- there's no
        # single top-level metric; it's one of several entries in Metrics[].
        # Find the one with a MetricStat (the underlying metric query, not
        # the anomaly-band expression) and pull namespace/metric/dims/period
        # from there. No fixed Threshold exists -- the band is dynamic.
        metric_query = next((m for m in a.get("Metrics", []) if "MetricStat" in m), None)
        if metric_query is None:
            raise ValueError(f"Alarm {alarm_name!r} has no MetricStat in its Metrics -- can't determine its metric")
        metric = metric_query["MetricStat"]["Metric"]
        namespace = metric["Namespace"]
        metric_name = metric["MetricName"]
        dimensions = {d["Name"]: d["Value"] for d in metric.get("Dimensions", [])}
        period_seconds = metric_query["MetricStat"]["Period"]
        threshold = None

    return AlarmDetails(
        name=a["AlarmName"],
        description=a.get("AlarmDescription"),
        state_reason=a.get("StateReason", ""),
        state_updated=a["StateUpdatedTimestamp"],
        namespace=namespace,
        metric_name=metric_name,
        dimensions=dimensions,
        comparison_operator=a["ComparisonOperator"],
        threshold=threshold,
        period_seconds=period_seconds,
    )


def get_metric_anomaly(
    alarm: AlarmDetails,
    region: str = "us-east-1",
    baseline_lookback: timedelta = timedelta(hours=1),
    window_padding: timedelta = timedelta(minutes=10),
    client=None,
) -> MetricAnomaly:
    cw = client or boto3.client("cloudwatch", region_name=region)

    incident_time = alarm.state_updated
    window_start = incident_time - window_padding
    window_end = incident_time + window_padding
    baseline_start = incident_time - baseline_lookback
    baseline_end = incident_time - window_padding

    dims = [{"Name": k, "Value": v} for k, v in alarm.dimensions.items()]

    def _get_stats(start: datetime, end: datetime) -> list[dict]:
        resp = cw.get_metric_statistics(
            Namespace=alarm.namespace,
            MetricName=alarm.metric_name,
            Dimensions=dims,
            StartTime=start,
            EndTime=end,
            Period=max(alarm.period_seconds, 60),
            Statistics=["Average", "Maximum"],
        )
        return sorted(resp["Datapoints"], key=lambda d: d["Timestamp"])

    baseline_points = _get_stats(baseline_start, baseline_end)
    incident_points = _get_stats(window_start, window_end)

    baseline_avg = (
        sum(p["Average"] for p in baseline_points) / len(baseline_points)
        if baseline_points
        else None
    )
    anomaly_value = incident_points[-1]["Maximum"] if incident_points else None
    unit = incident_points[-1].get("Unit") if incident_points else None

    return MetricAnomaly(
        metric_name=alarm.metric_name,
        namespace=alarm.namespace,
        baseline_avg=baseline_avg,
        anomaly_value=anomaly_value,
        unit=unit,
        window_start=window_start,
        window_end=window_end,
        datapoints=[
            {"timestamp": p["Timestamp"].isoformat(), "value": p["Maximum"]}
            for p in incident_points
        ],
    )


def _run_logs_insights_query(
    logs_client,
    log_group: str,
    query: str,
    start: datetime,
    end: datetime,
    timeout_s: int = 30,
) -> list[dict]:
    start_resp = logs_client.start_query(
        logGroupName=log_group,
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()),
        queryString=query,
    )
    query_id = start_resp["queryId"]

    elapsed = 0
    poll_interval = 1
    result = None
    while elapsed < timeout_s:
        result = logs_client.get_query_results(queryId=query_id)
        if result["status"] in ("Complete", "Failed", "Cancelled"):
            break
        time.sleep(poll_interval)
        elapsed += poll_interval
    else:
        logs_client.stop_query(queryId=query_id)
        raise TimeoutError(f"Logs Insights query timed out after {timeout_s}s")

    if result is None or result["status"] != "Complete":
        status = result["status"] if result else "Unknown"
        raise RuntimeError(f"Logs Insights query ended with status {status}")

    rows = []
    for row in result["results"]:
        rows.append({col["field"]: col["value"] for col in row})
    return rows


def get_log_evidence(
    log_group: str,
    window_start: datetime,
    window_end: datetime,
    region: str = "us-east-1",
    error_sample_limit: int = 25,
    client=None,
) -> LogEvidence:
    logs_client = client or boto3.client("logs", region_name=region)

    error_query = (
        "fields @timestamp, @message "
        "| filter @message like /(?i)(error|exception|traceback|fail)/ "
        "| sort @timestamp desc "
        f"| limit {error_sample_limit}"
    )
    error_events = _run_logs_insights_query(logs_client, log_group, error_query, window_start, window_end)

    rate_query = (
        "filter @message like /(?i)(error|exception|traceback|fail)/ "
        "| stats count(*) as errorCount by bin(1m) as bin"
    )
    rate_rows = _run_logs_insights_query(logs_client, log_group, rate_query, window_start, window_end)
    error_rate_by_bin = [{"bin": r.get("bin"), "count": r.get("errorCount")} for r in rate_rows]

    return LogEvidence(
        log_group=log_group,
        query=error_query,
        window_start=window_start,
        window_end=window_end,
        error_events=error_events,
        error_rate_by_bin=error_rate_by_bin,
    )


# ---------- orchestration ----------

def build_incident_context(alarm_name: str, log_group: Optional[str], region: str = "us-east-1") -> IncidentContext:
    """
    Assembles CloudWatch alarm + metric + (optionally) log evidence into one
    IncidentContext. Pass log_group=None (or "") to skip the log evidence
    step entirely -- for when the app doesn't ship logs yet. Re-enables
    itself automatically the moment a real log group is configured.

    PR/diff correlation is deliberately not wired in here yet. When it comes
    back, it should query the same window (metric.window_start / window_end)
    for candidate deploys and attach as a new field on IncidentContext (e.g.
    `candidate_prs: list[PullRequestDiff]`) — this function stays the single
    entry point the LLM agent calls.
    """
    alarm = get_alarm_details(alarm_name, region=region)
    metric = get_metric_anomaly(alarm, region=region)
    logs = (
        get_log_evidence(log_group, metric.window_start, metric.window_end, region=region)
        if log_group
        else None
    )
    return IncidentContext(alarm=alarm, metric=metric, logs=logs)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build incident context from a CloudWatch alarm")
    parser.add_argument("alarm_name")
    parser.add_argument("log_group")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    ctx = build_incident_context(args.alarm_name, args.log_group, region=args.region)
    print(ctx.to_llm_context())
