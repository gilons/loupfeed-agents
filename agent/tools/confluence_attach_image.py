"""Tool: ``confluence_attach_image`` — put a Teams image onto a Confluence page.

Screenshots people paste into Teams are often the substance of a capture page —
an error state, a UI someone is objecting to. They live as Teams *hosted
content*, which is binary and short-lived, so quoting a link is useless a day
later and the agent cannot read the picture itself.

This copies the bytes across: fetch the hosted content as the Teams app, upload
it to the page as a real attachment, and append an ``<ac:image>`` so it renders
inline. The image is preserved even though the agent never "sees" it.

Auth: Graph app token for the fetch (the app is installed in that conversation),
``ATLASSIAN_EMAIL`` + ``ATLASSIAN_API_TOKEN`` Basic auth for the upload.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any

import requests

from ..utils.atlassian_api import use_app_for
from ..utils.msgraph import GRAPH_BASE, get_graph_app_token

logger = logging.getLogger(__name__)

_TIMEOUT = 60
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _fail(reason: str, *, detail: str = "") -> dict[str, Any]:
    if detail:
        logger.warning("confluence_attach_image failed: %s", detail)
    return {"ok": False, "reason": reason}


def _atlassian_auth() -> tuple[str, str] | None:
    email = os.environ.get("ATLASSIAN_EMAIL", "")
    token = os.environ.get("ATLASSIAN_API_TOKEN", "")
    return (email, token) if email and token else None


def _site_base() -> str:
    site = os.environ.get("ATLASSIAN_SITE_URL", "https://dinolabgmbh.atlassian.net")
    return site.rstrip("/")


def _safe_filename(name: str, mime: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name or "").strip("-") or "teams-image"
    if "." not in stem:
        stem = f"{stem}.{_EXT_BY_MIME.get(mime, 'png')}"
    return stem[:120]


# Forge caps web-trigger payloads, and base64 inflates by a third, so only
# comfortably small images take the app path; larger ones stay on the
# deployment's own credential rather than failing.
_APP_ATTACH_LIMIT_BYTES = 600 * 1024


def app_attach_url() -> str:
    return os.environ.get("ATLASSIAN_APP_ATTACH_URL", "").strip()


def _attach_via_app(page_id: str, name: str, mime: str, data: bytes) -> bool:
    """Upload through the entry app so the attachment is owned by the app."""
    url = app_attach_url()
    secret = os.environ.get("ATLASSIAN_APP_SHARED_SECRET", "")
    if not url or not secret or not use_app_for(attributed=True):
        return False
    if len(data) > _APP_ATTACH_LIMIT_BYTES:
        logger.info(
            "attachment %s is %d KB, above the app path limit; using the deployment credential",
            name,
            len(data) // 1024,
        )
        return False
    try:
        r = requests.post(
            url,
            headers={"Content-Type": "application/json", "X-Loupfeed-Secret": secret},
            json={
                "pageId": str(page_id),
                "filename": name,
                "contentType": mime,
                "dataBase64": base64.b64encode(data).decode(),
            },
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("app attach failed: %s: %s", type(exc).__name__, exc)
        return False
    if r.status_code >= 400:
        logger.warning("app attach failed: %s %s", r.status_code, r.text[:200])
        return False
    logger.info("attachment %s uploaded by the app", name)
    return True


def confluence_attach_image(page_id: str, image_url: str, filename: str = "") -> dict[str, Any]:
    """Copy an image from a Teams message onto a Confluence page.

    Use this when a message carries a screenshot that belongs in the capture
    page — an error, a mockup, a UI someone is reacting to. Quoting the Teams
    link is not enough: that link dies, the attachment does not.

    Get ``image_url`` from the message body or attachments via ``graph_api``: it
    looks like ``…/messages/{id}/hostedContents/{id}/$value``. Both the plain
    hosted-content URL and the ``$value`` form work.

    You cannot see the image yourself — do not describe what it depicts unless
    someone told you. Attach it and say it is attached.

    Args:
        page_id: The Confluence page to attach to (numeric id).
        image_url: The Teams hosted-content URL for the image.
        filename: Optional name for the attachment; one is derived if omitted.

    Returns:
        ``{"ok": True, "filename": str, "bytes": int, "page_id": str}`` or
        ``{"ok": False, "reason": str}``.
    """
    if not page_id or not image_url:
        return _fail("I need both the page and the image to attach it.")
    auth = _atlassian_auth()
    if not auth:
        return _fail(
            "I can't attach files to Confluence — my access there isn't set up for it.",
            detail="ATLASSIAN_EMAIL/ATLASSIAN_API_TOKEN not set",
        )
    token = get_graph_app_token()
    if not token:
        return _fail("I can't reach Teams to fetch that image right now.")

    url = image_url if image_url.endswith("$value") else f"{image_url.rstrip('/')}/$value"
    if url.startswith("/"):
        url = f"{GRAPH_BASE}{url}"
    try:
        got = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        return _fail(
            "I couldn't download that image from Teams.", detail=f"{type(exc).__name__}: {exc}"
        )
    if got.status_code != 200:
        return _fail(
            "I couldn't download that image from Teams.",
            detail=f"GET hosted content -> {got.status_code}: {got.text[:200]}",
        )

    data = got.content
    if len(data) > _MAX_IMAGE_BYTES:
        return _fail(f"That image is too large for me to copy across ({len(data) // 1024} KB).")
    mime = (got.headers.get("Content-Type") or "image/png").split(";")[0].strip()
    if not mime.startswith("image/"):
        return _fail(
            "That link isn't an image, so I didn't attach it.",
            detail=f"content-type was {mime}",
        )
    name = _safe_filename(filename, mime)

    if _attach_via_app(page_id, name, mime, data):
        return _attached(name, data, page_id)

    if not auth:
        return _fail(
            "I can't attach files to Confluence — my access there isn't set up for it.",
            detail="app attach unavailable and no ATLASSIAN_EMAIL/ATLASSIAN_API_TOKEN",
        )
    base = _site_base()
    try:
        up = requests.post(
            f"{base}/wiki/rest/api/content/{page_id}/child/attachment",
            headers={"X-Atlassian-Token": "no-check"},
            auth=auth,
            files={"file": (name, data, mime)},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        return _fail(
            "I couldn't upload the image to the page.", detail=f"{type(exc).__name__}: {exc}"
        )
    if up.status_code >= 400:
        return _fail(
            "I couldn't upload the image to the page.",
            detail=f"attachment upload -> {up.status_code}: {up.text[:300]}",
        )

    return _attached(name, data, page_id)


def _attached(name: str, data: bytes, page_id: str) -> dict[str, Any]:
    return {
        "ok": True,
        "filename": name,
        "bytes": len(data),
        "page_id": str(page_id),
        "embed_markup": f'<ac:image><ri:attachment ri:filename="{name}" /></ac:image>',
        "next": "Append embed_markup to the page body so the image renders inline.",
    }
