"""Atlassian REST access, through the entry app when it is available.

The entry app (Forge) exposes an allowlisted proxy web trigger that performs
requests with ``asApp()``. Routing through it means a deployment needs no
Atlassian credential of its own, and everything the agents touch is
attributed to the app. When no proxy is configured the deployment's own
Basic-auth credential is used, so installs without the app keep working.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 30


class AtlassianResponse:
    """The bits of a response callers actually use, from either path."""

    def __init__(self, status_code: int, text: str, *, via_app: bool) -> None:
        self.status_code = status_code
        self.text = text
        self.via_app = via_app

    def json(self) -> Any:
        return json.loads(self.text) if self.text else {}

    @property
    def ok(self) -> bool:
        return self.status_code < 400


def proxy_url() -> str:
    return os.environ.get("ATLASSIAN_APP_PROXY_URL", "").strip()


def shared_secret() -> str:
    return os.environ.get("ATLASSIAN_APP_SHARED_SECRET", "")


def site_base() -> str:
    return os.environ.get("ATLASSIAN_SITE_URL", "https://dinolabgmbh.atlassian.net").rstrip("/")


def basic_auth() -> tuple[str, str] | None:
    email = os.environ.get("ATLASSIAN_EMAIL", "")
    token = os.environ.get("ATLASSIAN_API_TOKEN", "")
    return (email, token) if email and token else None


def _via_app(product: str, method: str, path: str, body: Any | None) -> AtlassianResponse | None:
    url, secret = proxy_url(), shared_secret()
    if not url or not secret:
        return None
    payload: dict[str, Any] = {"product": product, "method": method, "path": path}
    if body is not None:
        payload["body"] = body
    try:
        r = requests.post(
            url,
            headers={"Content-Type": "application/json", "X-Loupfeed-Secret": secret},
            json=payload,
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("atlassian proxy unreachable: %s: %s", type(exc).__name__, exc)
        return None
    if r.status_code == 403:
        # The app refused the path: a deliberate allowlist decision, not a
        # transport failure, so do not quietly widen access via the fallback.
        logger.warning("atlassian proxy refused %s %s", method, path)
        return AtlassianResponse(403, r.text, via_app=True)
    if r.status_code >= 400:
        logger.warning("atlassian proxy error %s for %s %s", r.status_code, method, path)
        return None
    envelope = r.json()
    return AtlassianResponse(
        int(envelope.get("status", 502)), str(envelope.get("body", "")), via_app=True
    )


def atlassian_request(
    product: str, method: str, path: str, body: Any | None = None
) -> AtlassianResponse:
    """Perform an Atlassian request, preferring the entry app.

    Args:
        product: ``"jira"`` or ``"confluence"``.
        method: HTTP method.
        path: Site-relative path, e.g. ``/wiki/api/v2/pages/123?body-format=storage``.
        body: JSON body, when the method takes one.
    """
    method = method.upper()
    through_app = _via_app(product, method, path, body)
    if through_app is not None:
        return through_app

    auth = basic_auth()
    if not auth:
        logger.warning("atlassian request skipped: no app proxy and no credential")
        return AtlassianResponse(503, "", via_app=False)
    try:
        r = requests.request(
            method,
            f"{site_base()}{path}",
            auth=auth,
            json=body,
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("atlassian request failed: %s: %s", type(exc).__name__, exc)
        return AtlassianResponse(502, "", via_app=False)
    return AtlassianResponse(r.status_code, r.text, via_app=False)
