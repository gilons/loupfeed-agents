"""Inbound entry point for the Atlassian entry app (Forge).

The Forge app owns the Atlassian-side subscription and forwards the events
it cares about here. Authentication is a shared secret established when the
customer configures the app for their deployment: Forge sends it in
``X-Loupfeed-Secret``, this endpoint compares it in constant time and drops
anything that does not match. No secret, no processing.

This is deliberately the same shape as ``teams_adapter``: verify, normalise
the payload into a requester + surface + text triple, then hand off to a
graph. The mention gate lives here too, because Forge forwards every
subscribed event and only some of them are addressed to us.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any

from fastapi import APIRouter, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter()

_SECRET_HEADER = "x-loupfeed-secret"


def shared_secret() -> str:
    return os.environ.get("ATLASSIAN_APP_SHARED_SECRET", "")


def _authorised(request: Request) -> bool:
    expected = shared_secret()
    if not expected:
        return False
    return hmac.compare_digest(request.headers.get(_SECRET_HEADER, ""), expected)


def _adf_text(node: Any) -> str:
    """Flatten an ADF document to plain text."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text") or "")
        return "".join(_adf_text(c) for c in node.get("content") or [])
    if isinstance(node, list):
        return "".join(_adf_text(c) for c in node)
    return ""


def _mention_ids(node: Any) -> list[str]:
    """Account ids mentioned anywhere in an ADF document."""
    found: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "mention":
            mention_id = str((node.get("attrs") or {}).get("id") or "")
            if mention_id:
                found.append(mention_id)
        for child in node.get("content") or []:
            found.extend(_mention_ids(child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_mention_ids(child))
    return found


def normalise(event: dict[str, Any]) -> dict[str, Any]:
    """The subset of an Atlassian event the agents actually need."""
    event_type = str(event.get("eventType") or "")
    issue = event.get("issue") or {}
    fields = issue.get("fields") or {}
    comment = event.get("comment") or {}
    content = event.get("content") or {}
    body = comment.get("body")
    return {
        "event_type": event_type,
        "product": "jira" if event_type.startswith("avi:jira") else "confluence",
        "issue_key": str(issue.get("key") or "") or None,
        "page_id": str(content.get("id") or "") or None,
        "title": str(fields.get("summary") or content.get("title") or "") or None,
        "requester_account_id": str(event.get("atlassianId") or "") or None,
        "assignee_account_id": str((fields.get("assignee") or {}).get("accountId") or "") or None,
        "text": _adf_text(body).strip() or None,
        "mentions": _mention_ids(body),
        "changed_fields": [i.get("field") for i in (event.get("changelog") or {}).get("items", [])],
    }


def is_addressed_to_us(normalised: dict[str, Any], app_account_id: str) -> bool:
    """Only act on events that name us: a mention, or an assignment to us.

    Forge forwards every subscribed event, so this gate is what keeps the
    agents out of ordinary project traffic (same lesson as the Teams RSC
    firehose).
    """
    if not app_account_id:
        return False
    if app_account_id in normalised.get("mentions", []):
        return True
    if normalised.get("assignee_account_id") == app_account_id and "assignee" in (
        normalised.get("changed_fields") or []
    ):
        return True
    return False


@router.post("/webhooks/atlassian")
async def atlassian_webhook(request: Request) -> Response:
    if not _authorised(request):
        logger.warning("atlassian webhook: rejected unauthenticated request")
        return Response(status_code=401)

    payload = await request.json()
    event = payload.get("event") or payload
    normalised = normalise(event)
    app_account_id = str(payload.get("appAccountId") or "")
    addressed = is_addressed_to_us(normalised, app_account_id)

    logger.info(
        "atlassian webhook: %s %s addressed=%s requester=%s text=%r",
        normalised["event_type"],
        normalised.get("issue_key") or normalised.get("page_id"),
        addressed,
        normalised.get("requester_account_id"),
        (normalised.get("text") or "")[:120],
    )
    # Agent dispatch lands here next (pm for pages, coding for bugs).
    return Response(status_code=202)
