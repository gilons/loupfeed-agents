"""MCP connector auth: token-first, with the Cloudflare-safe User-Agent.

Atlassian supports API-token auth for the Rovo MCP server, which needs no
interactive consent, no DCR client and no per-org callback allowlist, so a
configured token outranks a stored OAuth grant. Cloudflare 403s (error 1010)
any request whose User-Agent looks like a bare HTTP library.
"""

from __future__ import annotations

import base64
from unittest.mock import patch

import pytest

from agent import pm_connectors

TOKEN_ENV = {"ATLASSIAN_EMAIL": "loupfeed@dino-lab.io", "ATLASSIAN_API_TOKEN": "tok"}


def test_basic_header_is_built_from_email_and_token():
    with patch.dict("os.environ", TOKEN_ENV, clear=False):
        headers = pm_connectors.token_headers("atlassian")
    expected = base64.b64encode(b"loupfeed@dino-lab.io:tok").decode()
    assert headers == {"Authorization": f"Basic {expected}"}


def test_bearer_service_key_wins_over_basic():
    env = {**TOKEN_ENV, "ATLASSIAN_MCP_BEARER": "svc-key"}
    with patch.dict("os.environ", env, clear=False):
        assert pm_connectors.token_headers("atlassian") == {"Authorization": "Bearer svc-key"}


def test_other_connectors_have_no_token_path():
    with patch.dict("os.environ", TOKEN_ENV, clear=False):
        assert pm_connectors.token_headers("ms365") is None


@pytest.mark.asyncio
async def test_token_auth_is_preferred_over_a_stored_oauth_grant():
    async def _oauth(_name):
        return "oauth-token"

    with (
        patch.dict("os.environ", TOKEN_ENV, clear=False),
        patch.object(pm_connectors, "get_access_token", _oauth),
    ):
        headers, source = await pm_connectors._auth_headers("atlassian")
    assert source == "token" and headers["Authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_oauth_still_used_when_no_token_is_configured():
    async def _oauth(_name):
        return "oauth-token"

    env = {"ATLASSIAN_EMAIL": "", "ATLASSIAN_API_TOKEN": "", "ATLASSIAN_MCP_BEARER": ""}
    with (
        patch.dict("os.environ", env, clear=False),
        patch.object(pm_connectors, "get_access_token", _oauth),
    ):
        headers, source = await pm_connectors._auth_headers("atlassian")
    assert source == "oauth" and headers == {"Authorization": "Bearer oauth-token"}


@pytest.mark.asyncio
async def test_the_escape_hatch_restores_oauth_precedence():
    async def _oauth(_name):
        return "oauth-token"

    env = {**TOKEN_ENV, "LOUPFEED_MCP_PREFER_OAUTH": "1"}
    with (
        patch.dict("os.environ", env, clear=False),
        patch.object(pm_connectors, "get_access_token", _oauth),
    ):
        _, source = await pm_connectors._auth_headers("atlassian")
    assert source == "oauth"


@pytest.mark.asyncio
async def test_every_connection_carries_a_real_user_agent():
    """A bare library UA gets a Cloudflare 1010 that names no cause."""

    async def _no_oauth(_name):
        return ""

    with (
        patch.dict("os.environ", TOKEN_ENV, clear=False),
        patch.object(pm_connectors, "get_access_token", _no_oauth),
        patch.object(pm_connectors, "connector_registry", lambda: {"atlassian": "https://mcp"}),
    ):
        conns = await pm_connectors._connections()
    ua = conns["atlassian"]["headers"]["User-Agent"]
    assert "loupfeed" in ua and "python" not in ua.lower()
