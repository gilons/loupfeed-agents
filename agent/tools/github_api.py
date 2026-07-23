"""Tool: ``github_api``. Read-only GitHub REST access as the platform's GitHub App.

The pm agent runs on the same platform as the coding agent and shares its
GitHub App identity — so instead of guessing (or web-searching) facts about
repos, members, issues, or PRs, it can ask GitHub directly. GET-only by
design: mutations stay with the coding agent's git flow.
"""

from __future__ import annotations

import json
from typing import Any

import requests
from langgraph.config import get_config

from ..utils.github_checks import github_headers

_GITHUB_API = "https://api.github.com"
_MAX_RESPONSE_CHARS = 60_000
_TIMEOUT = 20


def _token() -> str | None:
    config = get_config()
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    if not isinstance(configurable, dict):
        configurable = {}
    token = configurable.get("chat_github_token")
    return token if isinstance(token, str) and token else None


def github_api(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a read-only (GET) GitHub REST endpoint, authenticated as the org's GitHub App.

    This sees the organization's private repos and membership — use it to
    VERIFY GitHub facts instead of guessing or web-searching. Useful endpoints:

    - ``/orgs/{org}/members`` — list org members (logins)
    - ``/orgs/{org}/members/{username}`` — membership check (status 204 = member, 404 = not)
    - ``/orgs/{org}/repos`` — repositories
    - ``/repos/{owner}/{repo}/issues?state=open`` — issues and PRs
    - ``/repos/{owner}/{repo}/commits?author={login-or-email}`` — commit history
    - ``/search/issues?q=...``, ``/search/code?q=...`` — search
    - ``/users/{username}`` — a user's public profile

    Args:
        path: The API path, starting with ``/`` (e.g. ``/orgs/dinolabdev/repos``).
        params: Optional query parameters.

    Returns:
        ``{"status": int, "body": <parsed JSON or text>}``. Only GET is
        possible with this tool; a 404 on a membership check means "not a
        member" rather than an error.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        return {"status": 0, "body": "path must start with '/' (e.g. /orgs/dinolabdev/repos)"}
    token = _token()
    if not token:
        return {"status": 0, "body": "GitHub App token unavailable for this run"}
    try:
        resp = requests.get(
            f"{_GITHUB_API}{path}",
            headers=github_headers(token),
            params=params or {},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        return {"status": 0, "body": f"request failed: {exc}"}

    if resp.status_code == 204 or not resp.content:
        return {"status": resp.status_code, "body": ""}
    try:
        body: Any = resp.json()
        text = json.dumps(body)
    except ValueError:
        text = resp.text
        body = text
    if len(text) > _MAX_RESPONSE_CHARS:
        return {
            "status": resp.status_code,
            "body": text[:_MAX_RESPONSE_CHARS],
            "truncated": True,
        }
    return {"status": resp.status_code, "body": body}
