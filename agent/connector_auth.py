"""OAuth 2.1 client + token store for MCP connectors.

Platform policy: **every connector authenticates via MCP OAuth 2.1** (RFC 8414
discovery, RFC 7591 dynamic client registration, PKCE, RFC 8707 resource
indicators). A user connects from a chat surface (e.g. the Teams adapter sends
them to ``/connectors/{name}/start``); the callback stores the tokens, and —
v1 policy — that single connection is shared org-wide.

Tokens and client registrations are stored under ``CONNECTOR_STORE_DIR``
(default ``.connectors/`` in the working directory) with secret fields
encrypted at rest via ``agent.encryption`` (``TOKEN_ENCRYPTION_KEY``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets as pysecrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx

from .encryption import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)

CLIENT_NAME = "loupfeed agents"
DEFAULT_ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp/authv2"

_PENDING_TTL_SECONDS = 900
_REFRESH_SKEW_SECONDS = 120

_lock = asyncio.Lock()
_pending: dict[str, dict] = {}  # state -> {connector, verifier, redirect_uri, at}


def connector_registry() -> dict[str, str]:
    """Known MCP connectors: name -> MCP endpoint URL."""
    registry = {
        "atlassian": os.environ.get("ATLASSIAN_MCP_URL", DEFAULT_ATLASSIAN_MCP_URL),
    }
    # Extra connectors without code changes: MCP_CONNECTOR_<NAME>_URL=...
    for key, value in os.environ.items():
        if key.startswith("MCP_CONNECTOR_") and key.endswith("_URL") and value:
            name = key[len("MCP_CONNECTOR_") : -len("_URL")].lower()
            registry[name] = value
    return registry


def _store_dir() -> Path:
    d = Path(os.environ.get("CONNECTOR_STORE_DIR", ".connectors"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record_path(connector: str) -> Path:
    return _store_dir() / f"{connector}.json"


async def _aload_record(connector: str) -> dict | None:
    return await asyncio.to_thread(_load_record, connector)


async def _asave_record(connector: str, record: dict) -> None:
    await asyncio.to_thread(_save_record, connector, record)


def _load_record(connector: str) -> dict | None:
    try:
        return json.loads(_record_path(connector).read_text())
    except (OSError, ValueError):
        return None


def _save_record(connector: str, record: dict) -> None:
    path = _record_path(connector)
    path.write_text(json.dumps(record))
    path.chmod(0o600)


async def _fetch_json(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        resp = await client.get(url, headers={"Accept": "application/json"})
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
    except (httpx.HTTPError, ValueError):
        pass
    return None


async def _discover(mcp_url: str) -> dict:
    """Resolve authorization/token/registration endpoints for an MCP server."""
    parsed = urlparse(mcp_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resource_md = None
        for candidate in (
            f"{origin}/.well-known/oauth-protected-resource{path}",
            f"{origin}/.well-known/oauth-protected-resource",
        ):
            resource_md = await _fetch_json(client, candidate)
            if resource_md:
                break

        auth_servers = (resource_md or {}).get("authorization_servers") or [origin]
        auth_base = str(auth_servers[0]).rstrip("/")
        as_parsed = urlparse(auth_base)
        as_origin = f"{as_parsed.scheme}://{as_parsed.netloc}"
        as_path = as_parsed.path.rstrip("/")

        as_md = None
        for candidate in (
            f"{as_origin}/.well-known/oauth-authorization-server{as_path}",
            f"{auth_base}/.well-known/oauth-authorization-server",
            f"{auth_base}/.well-known/openid-configuration",
        ):
            as_md = await _fetch_json(client, candidate)
            if as_md and as_md.get("authorization_endpoint"):
                break

    if not as_md or not as_md.get("authorization_endpoint") or not as_md.get("token_endpoint"):
        raise RuntimeError(f"OAuth discovery failed for MCP server {mcp_url}")

    scopes = (resource_md or {}).get("scopes_supported") or as_md.get("scopes_supported") or []
    return {
        "authorization_endpoint": as_md["authorization_endpoint"],
        "token_endpoint": as_md["token_endpoint"],
        "registration_endpoint": as_md.get("registration_endpoint"),
        "scopes": list(scopes),
    }


async def _ensure_client_registration(
    connector: str, mcp_url: str, redirect_uri: str, meta: dict
) -> str:
    """Return a client_id, registering dynamically (public client) if needed."""
    record = await _aload_record(connector) or {}
    client = record.get("client") or {}
    if client.get("client_id") and redirect_uri in (client.get("redirect_uris") or []):
        return client["client_id"]

    if not meta.get("registration_endpoint"):
        raise RuntimeError(f"{connector}: no registration_endpoint and no stored client_id")

    payload = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(meta["registration_endpoint"], json=payload)
        resp.raise_for_status()
        data = resp.json()

    record["client"] = {
        "client_id": data["client_id"],
        "redirect_uris": data.get("redirect_uris", [redirect_uri]),
    }
    record["mcp_url"] = mcp_url
    await _asave_record(connector, record)
    logger.info("connector %s: registered OAuth client", connector)
    return data["client_id"]


def _prune_pending() -> None:
    cutoff = time.time() - _PENDING_TTL_SECONDS
    for state in [s for s, p in _pending.items() if p["at"] < cutoff]:
        _pending.pop(state, None)


async def start_auth(connector: str, redirect_uri: str) -> str:
    """Begin the PKCE flow; returns the authorization URL to send the user to."""
    registry = connector_registry()
    if connector not in registry:
        raise KeyError(f"unknown connector: {connector}")
    mcp_url = registry[connector]

    meta = await _discover(mcp_url)
    client_id = await _ensure_client_registration(connector, mcp_url, redirect_uri, meta)

    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = pysecrets.token_urlsafe(24)

    async with _lock:
        _prune_pending()
        _pending[state] = {
            "connector": connector,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "token_endpoint": meta["token_endpoint"],
            "client_id": client_id,
            "mcp_url": mcp_url,
            "at": time.time(),
        }

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": mcp_url,
    }
    scopes = meta.get("scopes") or []
    if scopes:
        params["scope"] = " ".join(scopes)
    return f"{meta['authorization_endpoint']}?{urlencode(params)}"


async def finish_auth(state: str, code: str) -> str:
    """Exchange the code and persist the connection. Returns the connector name."""
    async with _lock:
        pending = _pending.pop(state, None)
    if pending is None:
        raise KeyError("unknown or expired OAuth state")

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "client_id": pending["client_id"],
        "code_verifier": pending["verifier"],
        "resource": pending["mcp_url"],
    }
    async with httpx.AsyncClient(timeout=20) as http:
        resp = await http.post(pending["token_endpoint"], data=form)
        resp.raise_for_status()
        tokens = resp.json()

    connector = pending["connector"]
    record = await _aload_record(connector) or {}
    record.update(
        {
            "mcp_url": pending["mcp_url"],
            "token_endpoint": pending["token_endpoint"],
            "access_token_enc": encrypt_token(tokens["access_token"]),
            "refresh_token_enc": (
                encrypt_token(tokens["refresh_token"]) if tokens.get("refresh_token") else None
            ),
            "expires_at": time.time() + float(tokens.get("expires_in") or 3600),
            "connected_at": time.time(),
        }
    )
    record.setdefault("client", {})["client_id"] = pending["client_id"]
    await _asave_record(connector, record)
    logger.info("connector %s: OAuth connection stored", connector)
    return connector


async def get_access_token(connector: str) -> str | None:
    """Valid access token for a connected connector (refreshing as needed)."""
    record = await _aload_record(connector)
    if not record or not record.get("access_token_enc"):
        return None

    if time.time() < float(record.get("expires_at") or 0) - _REFRESH_SKEW_SECONDS:
        return decrypt_token(record["access_token_enc"])

    refresh_enc = record.get("refresh_token_enc")
    if not refresh_enc:
        logger.warning("connector %s: token expired and no refresh token", connector)
        return None

    async with _lock:
        # Re-read inside the lock in case another task refreshed first.
        record = await _aload_record(connector) or record
        if time.time() < float(record.get("expires_at") or 0) - _REFRESH_SKEW_SECONDS:
            return decrypt_token(record["access_token_enc"])
        form = {
            "grant_type": "refresh_token",
            "refresh_token": decrypt_token(record["refresh_token_enc"]),
            "client_id": (record.get("client") or {}).get("client_id", ""),
            "resource": record.get("mcp_url", ""),
        }
        try:
            async with httpx.AsyncClient(timeout=20) as http:
                resp = await http.post(record["token_endpoint"], data=form)
                resp.raise_for_status()
                tokens = resp.json()
        except httpx.HTTPError:
            logger.exception("connector %s: token refresh failed", connector)
            return None

        record["access_token_enc"] = encrypt_token(tokens["access_token"])
        if tokens.get("refresh_token"):
            record["refresh_token_enc"] = encrypt_token(tokens["refresh_token"])
        record["expires_at"] = time.time() + float(tokens.get("expires_in") or 3600)
        await _asave_record(connector, record)
        logger.info("connector %s: access token refreshed", connector)
        return tokens["access_token"]


def connection_status() -> dict[str, dict]:
    """Public (secret-free) connection state for every registered connector."""
    status: dict[str, dict] = {}
    for name, url in connector_registry().items():
        record = _load_record(name) or {}
        status[name] = {
            "mcp_url": url,
            "connected": bool(record.get("access_token_enc")),
            "connected_at": record.get("connected_at"),
            "expires_at": record.get("expires_at"),
            "has_refresh_token": bool(record.get("refresh_token_enc")),
        }
    return status
