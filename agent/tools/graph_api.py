"""Tools: ``graph_api`` + ``graph_meeting_transcript``. Read-only Microsoft Graph.

The pm agent is installed into Teams teams/chats/meetings, where resource-
specific consent (RSC) grants its Entra app read access to that resource's
messages, members, and meeting artifacts — plus whatever tenant application
permissions were consented in Entra (directory, SharePoint sites). GET-only by
design, mirroring ``github_api``: writes stay with the Bot Framework reply
path and the planning-system connectors.
"""

from __future__ import annotations

import json
from typing import Any

import requests

from ..utils.msgraph import GRAPH_BASE, get_graph_app_token

_MAX_RESPONSE_CHARS = 60_000
_MAX_TRANSCRIPT_CHARS = 120_000
_TIMEOUT = 30

# Read-oriented Graph roots the pm agent has any business touching.
_ALLOWED_ROOTS = ("chats", "teams", "groups", "users", "sites", "drives")


def _headers() -> dict[str, str] | None:
    token = get_graph_app_token()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


def _allowed(path: str) -> bool:
    root = path.lstrip("/").split("/", 1)[0].split("?", 1)[0].lower()
    return root in _ALLOWED_ROOTS


def _get(path: str, params: dict[str, Any] | None = None) -> requests.Response:
    headers = _headers()
    if headers is None:
        raise RuntimeError("Microsoft Graph credentials not configured")
    return requests.get(
        f"{GRAPH_BASE}{path}", headers=headers, params=params or {}, timeout=_TIMEOUT
    )


def graph_api(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a read-only (GET) Microsoft Graph v1.0 endpoint as the loupfeed Teams app.

    Access is scoped by where the app is installed (Teams RSC) plus tenant
    read permissions — use it to READ Microsoft 365 context instead of asking
    people to paste it. Useful endpoints:

    - ``/chats/{chat-id}/messages`` — recent messages in a group/meeting chat
    - ``/chats/{chat-id}/members`` — who is in the chat
    - ``/chats/{chat-id}`` — chat properties (a meeting chat carries ``onlineMeetingInfo``)
    - ``/teams/{team-group-id}/channels`` — a team's channels
    - ``/teams/{team-group-id}/channels/{channel-id}/messages`` — channel messages
    - ``/teams/{team-group-id}/members`` — team roster
    - ``/groups/{team-group-id}/drive/root/children`` — files in the team's library
    - ``/users`` with ``$filter=startswith(displayName,'...')`` — resolve people
    - ``/users/{id-or-upn}`` / ``/users/{id}/manager`` — profile and org chart
    - ``/sites/{hostname}:/{site-path}`` — SharePoint sites

    Only GET is possible. Chat/channel ids come from the Teams context of the
    current conversation (see the system prompt) or from other Graph calls.

    Args:
        path: The API path, starting with ``/`` (e.g. ``/chats/{id}/messages``).
        params: Optional query parameters (``$top``, ``$filter``, ``$select``, ...).

    Returns:
        ``{"status": int, "body": <parsed JSON or text>}``. 403 usually means
        the app is not installed in that team/chat (no RSC grant there) or the
        tenant permission is missing — say so instead of retrying.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        return {"status": 0, "body": "path must start with '/' (e.g. /chats/{id}/messages)"}
    if not _allowed(path):
        return {
            "status": 0,
            "body": f"path root not allowed; must be one of: {', '.join(_ALLOWED_ROOTS)}",
        }
    try:
        resp = _get(path, params)
    except RuntimeError as exc:
        return {"status": 0, "body": str(exc)}
    except requests.RequestException as exc:
        return {"status": 0, "body": f"request failed: {exc}"}

    if resp.status_code == 204 or not resp.content:
        return {"status": resp.status_code, "body": ""}
    try:
        body: Any = resp.json()
        text = json.dumps(body)
    except ValueError:
        text = resp.text
        body = text
    if len(text) > _MAX_RESPONSE_CHARS:
        return {"status": resp.status_code, "body": text[:_MAX_RESPONSE_CHARS], "truncated": True}
    return {"status": resp.status_code, "body": body}


def graph_meeting_transcript(chat_id: str) -> dict[str, Any]:
    """Fetch the transcript of the Teams meeting behind a meeting chat.

    Use this right after a call, when mentioned in a meeting chat: pass the
    conversation's chat id (from the Teams context in the system prompt). It
    resolves the chat → its online meeting → the newest transcript and returns
    the transcript text (WebVTT: timestamped, speaker-attributed lines).

    Only works for scheduled meetings with transcription turned on; returns a
    clear reason otherwise (no meeting behind the chat, transcription off, or
    access not granted).

    Args:
        chat_id: The meeting chat id (looks like ``19:meeting_...@thread.v2``).

    Returns:
        ``{"ok": bool, "subject": str, "organizer_id": str, "transcript": str}``
        on success; ``{"ok": False, "reason": ...}`` on failure.
    """
    if not isinstance(chat_id, str) or not chat_id:
        return {"ok": False, "reason": "chat_id is required"}
    try:
        chat_resp = _get(f"/chats/{chat_id}")
        if chat_resp.status_code != 200:
            return {
                "ok": False,
                "reason": f"could not read chat ({chat_resp.status_code}): {chat_resp.text[:300]}",
            }
        chat = chat_resp.json()
        meeting_info = chat.get("onlineMeetingInfo") or {}
        join_url = meeting_info.get("joinWebUrl")
        organizer_id = ((meeting_info.get("organizer") or {}).get("id")) or ""
        if not join_url or not organizer_id:
            return {"ok": False, "reason": "this chat has no online meeting associated with it"}

        escaped = join_url.replace("'", "''")
        meetings_resp = _get(
            f"/users/{organizer_id}/onlineMeetings",
            params={"$filter": f"JoinWebUrl eq '{escaped}'"},
        )
        if meetings_resp.status_code != 200:
            return {
                "ok": False,
                "reason": (
                    f"could not resolve the meeting ({meetings_resp.status_code}): "
                    f"{meetings_resp.text[:300]}"
                ),
            }
        meetings = meetings_resp.json().get("value") or []
        if not meetings:
            return {"ok": False, "reason": "no online meeting found for this chat's join link"}
        meeting = meetings[0]

        transcripts_resp = _get(f"/users/{organizer_id}/onlineMeetings/{meeting['id']}/transcripts")
        if transcripts_resp.status_code != 200:
            return {
                "ok": False,
                "reason": (
                    f"could not list transcripts ({transcripts_resp.status_code}): "
                    f"{transcripts_resp.text[:300]}"
                ),
            }
        transcripts = transcripts_resp.json().get("value") or []
        if not transcripts:
            return {
                "ok": False,
                "reason": "no transcript for this meeting (was transcription turned on?)",
            }
        transcripts.sort(key=lambda t: str(t.get("createdDateTime") or ""), reverse=True)
        newest = transcripts[0]

        content_resp = _get(
            f"/users/{organizer_id}/onlineMeetings/{meeting['id']}"
            f"/transcripts/{newest['id']}/content",
            params={"$format": "text/vtt"},
        )
        if content_resp.status_code != 200:
            return {
                "ok": False,
                "reason": (
                    f"could not fetch transcript content ({content_resp.status_code}): "
                    f"{content_resp.text[:300]}"
                ),
            }
        text = content_resp.text
        truncated = len(text) > _MAX_TRANSCRIPT_CHARS
        return {
            "ok": True,
            "subject": str(meeting.get("subject") or chat.get("topic") or ""),
            "organizer_id": organizer_id,
            "created": str(newest.get("createdDateTime") or ""),
            "transcript": text[:_MAX_TRANSCRIPT_CHARS],
            **({"truncated": True} if truncated else {}),
        }
    except RuntimeError as exc:
        return {"ok": False, "reason": str(exc)}
    except requests.RequestException as exc:
        return {"ok": False, "reason": f"request failed: {exc}"}
