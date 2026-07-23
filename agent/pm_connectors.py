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

logger = logging.getLogger(__name__)

DEFAULT_ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"

# MCP tool listing does a network handshake; cache briefly so back-to-back runs
# don't pay it every time. Failures are cached too (avoid hammering a dead
# endpoint every run) but for a shorter window.
_CACHE_TTL_SECONDS = 300
_FAILURE_TTL_SECONDS = 60
_cache: tuple[float, float, list[BaseTool]] | None = None  # (at, ttl, tools)


def _connections() -> dict[str, dict]:
    connections: dict[str, dict] = {}
    url = os.environ.get("ATLASSIAN_MCP_URL", DEFAULT_ATLASSIAN_MCP_URL)
    bearer = os.environ.get("ATLASSIAN_MCP_BEARER", "")
    email = os.environ.get("ATLASSIAN_EMAIL", "")
    api_token = os.environ.get("ATLASSIAN_API_TOKEN", "")
    headers: dict[str, str] | None = None
    if bearer:
        headers = {"Authorization": f"Bearer {bearer}"}
    elif email and api_token:
        basic = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        headers = {"Authorization": f"Basic {basic}"}
    if headers is not None:
        connections["atlassian"] = {
            "transport": "streamable_http",
            "url": url,
            "headers": headers,
        }
    return connections


def connector_names() -> list[str]:
    return sorted(_connections())


async def load_connector_tools() -> list[BaseTool]:
    """Load tools from every configured MCP connector; [] when none/unreachable."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _cache[1]:
        return _cache[2]

    connections = _connections()
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
