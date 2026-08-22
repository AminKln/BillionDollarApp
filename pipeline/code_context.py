"""
code_context.py

Pulls REAL codebase context from a git checkout of the app that fired an
alarm: the full current source tree (not commit-by-commit diffs -- for
diagnosis, what the code *is* right now matters more than what changed in
any one PR/commit), plus a README/docs summary and a one-line "latest
commit" note so the LLM knows how fresh the snapshot is.

Works against a local checkout (GIT_REPO_PATH) or a clone URL -- if the
path doesn't exist locally it's cloned into a temp directory. Uses the
system `git` binary via subprocess; no GitPython dependency.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MAX_TOTAL_CODE_CHARS = 40_000    # keeps the whole-codebase dump inside a sane prompt budget
MAX_FILE_CHARS = 12_000          # caps any single file so one huge file can't eat the whole budget
README_MAX_CHARS = 3_000

README_CANDIDATES = {"README.md", "README.rst", "README", "readme.md"}
DOC_CANDIDATES = {"docs/architecture.md", "ARCHITECTURE.md", "docs/README.md"}

SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".zip", ".tar", ".gz",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mov", ".pyc", ".class", ".jar",
    ".so", ".dll", ".exe", ".lock",
}
SKIP_BASENAMES = {"package-lock.json", "yarn.lock", "poetry.lock", "Pipfile.lock", "Cargo.lock"}

# metric-name keyword -> related terms, used to prioritize which files stay
# in full when the codebase exceeds MAX_TOTAL_CODE_CHARS -- not to filter
# anything out outright, every file is included unless the budget runs out.
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


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------

@dataclass
class SourceFile:
    path: str
    content: Optional[str]   # None if budget-trimmed out entirely
    truncated: bool = False
    relevance_score: int = 0


@dataclass
class LatestCommit:
    sha: str
    author: str
    date: str
    message: str


@dataclass
class CodeContext:
    repo_path: str
    remote_url: Optional[str]
    readme: Optional[str]
    latest_commit: Optional[LatestCommit]
    files: list[SourceFile]
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------

def _run_git(args: list[str], cwd: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def ensure_local_checkout(repo_path_or_url: str) -> str:
    """Returns a local path to a git checkout: uses `repo_path_or_url`
    directly if it's already a local git repo, otherwise clones it (a URL,
    or a path that doesn't exist yet) into a temp directory."""
    looks_like_url = repo_path_or_url.startswith(("http://", "https://", "git@"))
    local_path = Path(repo_path_or_url)

    if not looks_like_url and local_path.is_dir() and (local_path / ".git").exists():
        try:
            _run_git(["fetch", "--quiet"], cwd=str(local_path))
        except (RuntimeError, subprocess.TimeoutExpired):
            pass  # best-effort freshness; stale local history is still usable
        return str(local_path)

    if not looks_like_url:
        raise ValueError(f"{repo_path_or_url!r} is not a local git checkout and doesn't look like a clone URL")

    dest = tempfile.mkdtemp(prefix="code_context_")
    _run_git(["clone", "--quiet", repo_path_or_url, dest], cwd=".")
    return dest


def _get_remote_url(repo_path: str) -> Optional[str]:
    try:
        return _run_git(["remote", "get-url", "origin"], cwd=repo_path).strip() or None
    except RuntimeError:
        return None


def get_latest_commit(repo_path: str) -> Optional[LatestCommit]:
    out = _run_git(["log", "-1", "--pretty=format:%H%x1f%an%x1f%aI%x1f%s"], cwd=repo_path)
    if not out.strip():
        return None
    sha, author, date, message = out.split("\x1f", 3)
    return LatestCommit(sha=sha, author=author, date=date, message=message)


# ---------------------------------------------------------------------------
# source file collection + relevance-ordered budget trimming
# ---------------------------------------------------------------------------

def list_tracked_files(repo_path: str) -> list[str]:
    out = _run_git(["ls-files"], cwd=repo_path)
    return [f for f in out.splitlines() if f.strip()]


def _is_probably_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:1024]
    except OSError:
        return True
    return b"\x00" in chunk


def _is_readme_or_doc(rel_path: str) -> bool:
    return Path(rel_path).name in README_CANDIDATES or rel_path in DOC_CANDIDATES


def _should_skip(rel_path: str) -> bool:
    p = Path(rel_path)
    if p.name in SKIP_BASENAMES or p.suffix.lower() in SKIP_EXTENSIONS:
        return True
    return _is_readme_or_doc(rel_path)  # README/docs are rendered in their own section, not the file dump


def _metric_keywords(metric_name: str) -> list[str]:
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+", metric_name)
    keywords = {w.lower() for w in words}
    for word in list(keywords):
        keywords.update(METRIC_HINTS.get(word, []))
    return sorted(keywords)


def score_relevance(rel_path: str, content: str, keywords: list[str]) -> int:
    if not keywords:
        return 0
    score = 0
    haystack_path = rel_path.lower()
    haystack_content = content.lower()
    for kw in keywords:
        score += haystack_path.count(kw) * 5   # a matching filename/path is a stronger signal
        score += haystack_content.count(kw)
    return score


def collect_source_files(repo_path: str, metric_name: Optional[str] = None) -> tuple[list[SourceFile], Optional[str]]:
    """Reads every tracked, non-binary, non-skipped file in full. If the
    total exceeds MAX_TOTAL_CODE_CHARS, files are ranked by relevance to
    the alarm's metric and the lowest-relevance files are the ones dropped
    (content=None) or truncated first -- everything still gets a line in
    the prompt, nothing is silently missing."""
    root = Path(repo_path)
    tracked = list_tracked_files(repo_path)
    keywords = _metric_keywords(metric_name) if metric_name else []

    candidates = []
    for rel_path in tracked:
        if _should_skip(rel_path):
            continue
        full = root / rel_path
        if not full.is_file() or _is_probably_binary(full):
            continue
        candidates.append((rel_path, full.read_text(errors="replace")))

    scored = sorted(
        ((rel_path, content, score_relevance(rel_path, content, keywords)) for rel_path, content in candidates),
        key=lambda t: (-t[2], t[0]),
    )

    files: list[SourceFile] = []
    budget = MAX_TOTAL_CODE_CHARS
    for rel_path, content, score in scored:
        if budget <= 0:
            files.append(SourceFile(path=rel_path, content=None, relevance_score=score))
            continue
        capped = content[:MAX_FILE_CHARS]
        take = capped[:budget]
        truncated = len(content) > len(take)
        budget -= len(take)
        files.append(SourceFile(path=rel_path, content=take, truncated=truncated, relevance_score=score))

    note = None
    if any(f.content is None for f in files):
        note = f"codebase exceeds the {MAX_TOTAL_CODE_CHARS}-char prompt budget -- lowest-relevance files omitted below"

    order_index = {p: i for i, p in enumerate(tracked)}
    files.sort(key=lambda f: order_index.get(f.path, 0))  # restore repo order for a readable prompt
    return files, note


def get_readme(repo_path: str) -> Optional[str]:
    root = Path(repo_path)
    for candidate in [*README_CANDIDATES, *DOC_CANDIDATES]:
        path = root / candidate
        if path.is_file():
            text = path.read_text(errors="replace")
            return text[:README_MAX_CHARS] + ("\n...[truncated]" if len(text) > README_MAX_CHARS else "")
    return None


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def build_code_context(repo_path_or_url: str, metric_name: Optional[str] = None) -> CodeContext:
    repo_path = ensure_local_checkout(repo_path_or_url)
    remote_url = _get_remote_url(repo_path)
    latest_commit = get_latest_commit(repo_path)
    readme = get_readme(repo_path)
    files, note = collect_source_files(repo_path, metric_name=metric_name)

    return CodeContext(repo_path=repo_path, remote_url=remote_url, readme=readme, latest_commit=latest_commit, files=files, note=note)


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Fetch the real current codebase from a git repo")
    parser.add_argument("repo_path_or_url")
    parser.add_argument("--metric-name", default=None)
    args = parser.parse_args()

    ctx = build_code_context(args.repo_path_or_url, metric_name=args.metric_name)
    print(json.dumps(ctx, default=lambda o: o.__dict__, indent=2))
