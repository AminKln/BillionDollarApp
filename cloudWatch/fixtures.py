"""
fixtures.py

Offline test doubles for context_builder. Uses botocore's Stubber so the
real collector functions (get_alarm_details, get_metric_anomaly,
get_log_evidence) run their actual parsing logic — just fed canned,
correctly-shaped API responses instead of live AWS traffic.

No AWS account, credentials, or network access required.
"""

from datetime import datetime, timedelta, timezone

import boto3
from botocore.stub import Stubber


SCENARIOS = {
    "db_timeout": {
        "alarm_name": "high-5xx-rate",
        "namespace": "AWS/ApplicationELB",
        "metric_name": "HTTPCode_Target_5XX_Count",
        "dimensions": {"LoadBalancer": "app/my-lb/abc123"},
        "threshold": 10.0,
        "comparison_operator": "GreaterThanThreshold",
        "period": 60,
        "trigger_time": datetime(2026, 8, 21, 14, 32, tzinfo=timezone.utc),
        "baseline_avg": 1.2,
        "anomaly_value": 87.0,
        "unit": "Count",
        "log_group": "/ecs/order-service",
        "error_message": "Npgsql.NpgsqlException: Timeout during reading attempt at OrderRepository.GetById",
        "error_bins": [
            ("2026-08-21 14:31:00", 3),
            ("2026-08-21 14:32:00", 45),
            ("2026-08-21 14:33:00", 39),
        ],
    },
    "ambiguous_5xx": {
        "alarm_name": "checkout-5xx-spike",
        "namespace": "AWS/ApplicationELB",
        "metric_name": "HTTPCode_Target_5XX_Count",
        "dimensions": {"LoadBalancer": "app/my-lb/abc123"},
        "threshold": 10.0,
        "comparison_operator": "GreaterThanThreshold",
        "period": 60,
        "trigger_time": datetime(2026, 8, 21, 16, 5, tzinfo=timezone.utc),
        "baseline_avg": 1.5,
        "anomaly_value": 62.0,
        "unit": "Count",
        "log_group": "/ecs/order-service",
        "error_message": "502 Bad Gateway: upstream connect error or disconnect/reset before headers",
        "error_bins": [
            ("2026-08-21 16:04:00", 4),
            ("2026-08-21 16:05:00", 58),
            ("2026-08-21 16:06:00", 51),
        ],
    },
}


def _fake_client(service: str):
    # Dummy creds so boto3 never tries to resolve real credentials — the
    # Stubber intercepts before any network call is made anyway.
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id="fake",
        aws_secret_access_key="fake",
    )


def build_stubbed_cloudwatch(scenario: dict):
    """One client queued with describe_alarms + two get_metric_statistics
    calls (baseline window, then incident window) — matches the exact call
    order get_alarm_details() and get_metric_anomaly() make."""
    client = _fake_client("cloudwatch")
    stubber = Stubber(client)

    stubber.add_response(
        "describe_alarms",
        {
            "MetricAlarms": [
                {
                    "AlarmName": scenario["alarm_name"],
                    "AlarmDescription": "Fake alarm for local prototyping",
                    "StateReason": f"Threshold Crossed: {scenario['anomaly_value']} > {scenario['threshold']}",
                    "StateUpdatedTimestamp": scenario["trigger_time"],
                    "Namespace": scenario["namespace"],
                    "MetricName": scenario["metric_name"],
                    "Dimensions": [{"Name": k, "Value": v} for k, v in scenario["dimensions"].items()],
                    "ComparisonOperator": scenario["comparison_operator"],
                    "Threshold": scenario["threshold"],
                    "Period": scenario["period"],
                }
            ]
        },
        {"AlarmNames": [scenario["alarm_name"]]},
    )

    incident_time = scenario["trigger_time"]
    baseline_datapoints = [
        {
            "Timestamp": incident_time - timedelta(minutes=m),
            "Average": scenario["baseline_avg"],
            "Maximum": scenario["baseline_avg"] * 1.1,
            "Unit": scenario["unit"],
        }
        for m in range(60, 10, -10)
    ]
    incident_datapoints = [
        {
            "Timestamp": incident_time,
            "Average": scenario["anomaly_value"] * 0.9,
            "Maximum": scenario["anomaly_value"],
            "Unit": scenario["unit"],
        }
    ]
    stubber.add_response("get_metric_statistics", {"Datapoints": baseline_datapoints, "Label": scenario["metric_name"]})
    stubber.add_response("get_metric_statistics", {"Datapoints": incident_datapoints, "Label": scenario["metric_name"]})

    stubber.activate()
    return client, stubber


def build_stubbed_logs(scenario: dict):
    """One client queued for the two Logs Insights queries get_log_evidence()
    issues: the error-sample query, then the error-rate-by-bin query."""
    client = _fake_client("logs")
    stubber = Stubber(client)

    stubber.add_response("start_query", {"queryId": "fake-query-1"})
    stubber.add_response(
        "get_query_results",
        {
            "status": "Complete",
            "results": [
                [
                    {"field": "@timestamp", "value": "2026-08-21 14:32:04.881"},
                    {"field": "@message", "value": scenario["error_message"]},
                ]
            ],
        },
    )

    stubber.add_response("start_query", {"queryId": "fake-query-2"})
    stubber.add_response(
        "get_query_results",
        {
            "status": "Complete",
            "results": [
                [{"field": "bin", "value": ts}, {"field": "errorCount", "value": str(count)}]
                for ts, count in scenario["error_bins"]
            ],
        },
    )

    stubber.activate()
    return client, stubber
