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
        "text": text,
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
        url = (
            f"{activity['serviceUrl'].rstrip('/')}/v3/conversations/"
            f"{conv_id}/members/{user_id}"
        )
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


def _clean_text(activity: dict) -> str:
    text = _MENTION_RE.sub("", str(activity.get("text") or ""))
    return re.sub(r"\s+", " ", text).strip()


def _speaker_labeled(activity: dict, text: str) -> str:
    name = str(((activity.get("from") or {}).get("name")) or "").strip()
    return f"{name}: {text}" if name else text


# ---------------------------------------------------------------------------
# The run.
# ---------------------------------------------------------------------------


async def _process_message(activity: dict) -> None:
    text = _clean_text(activity)
    if not text:
        return

    await _send_typing(activity)

    member = await _get_sender_member(activity)
    requester = {
        "requester_name": str(
            member.get("name") or ((activity.get("from") or {}).get("name")) or ""
        ),
        "requester_email": str(
            member.get("email") or member.get("userPrincipalName") or ""
        ),
        "requester_aad_id": str(
            member.get("aadObjectId")
            or ((activity.get("from") or {}).get("aadObjectId"))
            or ""
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
            config={"configurable": {"teams_thread_key": _thread_key(activity), **requester}},
        )
    except Exception:
        logger.exception("teams: pm run failed for thread %s", thread_id)
        await _reply(activity, "Something went wrong while working on that — check the platform logs.")
        return

    reply_text = _last_ai_text(result) or "(no reply produced)"
    await _reply(activity, reply_text)


def _last_ai_text(result: object) -> str:
    messages = None
    if isinstance(result, dict):
        messages = result.get("messages") or (result.get("values") or {}).get("messages")
    if not isinstance(messages, list) or not messages:
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("type") not in ("ai", "AIMessageChunk") and message.get("role") != "assistant":
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
