"""Tools: pin a symptom to the commits that could have caused it.

All four go through the GitHub API rather than a clone, so triage needs no
sandbox and can reach across repositories in one run. Every tool takes the
repository explicitly (``owner/name``) for the same reason.

The one rule these tools are shaped around: a report's line numbers are only
valid at the commit that built it. Blaming them at the head of the default
branch names a real commit, plausibly, and wrongly. So ``ref`` is a required
argument on ``git_blame_line`` and there is no default for it.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from langgraph.config import get_config

from ..utils.github_checks import github_headers

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_GITHUB_GRAPHQL = "https://api.github.com/graphql"
_TIMEOUT = 30
_MAX_PATCH_CHARS = 30_000

_BLAME_QUERY = """
query($owner: String!, $name: String!, $ref: String!, $path: String!) {
  repository(owner: $owner, name: $name) {
    object(expression: $ref) {
      ... on Commit {
        blame(path: $path) {
          ranges {
            startingLine
            endingLine
            commit {
              oid
              messageHeadline
              committedDate
              author { name email }
            }
          }
        }
      }
    }
  }
}
"""


def _token(repo: str) -> str | None:
    """A token for this repository, from the per-repo map the graph seeds."""
    config = get_config()
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    if not isinstance(configurable, dict):
        configurable = {}
    tokens = configurable.get("triage_github_tokens")
    if isinstance(tokens, dict):
        token = tokens.get(repo)
        if isinstance(token, str) and token:
            return token
    fallback = configurable.get("chat_github_token")
    return fallback if isinstance(fallback, str) and fallback else None


def _split(repo: str) -> tuple[str, str]:
    owner, _, name = (repo or "").strip().partition("/")
    return owner, name


def _rest(repo: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    owner, name = _split(repo)
    if not owner or not name:
        return {"ok": False, "error": "repo must look like 'owner/name'"}
    token = _token(repo)
    if not token:
        return {"ok": False, "error": f"no GitHub token available for {repo}"}
    try:
        response = requests.get(
            f"{_GITHUB_API}/repos/{owner}/{name}{path}",
            headers=github_headers(token),
            params=params or {},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("github request failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": "GitHub could not be reached"}
    if response.status_code == 404:
        return {"ok": False, "error": f"not found in {repo}"}
    if response.status_code >= 400:
        return {"ok": False, "error": f"GitHub returned {response.status_code}"}
    try:
        return {"ok": True, "body": response.json()}
    except ValueError:
        return {"ok": False, "error": "GitHub returned a non-JSON body"}


def _commit_row(commit: dict[str, Any]) -> dict[str, Any]:
    detail = commit.get("commit") if isinstance(commit.get("commit"), dict) else {}
    author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
    message = str(detail.get("message") or "")
    return {
        "sha": commit.get("sha"),
        "author": author.get("name"),
        "date": author.get("date"),
        "headline": message.splitlines()[0] if message else "",
        "url": commit.get("html_url"),
    }


def git_blame_line(repo: str, path: str, line: int, ref: str) -> dict[str, Any]:
    """Name the commit that last touched one line, as of one exact commit.

    This is the strongest pin available: given a report's release
    (``<surface>@<commit>``) and its resolved source (``path:line``), this
    returns the commit responsible for that line in the build the reporter was
    actually running.

    ``ref`` is required and should be the release's commit sha. Passing a branch
    name instead makes the answer meaningless whenever the file has moved since
    the build, because the line number belongs to the older tree.

    Args:
        repo: ``owner/name``.
        path: Repository-relative path (apply the surface's build root first).
        line: 1-based line number from the report.
        ref: The commit sha the report came from.

    Returns:
        ``{ok, commit: {sha, author, date, headline}, range}`` or
        ``{ok: False, error}``.
    """
    owner, name = _split(repo)
    if not owner or not name:
        return {"ok": False, "error": "repo must look like 'owner/name'"}
    token = _token(repo)
    if not token:
        return {"ok": False, "error": f"no GitHub token available for {repo}"}
    if not ref:
        return {"ok": False, "error": "ref is required: blame at the report's commit, not at main"}
    try:
        response = requests.post(
            _GITHUB_GRAPHQL,
            headers=github_headers(token),
            json={
                "query": _BLAME_QUERY,
                "variables": {"owner": owner, "name": name, "ref": ref, "path": path},
            },
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("github blame failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": "GitHub could not be reached"}
    if response.status_code >= 400:
        return {"ok": False, "error": f"GitHub returned {response.status_code}"}
    payload = response.json()
    if payload.get("errors"):
        first = payload["errors"][0].get("message", "blame query rejected")
        return {"ok": False, "error": f"GitHub: {first}"}
    obj = ((payload.get("data") or {}).get("repository") or {}).get("object")
    ranges = ((obj or {}).get("blame") or {}).get("ranges")
    if not isinstance(ranges, list) or not ranges:
        return {
            "ok": False,
            "error": f"no blame for {path} at {ref} (wrong path for this commit, or the file did not exist yet)",
        }
    for entry in ranges:
        if not isinstance(entry, dict):
            continue
        start, end = entry.get("startingLine"), entry.get("endingLine")
        if isinstance(start, int) and isinstance(end, int) and start <= line <= end:
            commit = entry.get("commit") or {}
            author = commit.get("author") or {}
            return {
                "ok": True,
                "repo": repo,
                "path": path,
                "line": line,
                "ref": ref,
                "commit": {
                    "sha": commit.get("oid"),
                    "author": author.get("name"),
                    "email": author.get("email"),
                    "date": commit.get("committedDate"),
                    "headline": commit.get("messageHeadline"),
                },
                "range": [start, end],
            }
    return {"ok": False, "error": f"line {line} is past the end of {path} at {ref}"}


def git_commits_touching(
    repo: str,
    path: str,
    ref: str | None = None,
    since: str | None = None,
    until: str | None = None,
    max_results: int = 20,
) -> dict[str, Any]:
    """List commits that touched a path, newest first, optionally date-bounded.

    File-level, not line-level: use it to widen from the blamed commit to the
    other changes around it, then read the diffs. Bound it with ``since`` and
    ``until`` (ISO 8601) when a report tells you when the symptom appeared, so
    the candidate set stays small.

    Args:
        repo: ``owner/name``.
        path: Repository-relative path, or a directory.
        ref: Branch or sha to walk back from. Defaults to the default branch.
        since: Only commits after this ISO timestamp.
        until: Only commits before this ISO timestamp.
        max_results: Capped at 50.

    Returns:
        ``{ok, commits: [{sha, author, date, headline, url}]}``.
    """
    params: dict[str, Any] = {"path": path, "per_page": max(1, min(max_results, 50))}
    if ref:
        params["sha"] = ref
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    fetched = _rest(repo, "/commits", params)
    if not fetched["ok"]:
        return fetched
    body = fetched["body"]
    commits = (
        [_commit_row(c) for c in body if isinstance(c, dict)] if isinstance(body, list) else []
    )
    return {"ok": True, "repo": repo, "path": path, "commits": commits}


def git_commit_diff(repo: str, sha: str, path: str | None = None) -> dict[str, Any]:
    """Read a commit's actual diff. Never name a suspect commit without this.

    A ranked list of shas nobody opened is a guess. This returns the message and
    the patch, so a suspect can be described in terms of what it changed and how
    that produces the reported symptom.

    Args:
        repo: ``owner/name``.
        sha: The commit to read.
        path: Only this file's patch, when the commit is large.

    Returns:
        ``{ok, sha, headline, message, author, date, files: [{path, status,
        additions, deletions, patch}]}``.
    """
    fetched = _rest(repo, f"/commits/{sha}")
    if not fetched["ok"]:
        return fetched
    body = fetched["body"]
    if not isinstance(body, dict):
        return {"ok": False, "error": "unexpected commit payload"}
    row = _commit_row(body)
    detail = body.get("commit") if isinstance(body.get("commit"), dict) else {}
    files = []
    budget = _MAX_PATCH_CHARS
    for entry in body.get("files", []) if isinstance(body.get("files"), list) else []:
        if not isinstance(entry, dict):
            continue
        if path and entry.get("filename") != path:
            continue
        patch = str(entry.get("patch") or "")
        truncated = len(patch) > budget
        if truncated:
            patch = patch[:budget]
        budget = max(0, budget - len(patch))
        files.append(
            {
                "path": entry.get("filename"),
                "status": entry.get("status"),
                "additions": entry.get("additions"),
                "deletions": entry.get("deletions"),
                "patch": patch,
                "patch_truncated": truncated,
            }
        )
    return {
        "ok": True,
        "repo": repo,
        "sha": row["sha"],
        "headline": row["headline"],
        "message": str(detail.get("message") or ""),
        "author": row["author"],
        "date": row["date"],
        "url": row["url"],
        "files": files,
        "files_omitted": (
            len(body.get("files", [])) - len(files) if isinstance(body.get("files"), list) else 0
        ),
    }


def git_compare(
    repo: str, base: str, head: str, path_prefix: str | None = None, max_commits: int = 50
) -> dict[str, Any]:
    """Everything that landed between two commits: the introduced-in window.

    Given a last-known-good release and a first-bad one, both of which a
    loupfeed release string already carries as shas, this is the set the culprit
    must be in. Intersect it with the blamed file to get a short list.

    Args:
        repo: ``owner/name``.
        base: The good commit sha (or tag/branch).
        head: The bad commit sha.
        path_prefix: Only report files under this prefix.
        max_commits: Capped at 100.

    Returns:
        ``{ok, ahead_by, commits: [...], files: [{path, status, additions,
        deletions}]}``.
    """
    fetched = _rest(repo, f"/compare/{base}...{head}")
    if not fetched["ok"]:
        return fetched
    body = fetched["body"]
    if not isinstance(body, dict):
        return {"ok": False, "error": "unexpected compare payload"}
    commits = [_commit_row(c) for c in body.get("commits", []) if isinstance(c, dict)][
        -max(1, min(max_commits, 100)) :
    ]
    files = []
    for entry in body.get("files", []) if isinstance(body.get("files"), list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("filename") or "")
        if path_prefix and not name.startswith(path_prefix):
            continue
        files.append(
            {
                "path": name,
                "status": entry.get("status"),
                "additions": entry.get("additions"),
                "deletions": entry.get("deletions"),
            }
        )
    return {
        "ok": True,
        "repo": repo,
        "base": base,
        "head": head,
        "status": body.get("status"),
        "ahead_by": body.get("ahead_by"),
        "commits": commits,
        "files": files,
    }
