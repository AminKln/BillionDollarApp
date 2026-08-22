"""
diagnose.py

The one function the anomaly-detection side of this project needs:

    from diagnose import diagnose
    result = diagnose(event)   # -> Claude's full root-cause diagnosis, as text

Call it with whatever the detector has on hand when an anomaly fires -- a
bare CloudWatch alarm name, a parsed alarm-state-change dict, a raw SNS
event envelope, or a JSON string of any of those. Everything else (pulling
real CloudWatch evidence, cloning/reading the target repo, assembling the
prompt, calling Claude) happens inside this one call.

Requires ANTHROPIC_API_KEY in the environment. AWS credentials come from
the environment / instance role, same as any boto3 call -- nothing is
hardcoded except this project's fixed demo target (the app repo + EC2
instance), which can still be overridden via env vars or function args if
that ever changes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Makes the cross-module imports below (build_prompt -> cloudwatch_context /
# code_context) resolve correctly no matter what directory this is imported
# from -- the caller just needs this file's path, not a matching cwd/PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import anthropic

from build_prompt import build_diagnosis_prompt

MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000  # Sonnet 5 has adaptive thinking on by default -- a low cap
                     # can be spent entirely on thinking, leaving no text output.

# This project's one demo target -- override via GIT_REPO_PATH/INSTANCE_ID/
# AWS_REGION env vars or the matching function args if it ever changes. The
# point of having defaults at all is that the caller shouldn't need to know
# these just to get a diagnosis.
DEFAULT_GIT_REPO = "https://github.com/Tehreem404/bad_app_demo.git"
DEFAULT_INSTANCE_ID = "i-091814f7a41456cb0"
DEFAULT_REGION = "us-east-1"


def diagnose(event, git_repo_path: str | None = None, instance_id: str | None = None, region: str | None = None) -> str:
    """event: whatever the anomaly detector produces -- alarm name, parsed
    alarm-state-change dict, raw SNS envelope, or a JSON string of one.
    Returns Claude's full diagnosis (hypothesis, suspect code, suggested
    fix) as plain text. Raises if the alarm can't be found, the repo can't
    be read, or the Anthropic call fails -- this is intentionally not
    swallowed, so a broken run surfaces instead of silently returning
    nothing."""
    os.environ["INSTANCE_ID"] = instance_id or os.environ.get("INSTANCE_ID") or DEFAULT_INSTANCE_ID

    prompt = build_diagnosis_prompt(
        event,
        git_repo_path=git_repo_path or os.environ.get("GIT_REPO_PATH") or DEFAULT_GIT_REPO,
        region=region or os.environ.get("AWS_REGION") or DEFAULT_REGION,
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a diagnosis prompt and send it to Claude")
    parser.add_argument("alarm_name_or_sns_file", help="alarm name, or a path to a JSON file with an SNS/alarm message")
    parser.add_argument("--git-repo", default=None, help="local checkout path or clone URL (or set GIT_REPO_PATH)")
    parser.add_argument("--instance-id", default=None, help="EC2 instance ID for host metadata (or set INSTANCE_ID)")
    parser.add_argument("--region", default=None)
    args = parser.parse_args()

    arg = args.alarm_name_or_sns_file
    if os.path.isfile(arg):
        with open(arg) as f:
            event = json.load(f)
    else:
        event = arg

    print(diagnose(event, git_repo_path=args.git_repo, instance_id=args.instance_id, region=args.region))
