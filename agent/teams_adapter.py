"""Microsoft Teams entry adapter → the pm graph.

Implements the platform's thread ⇄ session rule (docs: loupfeed agents
platform, §3.1): every Teams conversation thread maps to exactly one LangGraph
thread.

- Channel messages: the Teams ``conversation.id`` carries
  ``;messageid=<root>`` for thread replies; for a top-level mention we key on
  the tagged message itself and answer as a reply, which creates the thread.
- Personal (1:1) chats: the chat id is the session key (threading fallback).
- Multi-user threads: each message is speaker-labeled ("Name: ...") before it
  reaches the agent.

The endpoint returns 200 immediately (Teams requires a fast ack) and processes
the run in the background, posting the agent's reply back into the thread via
the Bot Connector REST API. When the Atlassian connector isn't connected yet,
the bot replies with the OAuth sign-in link (``/connectors/atlassian/start``)
instead of running the agent.

Env: ``TEAMS_APP_ID``, ``TEAMS_APP_PASSWORD``, ``TEAMS_APP_TENANT_ID``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid

import httpx
import jwt as pyjwt
from fastapi import APIRouter, BackgroundTasks, Request, Response
from jwt import PyJWKClient
from langgraph_sdk import get_client

from .utils.redact_internals import redact_internals

logger = logging.getLogger(__name__)

router = APIRouter(tags=["teams"])

LANGGRAPH_URL = os.environ.get("LANGGRAPH_URL") or os.environ.get(
    "LANGGRAPH_URL_PROD", "http://localhost:2024"
)
PM_GRAPH = "pm"
_THREAD_NAMESPACE = uuid.UUID("6c0075fe-ed00-4c9e-9f36-1a2b3c4d5e6f")

_BOTFRAMEWORK_OPENID = "https://login.botframework.com/v1/.well-known/openidconfiguration"
_BOTFRAMEWORK_ISSUER = "https://api.botframework.com"

_jwks_client: PyJWKClient | None = None
_jwks_client_at = 0.0
_connector_token: dict | None = None  # {value, expires_at}
_member_cache: dict[str, tuple[float, dict]] = {}  # f"{conv}:{user}" -> (at, member)
_MEMBER_CACHE_TTL = 3600.0


def _app_id() -> str:
    return os.environ.get("TEAMS_APP_ID", "")


def _configured() -> bool:
    return bool(_app_id() and os.environ.get("TEAMS_APP_PASSWORD"))


# ---------------------------------------------------------------------------
# Inbound auth: verify the Bot Framework JWT on incoming activities.
# ---------------------------------------------------------------------------


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client, _jwks_client_at
    if _jwks_client is None or time.time() - _jwks_client_at > 24 * 3600:
        with httpx.Client(timeout=10) as http:
            jwks_uri = http.get(_BOTFRAMEWORK_OPENID).json()["jwks_uri"]
        _jwks_client = PyJWKClient(jwks_uri, cache_keys=True)
        _jwks_client_at = time.time()
    return _jwks_client


def _verify_activity_auth(auth_header: str) -> bool:
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header.removeprefix("Bearer ")
    try:
        key = _get_jwks_client().get_signing_key_from_jwt(token).key
        claims = pyjwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=_app_id(),
            options={"require": ["exp", "aud", "iss"]},
        )
        return claims.get("iss") == _BOTFRAMEWORK_ISSUER
    except Exception:
        logger.warning("teams: rejected activity with invalid auth", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Outbound: Bot Connector client-credentials token + reply posting.
# ---------------------------------------------------------------------------


async def _get_connector_token() -> str:
    global _connector_token
    if _connector_token and time.time() < _connector_token["expires_at"] - 60:
        return _connector_token["value"]
    tenant = os.environ.get("TEAMS_APP_TENANT_ID") or "botframework.com"
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": _app_id(),
                "client_secret": os.environ.get("TEAMS_APP_PASSWORD", ""),
                "scope": "https://api.botframework.com/.default",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    _connector_token = {
        "value": data["access_token"],
        "expires_at": time.time() + float(data.get("expires_in") or 3600),
    }
    return _connector_token["value"]


async def _post_activity(service_url: str, conversation_id: str, payload: dict) -> None:
    token = await _get_connector_token()
    url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code >= 300:
            logger.error("teams: post activity failed %s: %s", resp.status_code, resp.text[:300])


async def _reply(activity: dict, text: str) -> None:
    conversation_id = activity["conversation"]["id"]
    payload = {
        "type": "message",
        "text": redact_internals(text),
        "textFormat": "markdown",
        "replyToId": activity.get("id"),
        "from": activity.get("recipient"),
        "recipient": activity.get("from"),
        "conversation": activity.get("conversation"),
    }
    await _post_activity(activity["serviceUrl"], conversation_id, payload)


async def _send_typing(activity: dict) -> None:
    payload = {
        "type": "typing",
        "replyToId": activity.get("id"),
        "from": activity.get("recipient"),
        "recipient": activity.get("from"),
        "conversation": activity.get("conversation"),
    }
    await _post_activity(activity["serviceUrl"], activity["conversation"]["id"], payload)


async def _get_sender_member(activity: dict) -> dict:
    """Sender's Teams member record (name, email/UPN, aadObjectId), cached.

    Identity P1: this is the zero-friction tier of the platform identity map —
    the work email joins Teams users to the user-mappings store (GitHub today,
    Atlassian accountId in P2).
    """
    conv_id = str((activity.get("conversation") or {}).get("id") or "")
    user_id = str((activity.get("from") or {}).get("id") or "")
    if not conv_id or not user_id:
        return {}
    key = f"{conv_id}:{user_id}"
    cached = _member_cache.get(key)
    if cached and time.time() - cached[0] < _MEMBER_CACHE_TTL:
        return cached[1]
    try:
        token = await _get_connector_token()
        url = f"{activity['serviceUrl'].rstrip('/')}/v3/conversations/{conv_id}/members/{user_id}"
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            member = resp.json()
    except Exception:
        logger.warning("teams: member lookup failed for %s", user_id, exc_info=True)
        member = {}
    _member_cache[key] = (time.time(), member)
    return member


# ---------------------------------------------------------------------------
# Thread ⇄ session mapping + message shaping.
# ---------------------------------------------------------------------------


def _thread_key(activity: dict) -> str:
    """One Teams thread → one stable key (platform rule: thread ⇄ session)."""
    conversation = activity.get("conversation") or {}
    conv_id = str(conversation.get("id") or "")
    conv_type = str(conversation.get("conversationType") or "")
    if conv_type == "channel" and ";messageid=" not in conv_id:
        # Top-level mention: key on the tagged message; our reply creates the thread.
        return f"{conv_id};messageid={activity.get('id')}"
    return conv_id


def langgraph_thread_id(activity: dict) -> str:
    return str(uuid.uuid5(_THREAD_NAMESPACE, f"loupfeed-teams:{_thread_key(activity)}"))


_MENTION_RE = re.compile(r"<at[^>]*>.*?</at>", re.DOTALL)


def _mentions_bot(activity: dict) -> bool:
    """Whether this activity explicitly @mentions us.

    Teams ids in mention entities are channel-prefixed (``28:<app-id>``), so
    compare on a suffix match against both the recipient id and the app id.
    """
    wanted = {
        str((activity.get("recipient") or {}).get("id") or ""),
        _app_id(),
    }
    wanted = {w for w in wanted if w}
    for entity in activity.get("entities") or []:
        if str(entity.get("type") or "").lower() != "mention":
            continue
        mentioned_id = str(((entity.get("mentioned") or {}).get("id")) or "")
        if not mentioned_id:
            continue
        if any(mentioned_id == w or mentioned_id.endswith(f":{w}") for w in wanted):
            return True
    return False


async def _thread_exists(thread_id: str) -> bool:
    """Whether we already have a session for this Teams thread."""
    try:
        await get_client(url=LANGGRAPH_URL).threads.get(thread_id)
        return True
    except Exception:
        return False


# Untagged follow-ups are honored only from the person we are actively talking
# to, and only briefly. Threads are also where the team banters with each
# other; a session alone must not turn into a standing subscription.
FOLLOWUP_WINDOW_SECONDS = 600

# thread id -> (sender key of the last person we engaged with, monotonic time)
_ACTIVE_EXCHANGES: dict[str, tuple[str, float]] = {}


def _sender_key(activity: dict) -> str:
    sender = activity.get("from") or {}
    return str(sender.get("aadObjectId") or sender.get("id") or "")


def _note_exchange(activity: dict) -> None:
    key = _sender_key(activity)
    if key:
        _ACTIVE_EXCHANGES[langgraph_thread_id(activity)] = (key, time.monotonic())


def _in_followup_window(activity: dict) -> bool:
    entry = _ACTIVE_EXCHANGES.get(langgraph_thread_id(activity))
    if not entry:
        return False
    sender, at = entry
    return sender == _sender_key(activity) and time.monotonic() - at <= FOLLOWUP_WINDOW_SECONDS


async def _is_addressed_to_us(activity: dict) -> bool:
    """Gate every inbound message: only act when the message is meant for us.

    RSC (``ChannelMessage.Read.Group``) makes Teams deliver *every* channel
    message to this endpoint, not just mentions — without this gate the agent
    answers ordinary team chatter. Rules:

    - 1:1 chat with the agent: always ours, no mention needed.
    - Channel thread: an untagged message is ours only when it comes from the
      person we are currently in an exchange with, within
      ``FOLLOWUP_WINDOW_SECONDS`` of our last engagement in that thread. A
      session existing is NOT enough — threads carry team banter between
      humans, and answering it made the bot reply to everything (observed on
      the 5 Aug standup thread).
    - Anywhere else (channel top-level, group chat, meeting chat): an explicit
      @mention is required.

    The follow-up window lives in process memory: after a restart the first
    follow-up needs one re-tag, which is acceptable.
    """
    conversation = activity.get("conversation") or {}
    if str(conversation.get("conversationType") or "") == "personal":
        return True
    if _mentions_bot(activity):
        return True
    if ";messageid=" not in str(conversation.get("id") or ""):
        return False
    return _in_followup_window(activity) and await _thread_exists(langgraph_thread_id(activity))


def _clean_text(activity: dict) -> str:
    text = _MENTION_RE.sub("", str(activity.get("text") or ""))
    return re.sub(r"\s+", " ", text).strip()


def _speaker_labeled(activity: dict, text: str) -> str:
    name = str(((activity.get("from") or {}).get("name")) or "").strip()
    return f"{name}: {text}" if name else text


def _graph_context(activity: dict) -> dict:
    """Microsoft Graph ids for the conversation, for the pm graph's read tools.

    The base conversation id (without the ``;messageid=`` thread suffix) is the
    Graph chat id for 1:1/group/meeting chats and the channel id for channels;
    ``channelData`` carries the team's AAD group id and, inside meeting chats,
    the meeting id. All read paths these unlock are RSC-scoped — they only work
    where the Teams app is actually installed.
    """
    conversation = activity.get("conversation") or {}
    conv_id = str(conversation.get("id") or "").split(";messageid=")[0]
    channel_data = activity.get("channelData") or {}
    context = {
        "teams_conversation_id": conv_id,
        "teams_conversation_type": str(conversation.get("conversationType") or ""),
        "teams_tenant_id": str(
            ((channel_data.get("tenant") or {}).get("id")) or conversation.get("tenantId") or ""
        ),
    }
    team_group_id = str(((channel_data.get("team") or {}).get("aadGroupId")) or "")
    if team_group_id:
        context["teams_team_group_id"] = team_group_id
    meeting_id = str(((channel_data.get("meeting") or {}).get("id")) or "")
    if meeting_id:
        context["teams_meeting_id"] = meeting_id
    return context


# ---------------------------------------------------------------------------
# The run.
# ---------------------------------------------------------------------------


async def _process_message(activity: dict) -> None:
    if not await _is_addressed_to_us(activity):
        # Ordinary team chatter — stay silent (no typing indicator, no run).
        return

    text = _clean_text(activity)
    if not text:
        return

    _note_exchange(activity)
    await _send_typing(activity)

    member = await _get_sender_member(activity)
    requester = {
        "requester_name": str(
            member.get("name") or ((activity.get("from") or {}).get("name")) or ""
        ),
        "requester_email": str(member.get("email") or member.get("userPrincipalName") or ""),
        "requester_aad_id": str(
            member.get("aadObjectId") or ((activity.get("from") or {}).get("aadObjectId")) or ""
        ),
    }

    thread_id = langgraph_thread_id(activity)
    client = get_client(url=LANGGRAPH_URL)
    await client.threads.create(thread_id=thread_id, if_exists="do_nothing")

    try:
        result = await client.runs.wait(
            thread_id,
            PM_GRAPH,
            input={"messages": [{"role": "user", "content": _speaker_labeled(activity, text)}]},
            config={
                "configurable": {
                    "teams_thread_key": _thread_key(activity),
                    **requester,
                    **_graph_context(activity),
                }
            },
        )
    except Exception:
        logger.exception("teams: pm run failed for thread %s", thread_id)
        await _reply(
            activity, "Something went wrong while working on that — check the platform logs."
        )
        return

    reply_text = _last_ai_text(result) or "(no reply produced)"
    await _reply(activity, reply_text)
    # Refresh the window from the reply, so a slow run doesn't eat into it.
    _note_exchange(activity)


def _last_ai_text(result: object) -> str:
    messages = None
    if isinstance(result, dict):
        messages = result.get("messages") or (result.get("values") or {}).get("messages")
    if not isinstance(messages, list) or not messages:
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if (
            message.get("type") not in ("ai", "AIMessageChunk")
            and message.get("role") != "assistant"
        ):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = "\n".join(t for t in texts if t)
            if joined.strip():
                return joined
    return ""


# ---------------------------------------------------------------------------
# Endpoint.
# ---------------------------------------------------------------------------


@router.post("/webhooks/teams")
async def teams_messages(request: Request, background_tasks: BackgroundTasks) -> Response:
    if not _configured():
        logger.warning("teams: activity received but TEAMS_APP_ID/PASSWORD not configured")
        return Response(status_code=503)

    auth_ok = await asyncio.to_thread(
        _verify_activity_auth, request.headers.get("Authorization", "")
    )
    if not auth_ok:
        return Response(status_code=401)

    activity = await request.json()
    activity_type = activity.get("type")

    if activity_type == "message":
        background_tasks.add_task(_process_message, activity)
    elif activity_type == "conversationUpdate":
        members_added = activity.get("membersAdded") or []
        bot_id = (activity.get("recipient") or {}).get("id")
        if any(m.get("id") == bot_id for m in members_added):
            background_tasks.add_task(
                _reply,
                activity,
                "👋 I'm **loupfeed** — mention me in a thread, channel, or meeting chat to ask "
                "about or act on your planning system.",
            )
    return Response(status_code=200)
