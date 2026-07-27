"""App-only Microsoft Graph token for the Teams bot's Entra app registration.

Reuses the bot's existing credentials (``TEAMS_APP_ID`` / ``TEAMS_APP_PASSWORD``
/ ``TEAMS_APP_TENANT_ID``): the same app registration carries the Teams RSC
grants (granted per team/chat at app install) and any tenant application
permissions Giles consents to in Entra. Client-credentials flow; token cached
in-process until near expiry. The token value is never logged.
"""

from __future__ import annotations

import os
import threading
import time

import requests

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

_token_lock = threading.Lock()
_token: dict | None = None  # {value, expires_at}


def graph_configured() -> bool:
    return bool(
        os.environ.get("TEAMS_APP_ID")
        and os.environ.get("TEAMS_APP_PASSWORD")
        and os.environ.get("TEAMS_APP_TENANT_ID")
    )


def get_graph_app_token() -> str | None:
    """Client-credentials Graph token, or None when Teams creds are absent."""
    global _token
    if not graph_configured():
        return None
    with _token_lock:
        if _token and time.time() < _token["expires_at"] - 60:
            return _token["value"]
        tenant = os.environ["TEAMS_APP_TENANT_ID"]
        resp = requests.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": os.environ["TEAMS_APP_ID"],
                "client_secret": os.environ["TEAMS_APP_PASSWORD"],
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        _token = {
            "value": data["access_token"],
            "expires_at": time.time() + float(data.get("expires_in") or 3600),
        }
        return _token["value"]
