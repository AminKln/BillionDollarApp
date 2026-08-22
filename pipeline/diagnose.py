"""
diagnose.py

Thin CLI wrapper: builds the diagnosis prompt (build_prompt.py) and sends it
to Claude, printing the real root-cause diagnosis. build_prompt.py itself
never calls an LLM -- this is the one script in this directory that does.
"""

import argparse
import json
import os

import anthropic

from build_prompt import build_diagnosis_prompt

MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000  # Sonnet 5 has adaptive thinking on by default -- a low cap
                     # can be spent entirely on thinking, leaving no text output.


def diagnose(alarm_message, git_repo_path=None, region=None) -> str:
    prompt = build_diagnosis_prompt(alarm_message, git_repo_path=git_repo_path, region=region)
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
    parser.add_argument("--region", default=None)
    args = parser.parse_args()

    arg = args.alarm_name_or_sns_file
    if os.path.isfile(arg):
        with open(arg) as f:
            alarm_message = json.load(f)
    else:
        alarm_message = arg

    print(diagnose(alarm_message, git_repo_path=args.git_repo, region=args.region))
