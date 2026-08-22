"""
demo_local.py

Runs the real context-building pipeline against fake-but-correctly-shaped
AWS data. Zero AWS account, credentials, or network needed — good for
iterating on the LLM reasoning step before there's a real app to point at.
"""

from context_builder import IncidentContext, get_alarm_details, get_log_evidence, get_metric_anomaly
from fixtures import SCENARIOS, build_stubbed_cloudwatch, build_stubbed_logs


def run_scenario(name: str) -> IncidentContext:
    scenario = SCENARIOS[name]

    cw_client, _ = build_stubbed_cloudwatch(scenario)
    alarm = get_alarm_details(scenario["alarm_name"], client=cw_client)
    metric = get_metric_anomaly(alarm, client=cw_client)

    logs_client, _ = build_stubbed_logs(scenario)
    logs = get_log_evidence(scenario["log_group"], metric.window_start, metric.window_end, client=logs_client)

    return IncidentContext(alarm=alarm, metric=metric, logs=logs)


if __name__ == "__main__":
    ctx = run_scenario("db_timeout")
    print(ctx.to_llm_context())
