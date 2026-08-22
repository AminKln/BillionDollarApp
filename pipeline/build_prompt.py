"""
build_prompt.py

Combines real CloudWatch evidence (cloudwatch_context.py) and real codebase
evidence (code_context.py) into a single natural-language prompt string,
ready to hand to Claude as the user turn in a messages.create() call. This
module does not call the LLM itself -- it only builds the string.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from cloudwatch_context import CloudWatchContext, build_cloudwatch_context
from code_context import CodeContext, build_code_context

DEFAULT_LOG_LINES_IN_PROMPT = 15


def _format_alarm_section(cw: CloudWatchContext) -> str:
    alarm = cw.alarm
    lines = [
        "## What fired",
        f"Alarm: {alarm.name}" + (f" -- {alarm.description}" if alarm.description else ""),
        f"Metric: {alarm.namespace}/{alarm.metric_name}"
        + (f" [{', '.join(f'{k}={v}' for k, v in alarm.dimensions.items())}]" if alarm.dimensions else ""),
        f"Trigger time: {alarm.trigger_time.isoformat()}",
        f"Detection: {'CloudWatch anomaly-detection band (dynamic threshold)' if alarm.is_anomaly_detection else f'static threshold {alarm.comparison_operator} {alarm.threshold}'}",
    ]
    if alarm.threshold is not None:
        lines.append(f"Threshold at trigger: {alarm.threshold}")
    lines.append(f"State reason (from CloudWatch): {alarm.state_reason}")

    lines.append("")
    lines.append(f"Metric datapoints, {cw.metric.window_start.isoformat()} to {cw.metric.window_end.isoformat()} ({cw.metric.statistic}/Maximum per {alarm.period_seconds}s):")
    if cw.metric.note:
        lines.append(f"  NOTE: {cw.metric.note}")
    for p in cw.metric.datapoints:
        lines.append(f"  {p['timestamp']}: {cw.metric.statistic}={p['value']}, max={p['max']} {p['unit'] or ''}")

    return "\n".join(lines)


def _format_instance_section(cw: CloudWatchContext) -> str:
    if cw.instance is None:
        return ""
    inst = cw.instance
    lines = ["## Host (EC2 instance)"]
    if inst.note and inst.instance_type is None:
        lines.append(f"Instance {inst.instance_id}: {inst.note}")
        return "\n".join(lines)
    lines.append(f"Instance: {inst.instance_id}" + (f" ({inst.name_tag})" if inst.name_tag else ""))
    lines.append(f"Type: {inst.instance_type}, AMI: {inst.ami_id}, AZ: {inst.availability_zone}")
    lines.append(f"State: {inst.state}, launched: {inst.launch_time.isoformat() if inst.launch_time else '?'}, uptime: {inst.uptime}")
    return "\n".join(lines)


def _format_logs_section(cw: CloudWatchContext, max_lines: int) -> str:
    lines = ["## Relevant logs"]
    if cw.logs.log_group is None:
        lines.append(cw.logs.note or "no log group configured")
        return "\n".join(lines)
    lines.append(f"Log group: {cw.logs.log_group}")
    if cw.logs.note:
        lines.append(f"NOTE: {cw.logs.note}")
    for entry in cw.logs.entries[:max_lines]:
        msg = (entry.get("message") or "").strip().replace("\n", " ")
        lines.append(f"  [{entry.get('timestamp', '?')}] {msg}")
    return "\n".join(lines)


def _format_app_section(code: CodeContext) -> str:
    lines = ["## What's running"]
    lines.append(f"Repo: {code.remote_url or code.repo_path}")
    if code.readme:
        lines.append("")
        lines.append(code.readme.strip())
    else:
        lines.append(code.note or "no README/architecture doc found")
    return "\n".join(lines)


LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".jsx": "jsx", ".tsx": "tsx",
    ".go": "go", ".rb": "ruby", ".java": "java", ".rs": "rust", ".sh": "bash",
    ".yml": "yaml", ".yaml": "yaml", ".json": "json", ".md": "markdown",
}


def _format_codebase_section(code: CodeContext) -> str:
    lines = ["## Codebase (current state)"]
    if code.latest_commit:
        c = code.latest_commit
        lines.append(f"As of latest commit {c.sha[:10]} ({c.date}) by {c.author}: {c.message}")
    if code.note:
        lines.append(f"NOTE: {code.note}")
    lines.append("")

    for f in code.files:
        if f.content is None:
            lines.append(f"### {f.path}  [omitted -- exceeded prompt budget, relevance={f.relevance_score}]")
            lines.append("")
            continue
        lang = LANG_BY_EXT.get(Path(f.path).suffix.lower(), "")
        trunc = "  [truncated]" if f.truncated else ""
        lines.append(f"### {f.path}{trunc}")
        lines.append(f"```{lang}")
        lines.append(f.content.rstrip("\n"))
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip()


ASK = """## Your task
Using only the evidence above:
1. Hypothesize the most likely root cause of this anomaly.
2. Point to the specific suspect file/line in the codebase above.
3. Suggest a concrete fix (or, if the evidence doesn't support one confidently, the smallest safe mitigation and why).
Be explicit about which pieces of evidence support your hypothesis, and say plainly if the evidence is too thin to be confident."""


def _assemble(cw: CloudWatchContext, code: CodeContext, max_log_lines: int) -> str:
    sections = [
        _format_alarm_section(cw),
        _format_instance_section(cw),
        _format_logs_section(cw, max_log_lines),
        _format_app_section(code),
        _format_codebase_section(code),
        ASK,
    ]
    return "\n\n".join(s for s in sections if s)


def build_diagnosis_prompt(
    alarm_message,
    git_repo_path: Optional[str] = None,
    region: Optional[str] = None,
    lookback_minutes: Optional[int] = None,
    lookahead_minutes: Optional[int] = None,
    max_log_lines: int = DEFAULT_LOG_LINES_IN_PROMPT,
) -> str:
    """Single entrypoint: SNS alarm message -> ready-to-send prompt string."""
    cw_ctx = build_cloudwatch_context(
        alarm_message, region=region, lookback_minutes=lookback_minutes, lookahead_minutes=lookahead_minutes,
    )

    repo = git_repo_path or os.environ.get("GIT_REPO_PATH")
    if not repo:
        raise ValueError("no git repo given -- pass git_repo_path or set GIT_REPO_PATH")
    code_ctx = build_code_context(repo, metric_name=cw_ctx.alarm.metric_name)

    return _assemble(cw_ctx, code_ctx, max_log_lines)


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Build the full diagnosis prompt for a fired alarm")
    parser.add_argument("alarm_name_or_sns_file", help="alarm name, or a path to a JSON file with an SNS/alarm message")
    parser.add_argument("--git-repo", default=None, help="local checkout path or clone URL (or set GIT_REPO_PATH)")
    parser.add_argument("--region", default=None)
    args = parser.parse_args()

    arg = args.alarm_name_or_sns_file
    if os.path.isfile(arg):
        with open(arg) as f:
            alarm_message = json.load(f)
    else:
        alarm_message = arg

    prompt = build_diagnosis_prompt(alarm_message, git_repo_path=args.git_repo, region=args.region)
    print(prompt)
