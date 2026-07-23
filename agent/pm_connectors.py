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

DEFAULT_ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"

# MCP tool listing does a network handshake; cache briefly so back-to-back runs
# don't pay it every time. Failures are cached too (avoid hammering a dead
# endpoint every run) but for a shorter window.
_CACHE_TTL_SECONDS = 300
_FAILURE_TTL_SECONDS = 60
_cache: tuple[float, float, list[BaseTool]] | None = None  # (at, ttl, tools)


def _fallback_headers(name: str) -> dict[str, str] | None:
    """Service-account fallback (env) — the platform policy is MCP OAuth 2.1
    via the token store; these envs exist for headless/CI deployments only."""
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


async def _connections() -> dict[str, dict]:
    connections: dict[str, dict] = {}
    for name, url in connector_registry().items():
        oauth_token = await get_access_token(name)
        headers = (
            {"Authorization": f"Bearer {oauth_token}"} if oauth_token else _fallback_headers(name)
        )
        if headers is not None:
            connections[name] = {
                "transport": "streamable_http",
                "url": url,
                "headers": headers,
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
