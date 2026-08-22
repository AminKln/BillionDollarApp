"""
github_issue.py

Surfaces the LLM's verdict somewhere a human will actually see it: opens a
GitHub issue on the target app's repo with the diagnosis, instead of the
verdict only ever reaching logger.info() (see docs/current-pipeline.md --
that was the only sink before this module existed).

Guards against issue spam on a flapping alarm: culprit-App-Anomaly has been
observed flipping ALARM<->OK repeatedly within minutes. open_or_update_issue()
looks for an existing OPEN issue whose title already names this alarm (a
fixed prefix, not a label -- see the note on _find_open_issue()) and adds a
comment to it instead of opening a new issue every time the alarm re-fires.

Requires GITHUB_TOKEN with write access to owner/repo -- unlike
github_context.py's read-only codebase fetch, issue creation is never
possible unauthenticated. Deliberately doesn't apply a label to the issue:
a token with only issue-write access (no broader repo/triage permission)
403s on label-create for a label that doesn't already exist in the repo --
confirmed against the real target repo.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

GITHUB_API = "https://api.github.com"


def open_or_update_issue(owner: str, repo: str, alarm_name: str, verdict: dict) -> dict:
    """Opens a new issue for this alarm, or -- if one is already open --
    adds a comment to it instead of creating a duplicate. Returns
    {number, html_url, action: "created"|"commented"}. Raises RuntimeError
    on any GitHub API failure (most commonly a missing/insufficient
    GITHUB_TOKEN); callers should treat that as non-fatal to the diagnosis
    itself, same as every other "evidence not available" style failure in
    this pipeline -- log it, don't crash the handler."""
    if not owner or not repo:
        raise ValueError("owner/repo required to open a GitHub issue")

    title, body = _format_verdict(alarm_name, verdict)

    existing = _find_open_issue(owner, repo, alarm_name)
    if existing:
        comment = _add_comment(owner, repo, existing["number"], body)
        return {"number": existing["number"], "html_url": existing["html_url"], "action": "commented"}

    issue = _create_issue(owner, repo, title, body)
    return {"number": issue["number"], "html_url": issue["html_url"], "action": "created"}


def _format_verdict(alarm_name: str, verdict: dict) -> tuple[str, str]:
    title = f"Anomaly diagnosis: {alarm_name} ({verdict.get('confidence', '?')} confidence)"

    lines = [
        f"**Hypothesis:** {verdict.get('hypothesis', '?')}",
        "",
        f"**Confidence:** {verdict.get('confidence', '?')}",
        "",
        "**Supporting evidence:**",
    ]
    for ev in verdict.get("supporting_evidence", []):
        lines.append(f"- `{ev.get('source', '?')}`: {ev.get('excerpt', '?')}")
    lines.append("")
    lines.append(f"**Suggested action:** {verdict.get('suggested_action', '?')}")
    lines.append("")
    lines.append("_Opened automatically by the `anomaly-review-agent` Lambda on a real CloudWatch alarm -- not a human-filed issue._")
    return title, "\n".join(lines)


def _find_open_issue(owner: str, repo: str, alarm_name: str) -> dict | None:
    """Searches open issues for one this module already opened for this
    alarm (matched by the fixed title prefix set in _format_verdict() --
    not a label: labeling requires repo permissions beyond issue-write,
    e.g. a fine-grained PAT scoped to just Issues, which 403s on
    label-create -- see the module docstring). GitHub's Issues list
    endpoint returns pull requests too -- filtered out below since a PR
    isn't a valid target to comment a diagnosis onto."""
    prefix = f"Anomaly diagnosis: {alarm_name} ("
    data = json.loads(_github_request(f"/repos/{owner}/{repo}/issues?state=open&per_page=100"))
    for issue in data:
        if "pull_request" in issue:
            continue
        if issue.get("title", "").startswith(prefix):
            return {"number": issue["number"], "html_url": issue["html_url"]}
    return None


def _create_issue(owner: str, repo: str, title: str, body: str) -> dict:
    data = _github_request(
        f"/repos/{owner}/{repo}/issues",
        method="POST",
        payload={"title": title, "body": body},
    )
    return json.loads(data)


def _add_comment(owner: str, repo: str, issue_number: int, body: str) -> dict:
    data = _github_request(
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        method="POST",
        payload={"body": body},
    )
    return json.loads(data)


def _github_request(path: str, method: str = "GET", payload: dict | None = None) -> bytes:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN is required to open/comment on a GitHub issue -- "
            "unlike the read-only codebase fetch, this is never possible "
            "unauthenticated. Set it to a token with write access to the "
            "target repo."
        )

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{GITHUB_API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {exc.code} for {method} {path}: {body}") from exc
