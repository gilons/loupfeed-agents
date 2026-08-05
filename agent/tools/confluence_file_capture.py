"""Tool: ``confluence_file_capture`` — file a capture page in its date tree.

Capture pages belong under ``<purpose folder> / <slug> YYYY / <slug> YYYY-MM /
<slug> YYYY-MM-DD``. The agent could not maintain that tree itself (it had no
folder tool), so pages piled up at the space root. This tool finds or creates
the chain and moves the page into the day folder in one call.

Folder titles are unique per SPACE in Confluence, not per parent — a bare
``2026`` can exist only once in the whole space. That is why every level is
prefixed with the purpose slug (``standups 2026-08-05``, ``issues 2026-07``):
two purposes can then hold the same date without colliding.

Auth: ``ATLASSIAN_EMAIL`` + ``ATLASSIAN_API_TOKEN`` Basic auth, same as
``confluence_attach_image``.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from typing import Any

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 30


def _fail(reason: str, *, detail: str = "") -> dict[str, Any]:
    if detail:
        logger.warning("confluence_file_capture failed: %s", detail)
    return {"ok": False, "reason": reason}


def _auth() -> tuple[str, str] | None:
    email = os.environ.get("ATLASSIAN_EMAIL", "")
    token = os.environ.get("ATLASSIAN_API_TOKEN", "")
    return (email, token) if email and token else None


def _site_base() -> str:
    return os.environ.get("ATLASSIAN_SITE_URL", "https://dinolabgmbh.atlassian.net").rstrip("/")


def _find_folder(auth: tuple[str, str], space_key: str, title: str) -> str | None:
    cql = urllib.parse.quote(f'space={space_key} and type=folder and title="{title}"')
    r = requests.get(
        f"{_site_base()}/wiki/rest/api/content/search?cql={cql}&limit=2",
        auth=auth,
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        return None
    results = r.json().get("results", [])
    return str(results[0]["id"]) if results else None


def _space_id(auth: tuple[str, str], space_key: str) -> str | None:
    r = requests.get(
        f"{_site_base()}/wiki/api/v2/spaces?keys={space_key}", auth=auth, timeout=_TIMEOUT
    )
    if r.status_code != 200:
        return None
    results = r.json().get("results", [])
    return str(results[0]["id"]) if results else None


def _ensure_folder(
    auth: tuple[str, str], space_key: str, space_id: str, title: str, parent_id: str | None
) -> str | None:
    r = requests.post(
        f"{_site_base()}/wiki/api/v2/folders",
        auth=auth,
        json={"spaceId": space_id, "parentId": parent_id, "title": title},
        timeout=_TIMEOUT,
    )
    if r.status_code < 400:
        return str(r.json()["id"])
    # 400 "A folder exists with the same title in this space" — find it instead.
    return _find_folder(auth, space_key, title)


def confluence_file_capture(
    page_id: str, purpose_folder: str, date: str, space_key: str = "SPRAW"
) -> dict[str, Any]:
    """Move a capture page into its purpose folder's date tree.

    Call this right after creating a capture page. It finds or creates the
    ``<slug> YYYY / <slug> YYYY-MM / <slug> YYYY-MM-DD`` chain under the
    purpose folder and moves the page there. Never build date folders by
    hand and never leave a capture at the space root when this tool works.

    Args:
        page_id: The Confluence page to file (numeric id).
        purpose_folder: The top-level purpose folder title, e.g. ``Standups``,
            ``Chats``, ``UI Review``, ``Spec Reviews``. Created at the space
            root if it does not exist yet.
        date: The capture's date as ``YYYY-MM-DD`` — the date of the call or
            conversation itself, not the day you are filing it.
        space_key: The Confluence space, default ``SPRAW``.

    Returns:
        ``{"ok": True, "filed_under": "<purpose>/<slug YYYY-MM-DD>"}`` or
        ``{"ok": False, "reason": str}``.
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        return _fail("The date must look like 2026-08-05.")
    if not page_id or not purpose_folder:
        return _fail("I need the page and the purpose folder to file it.")
    auth = _auth()
    if not auth:
        return _fail(
            "I can't file pages in Confluence — my access there isn't set up for it.",
            detail="ATLASSIAN_EMAIL/ATLASSIAN_API_TOKEN not set",
        )

    space_id = _space_id(auth, space_key)
    if not space_id:
        return _fail(f"I couldn't find the {space_key} space.")

    slug = re.sub(r"[^a-z0-9]+", "-", purpose_folder.lower()).strip("-")
    year, month = date[:4], date[:7]

    purpose_id = _find_folder(auth, space_key, purpose_folder) or _ensure_folder(
        auth, space_key, space_id, purpose_folder, None
    )
    if not purpose_id:
        return _fail(f'I couldn\'t find or create the "{purpose_folder}" folder.')

    parent = purpose_id
    for title in (f"{slug} {year}", f"{slug} {month}", f"{slug} {date}"):
        parent = _ensure_folder(auth, space_key, space_id, title, parent)
        if not parent:
            return _fail(
                f'I couldn\'t create the "{title}" folder.',
                detail=f"ensure_folder returned none under parent chain for {date}",
            )

    mv = requests.put(
        f"{_site_base()}/wiki/rest/api/content/{page_id}/move/append/{parent}",
        auth=auth,
        timeout=_TIMEOUT,
    )
    if mv.status_code >= 400:
        return _fail(
            "I couldn't move the page into its date folder.",
            detail=f"move -> {mv.status_code}: {mv.text[:200]}",
        )
    return {"ok": True, "filed_under": f"{purpose_folder}/{slug} {date}"}
