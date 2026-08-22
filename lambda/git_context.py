"""
Finds the last commit before an alarm's timestamp and fetches the diff since
then via the GitHub REST API. Deliberately dependency-free (plain
urllib.request, no `requests`) so the Lambda package needs no build step.
"""

import json
import os
import urllib.error
import urllib.request

GITHUB_API = "https://api.github.com"
MAX_DIFF_CHARS = 24_000  # rough cap so the diff stays inside the LLM's context budget


def _github_request(path, accept="application/vnd.github+json"):
    req = urllib.request.Request(f"{GITHUB_API}{path}")
    req.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {exc.code} for {path}: {body}") from exc


def get_commit_diff(owner, repo, until_timestamp):
    """Returns the diff + commit list between the last commit before
    `until_timestamp` and HEAD, matching the shape llm_review.py expects."""
    commits = json.loads(
        _github_request(f"/repos/{owner}/{repo}/commits?until={until_timestamp}&per_page=1")
    )
    if not commits:
        raise RuntimeError(f"No commits found in {owner}/{repo} before {until_timestamp}")
    good_sha = commits[0]["sha"]

    compare = json.loads(_github_request(f"/repos/{owner}/{repo}/compare/{good_sha}...HEAD"))

    diff_text = _github_request(
        f"/repos/{owner}/{repo}/compare/{good_sha}...HEAD",
        accept="application/vnd.github.v3.diff",
    ).decode("utf-8", errors="replace")

    truncated = len(diff_text) > MAX_DIFF_CHARS
    if truncated:
        diff_text = diff_text[:MAX_DIFF_CHARS]

    return {
        "good_commit_sha": good_sha,
        "commits": [c["sha"] for c in compare.get("commits", [])],
        "diff": diff_text,
        "diff_truncated": truncated,
    }
