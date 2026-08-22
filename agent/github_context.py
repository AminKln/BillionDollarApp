"""
github_context.py

Fetches real codebase context for the prompt: the target repo's entire
current source tree (every non-binary, non-skipped tracked file, in full up
to a char budget), a README if present, and a one-line "as of commit X"
freshness note. All via the GitHub REST API (plain `urllib`, no `requests`
dependency) -- deliberately API-based rather than a local `git clone`,
because the Lambda runtime this deploys into has no `git` binary and no
guarantee of scratch disk to clone into.

Same "dump the whole current codebase, not a diff" design as this repo's
pipeline/code_context.py (built on this project's feature/LLM_diagnosis_context
branch, which used a local `git clone` since it runs outside Lambda) --
reimplemented here against the GitHub API so it actually runs where this
Lambda is deployed. Keep the skip-list/budget/relevance logic in sync
between the two if either changes.

Un-stubs the previous placeholder version of get_codebase_context_from_github()
-- see git history for that stub and _fetch_from_github(), the single-file
Contents-API fetch it was going to grow into before this repo's app moved to
a separate repo the codebase context now has to be fetched from live.

GITHUB_TOKEN is optional for public repos (unauthenticated reads work, 60/hr
limit vs 5000/hr with a token) but required for private ones.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request

GITHUB_API = "https://api.github.com"

MAX_TOTAL_CODE_CHARS = 40_000    # keeps the whole-codebase dump inside a sane prompt budget
MAX_FILE_CHARS = 12_000          # caps any single file so one huge file can't eat the whole budget
README_MAX_CHARS = 3_000

README_NAMES = {"README.md", "README.rst", "README", "readme.md"}
SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".zip", ".tar", ".gz",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mov", ".pyc", ".class", ".jar",
    ".so", ".dll", ".exe", ".lock",
}
SKIP_BASENAMES = {"package-lock.json", "yarn.lock", "poetry.lock", "Pipfile.lock", "Cargo.lock"}

# metric-name keyword -> related terms, used only to prioritize which files
# stay in full when the codebase exceeds MAX_TOTAL_CODE_CHARS -- not to
# filter anything out outright, every file is included unless the budget
# runs out.
METRIC_HINTS = {
    "latency": ["latency", "sleep", "timeout", "slow", "delay", "route", "handler", "endpoint", "request"],
    "request": ["route", "handler", "endpoint", "request", "flask", "view", "controller"],
    "error": ["error", "except", "exception", "fail", "500", "5xx", "traceback"],
    "rate": ["error", "except", "exception", "fail", "500", "5xx"],
    "cpu": ["cpu", "burn", "loop", "hash", "thread", "worker", "process"],
    "utilization": ["cpu", "loop", "worker", "thread"],
    "memory": ["memory", "cache", "buffer", "leak", "alloc"],
    "disk": ["disk", "write", "file", "log", "storage"],
    "systemload": ["load", "thread", "worker", "cpu", "process"],
    "load": ["load", "thread", "worker", "cpu", "process"],
}


def get_codebase_context_from_github(owner: str, repo: str, ref: str | None = None, metric_name: str | None = None) -> str:
    """Returns a text digest of owner/repo's full current source tree, or an
    explanatory string (never fabricated content) if owner/repo aren't
    configured or the fetch fails -- callers should pass that string through
    to the prompt as-is, same as every other "evidence not available" note
    in this pipeline."""
    if not owner or not repo:
        return "[no codebase context -- GITHUB_OWNER/GITHUB_REPO not configured]"

    try:
        commit = _get_latest_commit(owner, repo, ref)
        tree, truncated = _get_full_tree(owner, repo, commit["sha"])
    except RuntimeError as exc:
        return f"[codebase context unavailable -- {exc}]"

    readme_entry = next((e for e in tree if e["type"] == "blob" and e["path"] in README_NAMES), None)
    readme = None
    if readme_entry:
        readme = _fetch_blob_text(owner, repo, readme_entry["sha"])
        if readme is not None:
            readme = readme[:README_MAX_CHARS] + ("\n...[truncated]" if len(readme) > README_MAX_CHARS else "")

    candidates = [e for e in tree if e["type"] == "blob" and not _should_skip(e["path"])]
    keywords = _metric_keywords(metric_name) if metric_name else []

    scored = []
    fetch_failures = []
    for entry in candidates:
        content = _fetch_blob_text(owner, repo, entry["sha"])
        if content is None:
            fetch_failures.append(entry["path"])  # binary or undecodable -- noted, not silently dropped
            continue
        scored.append((entry["path"], content, _score_relevance(entry["path"], content, keywords)))
    scored.sort(key=lambda t: (-t[2], t[0]))

    lines = [f"As of latest commit {commit['sha'][:10]} ({commit['date']}) by {commit['author']}: {commit['message']}"]
    if truncated:
        lines.append("NOTE: GitHub's tree API reported this listing as truncated (repo too large for one response) -- some files may be missing entirely.")
    if fetch_failures:
        lines.append(f"NOTE: skipped {len(fetch_failures)} binary/undecodable file(s): {', '.join(fetch_failures)}")
    lines.append("")

    budget = MAX_TOTAL_CODE_CHARS
    omitted = []
    for path, content, _score in scored:
        if budget <= 0:
            omitted.append(path)
            continue
        capped = content[:MAX_FILE_CHARS]
        take = capped[:budget]
        file_truncated = len(content) > len(take)
        budget -= len(take)
        lines.append(f"### {path}" + ("  [truncated]" if file_truncated else ""))
        lines.append("```")
        lines.append(take.rstrip("\n"))
        lines.append("```")
        lines.append("")
    if omitted:
        lines.append(f"NOTE: {len(omitted)} additional file(s) omitted -- exceeded the {MAX_TOTAL_CODE_CHARS}-char budget: {', '.join(omitted)}")

    digest = "\n".join(lines).rstrip()
    if readme:
        digest = f"README:\n{readme}\n\n{'=' * 40}\n\n{digest}"
    elif readme_entry:
        digest = f"README: present ({readme_entry['path']}) but could not be decoded as text\n\n{'=' * 40}\n\n{digest}"
    else:
        digest = f"README: none found in this repo\n\n{'=' * 40}\n\n{digest}"
    return digest


def _get_latest_commit(owner: str, repo: str, ref: str | None) -> dict:
    query = f"?sha={ref}&per_page=1" if ref else "?per_page=1"
    commits = json.loads(_github_request(f"/repos/{owner}/{repo}/commits{query}"))
    if not commits:
        raise RuntimeError(f"no commits found for {owner}/{repo}" + (f" @ {ref}" if ref else ""))
    c = commits[0]
    return {
        "sha": c["sha"],
        "author": c["commit"]["author"]["name"],
        "date": c["commit"]["author"]["date"],
        "message": c["commit"]["message"].splitlines()[0],
    }


def _get_full_tree(owner: str, repo: str, commit_sha: str) -> tuple[list[dict], bool]:
    data = json.loads(_github_request(f"/repos/{owner}/{repo}/git/trees/{commit_sha}?recursive=1"))
    return data.get("tree", []), bool(data.get("truncated"))


def _fetch_blob_text(owner: str, repo: str, blob_sha: str) -> str | None:
    """Returns the blob's decoded text, or None if it looks binary /
    doesn't decode as UTF-8 -- caller is responsible for noting the
    omission rather than silently dropping it."""
    data = json.loads(_github_request(f"/repos/{owner}/{repo}/git/blobs/{blob_sha}"))
    raw = base64.b64decode(data["content"])
    if b"\x00" in raw[:1024]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _should_skip(rel_path: str) -> bool:
    basename = rel_path.rsplit("/", 1)[-1]
    if basename in SKIP_BASENAMES or basename in README_NAMES:
        return True
    _, _, ext = basename.rpartition(".")
    return bool(ext) and f".{ext.lower()}" in SKIP_EXTENSIONS


def _metric_keywords(metric_name: str) -> list[str]:
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+", metric_name)
    keywords = {w.lower() for w in words}
    for word in list(keywords):
        keywords.update(METRIC_HINTS.get(word, []))
    return sorted(keywords)


def _score_relevance(rel_path: str, content: str, keywords: list[str]) -> int:
    if not keywords:
        return 0
    score = 0
    haystack_path = rel_path.lower()
    haystack_content = content.lower()
    for kw in keywords:
        score += haystack_path.count(kw) * 5   # a matching filename/path is a stronger signal
        score += haystack_content.count(kw)
    return score


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
