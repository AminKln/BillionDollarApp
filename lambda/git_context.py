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
MAX_PR_BODY_CHARS = 2_000  # cap per PR description
MAX_PR_PATCH_CHARS = 1_500  # cap per file patch in PR context
MAX_PR_FILES = 10           # max files to include per PR


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


def get_recent_prs(owner, repo, until_timestamp, count=3):
    """Returns the last `count` PRs merged before `until_timestamp` with
    file-level patches, so the LLM has causal context on recent changes."""
    pulls = json.loads(
        _github_request(
            f"/repos/{owner}/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=20"
        )
    )

    qualifying = [
        p for p in pulls
        if p.get("merged_at") and p["merged_at"] < until_timestamp
    ][:count]

    result = []
    for pr in qualifying:
        number = pr["number"]
        files_raw = json.loads(
            _github_request(f"/repos/{owner}/{repo}/pulls/{number}/files?per_page={MAX_PR_FILES}")
        )
        files = []
        for f in files_raw[:MAX_PR_FILES]:
            patch = f.get("patch", "")
            if len(patch) > MAX_PR_PATCH_CHARS:
                patch = patch[:MAX_PR_PATCH_CHARS] + "\n... (patch truncated)"
            files.append({
                "filename": f["filename"],
                "status": f["status"],
                "additions": f["additions"],
                "deletions": f["deletions"],
                "patch": patch,
            })

        body = pr.get("body") or ""
        result.append({
            "number": number,
            "title": pr["title"],
            "body": body[:MAX_PR_BODY_CHARS],
            "author": pr["user"]["login"],
            "merged_at": pr["merged_at"],
            "files": files,
        })

    return result


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
