"""MCP connector registry for the pm graph.

v1 is a small env-driven registry; connector #1 is Atlassian's Rovo MCP server
(Jira / Confluence / Compass) with headless API-token auth:

- Basic: ``ATLASSIAN_EMAIL`` + ``ATLASSIAN_API_TOKEN`` (personal API token)
- Bearer: ``ATLASSIAN_MCP_BEARER`` (service-account API key)

``ATLASSIAN_MCP_URL`` overrides the endpoint. Additional connectors (Notion,
Linear, ...) become new entries in :func:`_connections`. Interactive OAuth 2.1
collected from chat surfaces arrives with the Teams adapter (M2); a token store
then replaces the env lookup behind the same function.
"""

from __future__ import annotations

import base64
import logging
import os
import time

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from .connector_auth import connector_registry, get_access_token

logger = logging.getLogger(__name__)

DEFAULT_ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp/authv2"

# MCP tool listing does a network handshake; cache briefly so back-to-back runs
# don't pay it every time. Failures are cached too (avoid hammering a dead
# endpoint every run) but for a shorter window.
_CACHE_TTL_SECONDS = 300
_FAILURE_TTL_SECONDS = 60
_cache: tuple[float, float, list[BaseTool]] | None = None  # (at, ttl, tools)


# Cloudflare fronts the Atlassian MCP endpoint and rejects requests whose
# User-Agent looks like a bare HTTP library (error 1010, a 403 that names no
# cause). Every MCP request must carry a real one.
USER_AGENT = os.environ.get("LOUPFEED_USER_AGENT", "loupfeed-agents/1.0 (+https://loupfeed.dev)")


def prefer_token_auth() -> bool:
    """Whether a configured API token outranks a stored OAuth grant.

    Token auth is the supported machine-to-machine path for the Atlassian MCP
    server and needs no interactive consent, no dynamic client registration
    and no per-org callback-domain allowlist, so it is preferred by default.
    Set LOUPFEED_MCP_PREFER_OAUTH=1 to go back to the OAuth grant.
    """
    return os.environ.get("LOUPFEED_MCP_PREFER_OAUTH", "").strip() not in ("1", "true", "yes")


def token_headers(name: str) -> dict[str, str] | None:
    """API-token auth for a connector, when one is configured."""
    if name != "atlassian":
        return None
    bearer = os.environ.get("ATLASSIAN_MCP_BEARER", "")
    email = os.environ.get("ATLASSIAN_EMAIL", "")
    api_token = os.environ.get("ATLASSIAN_API_TOKEN", "")
    if bearer:
        return {"Authorization": f"Bearer {bearer}"}
    if email and api_token:
        basic = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        return {"Authorization": f"Basic {basic}"}
    return None


async def _auth_headers(name: str) -> tuple[dict[str, str] | None, str]:
    """Headers for a connector plus which credential they came from."""
    token = token_headers(name)
    if token is not None and prefer_token_auth():
        return token, "token"
    oauth_token = await get_access_token(name)
    if oauth_token:
        return {"Authorization": f"Bearer {oauth_token}"}, "oauth"
    return (token, "token") if token is not None else (None, "none")


async def _connections() -> dict[str, dict]:
    connections: dict[str, dict] = {}
    for name, url in connector_registry().items():
        headers, source = await _auth_headers(name)
        if headers is not None:
            logger.info("connector %s: authenticating with %s auth", name, source)
            connections[name] = {
                "transport": "streamable_http",
                "url": url,
                "headers": {**headers, "User-Agent": USER_AGENT},
            }
    return connections


def connector_names() -> list[str]:
    return sorted(connector_registry())


async def load_connector_tools() -> list[BaseTool]:
    """Load tools from every configured MCP connector; [] when none/unreachable."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _cache[1]:
        return _cache[2]

    connections = await _connections()
    if not connections:
        logger.info(
            "pm: no MCP connectors configured "
            "(set ATLASSIAN_EMAIL + ATLASSIAN_API_TOKEN, or ATLASSIAN_MCP_BEARER)"
        )
        _cache = (now, _CACHE_TTL_SECONDS, [])
        return []

    try:
        client = MultiServerMCPClient(connections)
        tools = await client.get_tools()
    except Exception:
        logger.exception("pm: failed to load MCP connector tools; continuing without them")
        _cache = (now, _FAILURE_TTL_SECONDS, [])
        return []

    logger.info("pm: loaded %d MCP tool(s) from: %s", len(tools), ", ".join(connections))
    _cache = (now, _CACHE_TTL_SECONDS, tools)
    return tools
