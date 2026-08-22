"""
fake_alert.py

Builds a fake-but-correctly-shaped IncidentContext for the manual run path
(run_manual.py) -- runs the real context_builder collector functions against
canned data from fixtures.py via botocore's Stubber, so the parsing logic is
real even though the underlying AWS data is not. Zero AWS account,
credentials, or network needed.
"""

from context_builder import IncidentContext, get_alarm_details, get_log_evidence, get_metric_anomaly
from fixtures import SCENARIOS, build_stubbed_cloudwatch, build_stubbed_logs


def build_fake_incident(scenario_name: str) -> IncidentContext:
    scenario = SCENARIOS[scenario_name]

    cw_client, _ = build_stubbed_cloudwatch(scenario)
    alarm = get_alarm_details(scenario["alarm_name"], client=cw_client)
    metric = get_metric_anomaly(alarm, client=cw_client)

    logs_client, _ = build_stubbed_logs(scenario)
    logs = get_log_evidence(scenario["log_group"], metric.window_start, metric.window_end, client=logs_client)

    return IncidentContext(alarm=alarm, metric=metric, logs=logs)
