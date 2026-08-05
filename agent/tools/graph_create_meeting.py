"""Tool: ``graph_create_meeting`` — schedule a Teams meeting on the requester's calendar.

The event is created on the ORGANIZER's calendar (the person who asked),
never on some bot mailbox, so it shows up for them like any meeting they
created themselves. Requires the app-only ``Calendars.ReadWrite`` Graph
permission with admin consent; until that is granted the tool fails with a
plain explanation.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

from ..utils.msgraph import GRAPH_BASE, get_graph_app_token
from ..utils.redact_internals import summarise_tool_failure

logger = logging.getLogger(__name__)

_TIMEOUT = 30
_ISO_LOCAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")


def _fail(reason: str, *, detail: str = "") -> dict[str, Any]:
    if detail:
        logger.warning("graph_create_meeting failed: %s", detail)
    return {"ok": False, "reason": reason}


def graph_create_meeting(
    organizer_email: str,
    subject: str,
    start: str,
    end: str,
    attendee_emails: list[str] | None = None,
    body_text: str = "",
    timezone: str = "Europe/Berlin",
) -> dict[str, Any]:
    """Create a Teams online meeting on the organizer's calendar.

    Only call this when someone EXPLICITLY asks for a meeting to be created,
    and always confirm the details (subject, time, attendees) in your reply.
    The organizer is the person who asked — take their email from the
    conversation context; never invent one.

    Args:
        organizer_email: Email of the person the meeting is created for.
        subject: Meeting title.
        start: Local start time, ``YYYY-MM-DDTHH:MM`` (no timezone suffix).
        end: Local end time, same format.
        attendee_emails: Emails to invite. The organizer is included
            automatically and does not need to be listed.
        body_text: Optional plain-text agenda for the invite body.
        timezone: IANA timezone the start/end are expressed in.

    Returns:
        ``{"ok": True, "web_link": str, "join_url": str, "event_id": str}``
        or ``{"ok": False, "reason": str}``.
    """
    if not organizer_email or "@" not in organizer_email:
        return _fail("I need the organizer's email address to create the meeting.")
    if not subject:
        return _fail("The meeting needs a subject.")
    for label, value in (("start", start), ("end", end)):
        if not _ISO_LOCAL.match(value or ""):
            return _fail(f"The {label} time must look like 2026-08-06T11:00 (local time).")

    token = get_graph_app_token()
    if not token:
        return _fail("I can't reach the calendar service right now.")

    payload: dict[str, Any] = {
        "subject": subject,
        "start": {"dateTime": start, "timeZone": timezone},
        "end": {"dateTime": end, "timeZone": timezone},
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness",
        "attendees": [
            {"emailAddress": {"address": a}, "type": "required"}
            for a in (attendee_emails or [])
            if a and "@" in a and a.lower() != organizer_email.lower()
        ],
    }
    if body_text:
        payload["body"] = {"contentType": "text", "content": body_text}

    try:
        r = requests.post(
            f"{GRAPH_BASE}/users/{organizer_email}/events",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        return _fail(
            "I couldn't reach the calendar service.", detail=f"{type(exc).__name__}: {exc}"
        )
    if r.status_code >= 400:
        return _fail(
            summarise_tool_failure(r.status_code, r.text, what=f"{organizer_email}'s calendar")
        )

    event = r.json()
    return {
        "ok": True,
        "event_id": str(event.get("id") or ""),
        "web_link": str(event.get("webLink") or ""),
        "join_url": str((event.get("onlineMeeting") or {}).get("joinUrl") or ""),
    }
