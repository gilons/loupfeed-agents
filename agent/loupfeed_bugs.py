"""Turning loupfeed feedback into Jira bugs.

A loupfeed instance forwards each new feedback report here; this files it on the
bug board of whichever surface that instance belongs to. Deliberately plain
Python and not an agent: deciding which project a report belongs on is a lookup
in the surface registry, and asking a model to do a lookup is slower, dearer and
occasionally wrong.

Two rules shape the result.

**One issue per thread, not per report.** loupfeed already groups feedback on
the same UI element into a thread, so three people complaining about the same
broken chip is one bug with three comments rather than three bugs. The join is a
label carrying the thread id, which means the dedupe survives this service
losing its memory: the state lives on the board.

**Never invent the project.** A delivery from an instance no surface claims is
answered 200 and dropped, not filed somewhere plausible. A bug on the wrong
board is worse than a bug nobody filed, because somebody has to find it first.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import quote

from .surfaces import surface_for_instance
from .utils.atlassian_api import atlassian_request
from .utils.markdown_reply import markdown_to_adf

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "x-loupfeed-signature"
TIMESTAMP_HEADER = "x-loupfeed-timestamp"

#: How far out of date a delivery may be. Long enough to survive a retry storm
#: or a clock a few minutes off, short enough that a captured body is not
#: replayable next week.
_MAX_SKEW_SECONDS = 15 * 60

_LABEL_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def verify_signature(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    """True when this body really came from the instance holding the secret.

    The timestamp is inside the MAC and is also checked for age: signing the
    body alone would leave a captured delivery replayable for ever.
    """
    if not secret or not signature or not timestamp:
        return False
    try:
        age = abs(time.time() - int(timestamp) / 1000)
    except ValueError:
        return False
    if age > _MAX_SKEW_SECONDS:
        return False
    expected = (
        "sha256="
        + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


def _label_for(thread_id: str) -> str:
    return f"loupfeed-{_LABEL_SAFE.sub('-', thread_id)[:200]}"


def _find_existing(project_key: str, label: str) -> str | None:
    """The issue already filed for this thread, if there is one.

    Searched rather than remembered. A service that keeps its own index of what
    it has filed is a service that files everything twice the first time it is
    restored from a backup.
    """
    jql = quote(f'project = "{project_key}" AND labels = "{label}" ORDER BY created ASC')
    r = atlassian_request(
        "jira", "GET", f"/rest/api/3/search/jql?jql={jql}&maxResults=1&fields=key"
    )
    if not r.ok:
        # Unknown is not "no": filing a duplicate is worse than filing nothing,
        # because the duplicate is the one somebody has to notice and close.
        raise RuntimeError(f"jira search failed with {r.status}")
    issues = (json.loads(r.text or "{}") or {}).get("issues") or []
    return issues[0].get("key") if issues else None


def _summary(event: dict[str, Any]) -> str:
    text = " ".join(str(event.get("text") or "").split())
    where = event.get("route") or ""
    head = text[:120] + ("…" if len(text) > 120 else "")
    return f"{head} ({where})" if where else head or "Feedback with no text"


def _reporter(event: dict[str, Any]) -> str:
    user = event.get("user")
    if isinstance(user, dict):
        for key in ("email", "name", "username", "id"):
            value = user.get(key)
            if value:
                return str(value)
    return "not identified"


def _body_markdown(event: dict[str, Any], instance_name: str, dashboard: str | None) -> str:
    element = event.get("element") or {}
    lines = [
        "> " + (str(event.get("text") or "").strip() or "_no text_"),
        "",
        "|  |  |",
        "| --- | --- |",
        f"| Reported by | {_reporter(event)} |",
        f"| Where | {event.get('url') or event.get('route') or 'not recorded'} |",
        f"| Release | `{event.get('release') or 'not recorded'}` |",
        f"| Environment | {event.get('environment') or instance_name} |",
        f"| On the page | {element.get('text') or element.get('selector') or 'nothing anchored'} |",
        f"| Received | {event.get('receivedAt') or 'unknown'} |",
    ]
    if dashboard:
        lines += ["", f"[Open the thread in loupfeed]({dashboard})"]
    lines += [
        "",
        "_Filed automatically from loupfeed. Further reports on the same element"
        " arrive as comments here rather than as new issues._",
    ]
    return "\n".join(lines)


def _dashboard_url(target: dict[str, str], thread_id: str) -> str | None:
    api = target.get("api")
    return f"{api}/threads/{quote(thread_id)}" if api else None


def file_feedback(delivery: dict[str, Any]) -> dict[str, Any]:
    """File one delivery. Returns what happened, for the caller to log.

    Raises on a Jira failure so the sender retries: the delivery Lambda treats a
    5xx as retryable, which is exactly the behaviour a transient Jira outage
    should get.
    """
    instance = delivery.get("instance") or {}
    event = delivery.get("event") or {}
    org, project = str(instance.get("org") or ""), str(instance.get("project") or "")

    found = surface_for_instance(org, project)
    if not found:
        logger.info("loupfeed delivery from an unregistered instance %s/%s", org, project)
        return {"status": "ignored", "reason": "no surface for that instance"}

    surface, target = found
    projects = surface.get("jira_projects") or []
    if not projects:
        return {"status": "ignored", "reason": "surface has no jira project"}
    project_key = str(projects[0])

    thread_id = str(event.get("threadId") or event.get("eventId") or "")
    if not thread_id:
        return {"status": "ignored", "reason": "delivery carried no id"}

    label = _label_for(thread_id)
    existing = _find_existing(project_key, label)
    dashboard = _dashboard_url(target, thread_id)

    if existing:
        body = {
            "body": markdown_to_adf(
                "Another report on this element:\n\n"
                + _body_markdown(event, target.get("name", ""), dashboard)
            )
        }
        r = atlassian_request(
            "jira", "POST", f"/rest/api/3/issue/{existing}/comment", body, attributed=True
        )
        if not r.ok:
            raise RuntimeError(f"jira comment failed with {r.status}")
        return {"status": "commented", "issue": existing}

    body = {
        "fields": {
            "project": {"key": project_key},
            "issuetype": {"name": "Bug"},
            "summary": _summary(event),
            "description": markdown_to_adf(
                _body_markdown(event, target.get("name", ""), dashboard)
            ),
            "labels": [label, "loupfeed"],
        }
    }
    r = atlassian_request("jira", "POST", "/rest/api/3/issue", body, attributed=True)
    if not r.ok:
        raise RuntimeError(f"jira create failed with {r.status}: {r.text[:300]}")
    key = (json.loads(r.text or "{}") or {}).get("key")
    logger.info("filed loupfeed feedback as %s", key)
    return {"status": "created", "issue": key}


def webhook_secret() -> str:
    return os.environ.get("LOUPFEED_WEBHOOK_SECRET", "")
