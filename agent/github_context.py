"""
github_context.py

Codebase context for the prompt. Stubbed for now (get_codebase_context_from_github
returns a fixed placeholder, no network call) so getting real alerts flowing
doesn't depend on this session's restructure being pushed to GitHub first.

_fetch_from_github() below is the real, already-tested implementation (live
fetch via the GitHub Contents API) -- to re-enable it, swap the body of
get_codebase_context_from_github() to call it instead of returning the
placeholder. GITHUB_TOKEN is optional for public repos (unauthenticated
reads work, 60/hr limit vs 5000/hr with a token) but required for private
ones.
"""

import base64
import json
import os
import urllib.error
import urllib.request

GITHUB_API = "https://api.github.com"
DEFAULT_PATH = "agent/codebase_context.md"


def get_codebase_context_from_github(owner: str, repo: str, path: str = DEFAULT_PATH, ref: str | None = None) -> str:
    return (
        f"[PLACEHOLDER] Codebase context not fetched from GitHub right now "
        f"(stubbed) for {owner or '?'}/{repo or '?'}. See "
        f"_fetch_from_github() in this file for the real implementation."
    )


def _fetch_from_github(owner: str, repo: str, path: str = DEFAULT_PATH, ref: str | None = None) -> str:
    """Fetches `path` from `owner/repo` via the GitHub Contents API and
    returns its decoded text. `ref` pins a branch/commit/tag; omit for the
    repo's default branch."""
    query = f"?ref={ref}" if ref else ""
    data = json.loads(_github_request(f"/repos/{owner}/{repo}/contents/{path}{query}"))
    return base64.b64decode(data["content"]).decode("utf-8")


def _github_request(path):
    req = urllib.request.Request(f"{GITHUB_API}{path}")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {exc.code} for {path}: {body}") from exc
