#!/usr/bin/env python3
"""
verify_alarm_shapes.py

Stand-alone proof that the fixed get_alarm_details() logic (see
anomaly-alarm-shape.patch / README.md in this directory) correctly parses
BOTH shapes describe-alarms can return:

  - culprit-App-High     -> plain metric alarm (top-level Namespace/MetricName/...)
  - culprit-App-Anomaly  -> metric-math / ANOMALY_DETECTION_BAND alarm
                             (Namespace lives inside Metrics[].MetricStat.Metric)

It calls the real `aws cloudwatch describe-alarms` for both alarms (no
mocked data), runs the fixed parsing logic against the live response, and
prints what it resolved. No credentials or secrets are read or printed.

Usage:
    python3 verify_alarm_shapes.py [--region us-east-1] alarm_name [alarm_name ...]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Optional


def describe_alarms(alarm_names: list[str], region: str) -> dict:
    """Shell out to the AWS CLI so this script has zero Python deps beyond stdlib."""
    cmd = [
        "aws", "cloudwatch", "describe-alarms",
        "--alarm-names", *alarm_names,
        "--region", region,
        "--output", "json",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def get_alarm_details(a: dict) -> dict:
    """
    The fixed logic from context_builder.py: handles both the plain metric
    alarm shape and the metric-math/anomaly-detection shape.
    """
    if "Namespace" in a:
        # Simple, single-metric alarm -- Namespace/MetricName/Dimensions/
        # Period/Threshold all live at the top level.
        namespace = a["Namespace"]
        metric_name = a["MetricName"]
        dimensions = {d["Name"]: d["Value"] for d in a.get("Dimensions", [])}
        period_seconds = a["Period"]
        threshold: Optional[float] = a.get("Threshold")
    else:
        # Metric-math alarm (e.g. ANOMALY_DETECTION_BAND) -- there's no
        # single top-level metric; it's one of several entries in Metrics[].
        # Find the one with a MetricStat (the underlying metric query, not
        # the anomaly-band expression) and pull namespace/metric/dims/period
        # from there. No fixed Threshold exists -- the band is dynamic.
        metric_query = next((m for m in a.get("Metrics", []) if "MetricStat" in m), None)
        if metric_query is None:
            raise ValueError(f"Alarm {a.get('AlarmName')!r} has no MetricStat in its Metrics -- can't determine its metric")
        metric = metric_query["MetricStat"]["Metric"]
        namespace = metric["Namespace"]
        metric_name = metric["MetricName"]
        dimensions = {d["Name"]: d["Value"] for d in metric.get("Dimensions", [])}
        period_seconds = metric_query["MetricStat"]["Period"]
        threshold = None

    return {
        "name": a["AlarmName"],
        "namespace": namespace,
        "metric_name": metric_name,
        "dimensions": dimensions,
        "comparison_operator": a["ComparisonOperator"],
        "threshold": threshold,
        "threshold_metric_id": a.get("ThresholdMetricId"),
        "period_seconds": period_seconds,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("alarm_names", nargs="*", default=["culprit-App-High", "culprit-App-Anomaly"])
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    resp = describe_alarms(args.alarm_names, args.region)
    alarms_by_name = {a["AlarmName"]: a for a in resp.get("MetricAlarms", [])}

    ok = True
    for name in args.alarm_names:
        a = alarms_by_name.get(name)
        if a is None:
            print(f"FAIL {name}: not found")
            ok = False
            continue
        try:
            details = get_alarm_details(a)
            print(f"OK   {name}")
            print(f"       namespace           = {details['namespace']!r}")
            print(f"       metric_name         = {details['metric_name']!r}")
            print(f"       dimensions          = {details['dimensions']}")
            print(f"       comparison_operator = {details['comparison_operator']!r}")
            print(f"       threshold           = {details['threshold']!r}")
            print(f"       threshold_metric_id = {details['threshold_metric_id']!r}")
            print(f"       period_seconds      = {details['period_seconds']!r}")
        except Exception as e:
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
