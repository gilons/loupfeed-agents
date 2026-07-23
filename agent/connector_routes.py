"""HTTP routes for MCP connector OAuth (mounted on the platform's FastAPI app).

- ``GET /connectors/status`` — secret-free connection state per connector.
- ``GET /connectors/{name}/start`` — begins the OAuth 2.1 PKCE flow and
  redirects the user to the provider's consent page. Chat adapters (Teams sign-in
  cards, etc.) point users here.
- ``GET /connectors/{name}/callback`` — the registered redirect URI; stores the
  tokens and shows a close-this-tab page.

``CONNECTOR_PUBLIC_BASE_URL`` must be the externally reachable base URL of this
service (e.g. the CloudFront domain) — it forms the redirect URI that gets
registered with the provider.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .connector_auth import connection_status, finish_auth, start_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connectors", tags=["connectors"])


def _public_base(request: Request) -> str:
    configured = os.environ.get("CONNECTOR_PUBLIC_BASE_URL", "").rstrip("/")
    return configured or str(request.base_url).rstrip("/")


@router.get("/status")
async def connectors_status() -> JSONResponse:
    return JSONResponse(connection_status())


@router.get("/{connector}/start")
async def connector_start(connector: str, request: Request) -> RedirectResponse:
    redirect_uri = f"{_public_base(request)}/connectors/{connector}/callback"
    try:
        authorize_url = await start_auth(connector, redirect_uri)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # discovery/registration failures → readable error
        logger.exception("connector %s: start failed", connector)
        raise HTTPException(status_code=502, detail=f"OAuth start failed: {exc}") from exc
    return RedirectResponse(authorize_url, status_code=302)


@router.get("/{connector}/callback")
async def connector_callback(
    connector: str,
    state: str = "",
    code: str = "",
    error: str = "",
    error_description: str = "",
) -> HTMLResponse:
    if error:
        logger.warning("connector %s: OAuth error: %s %s", connector, error, error_description)
        return HTMLResponse(
            f"<h3>Connection failed</h3><p>{error}: {error_description}</p>", status_code=400
        )
    if not state or not code:
        raise HTTPException(status_code=400, detail="missing state or code")
    try:
        name = await finish_auth(state, code)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return HTMLResponse(
        f"<h3>✅ {name} connected</h3><p>loupfeed agents can now act on your "
        f"{name} workspace. You can close this tab.</p>"
    )
