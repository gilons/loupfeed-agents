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

import asyncio
import hmac
import logging
import os
import re
import uuid
from typing import Any

import requests
from fastapi import APIRouter, BackgroundTasks, Request, Response
from langgraph_sdk import get_client

from .surfaces import surface_for_issue
from .utils.atlassian_api import atlassian_request, use_app_for
from .utils.markdown_reply import markdown_to_adf, markdown_to_storage
from .utils.redact_internals import redact_internals

logger = logging.getLogger(__name__)

router = APIRouter()

_SECRET_HEADER = "x-loupfeed-secret"
_TIMEOUT = 30
LANGGRAPH_URL = os.environ.get("LANGGRAPH_URL") or os.environ.get(
    "LANGGRAPH_URL_PROD", "http://localhost:2024"
)


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
        "issue_type": str((fields.get("issuetype") or {}).get("name") or "") or None,
        "page_id": str(content.get("id") or "") or None,
        "title": str(fields.get("summary") or content.get("title") or "") or None,
        "requester_account_id": str(event.get("atlassianId") or "") or None,
        "assignee_account_id": str((fields.get("assignee") or {}).get("accountId") or "") or None,
        "text": _adf_text(body).strip() or None,
        "mentions": _mention_ids(body),
        "changed_fields": [i.get("field") for i in (event.get("changelog") or {}).get("items", [])],
    }


_STORAGE_MENTION = re.compile(r'ri:account-id="([^"]+)"')
_TAGS = re.compile(r"<[^>]+>")


def hydrate_confluence_page(normalised: dict[str, Any]) -> dict[str, Any]:
    """Fill in a page's own body so a mention written INTO the page is seen.

    ``avi:confluence:updated:page`` carries no body either, so mentioning the
    agent in the page text itself (rather than in a comment) was invisible.
    The reply still goes to the page, which is already the surface.
    """
    if normalised.get("event_type") != "avi:confluence:updated:page":
        return normalised
    page_id = normalised.get("page_id")
    if not page_id:
        return normalised
    r = atlassian_request("confluence", "GET", f"/wiki/api/v2/pages/{page_id}?body-format=storage")
    if not r.ok:
        logger.warning("confluence page hydrate failed: %s", r.status_code)
        return normalised
    data = r.json()
    storage = str(((data.get("body") or {}).get("storage") or {}).get("value") or "")
    normalised["mentions"] = _STORAGE_MENTION.findall(storage)
    normalised["title"] = str(data.get("title") or normalised.get("title") or "") or None
    # Keep the page text out of the prompt: pages are long and the agent has
    # tools to read it. The mention is what we needed from the body.
    return normalised


def hydrate_confluence_comment(normalised: dict[str, Any]) -> dict[str, Any]:
    """Fill in a Confluence comment's body, which its event does not carry.

    Jira comment events ship the full ADF body; Confluence events ship only
    the comment id, so a mention is invisible until the body is fetched.
    Without this the gate can never see a Confluence mention.
    """
    if normalised.get("product") != "confluence" or normalised.get("text"):
        return normalised
    comment_id = normalised.get("page_id")
    if not comment_id:
        return normalised
    r = atlassian_request(
        "confluence", "GET", f"/wiki/api/v2/footer-comments/{comment_id}?body-format=storage"
    )
    if not r.ok:
        logger.warning("confluence hydrate failed: %s", r.status_code)
        return normalised
    data = r.json()
    storage = str(((data.get("body") or {}).get("storage") or {}).get("value") or "")
    normalised["mentions"] = _STORAGE_MENTION.findall(storage)
    normalised["text"] = _TAGS.sub(" ", storage).strip() or None
    # The comment hangs off a page; that page is the surface we reply on.
    parent = str(data.get("pageId") or "")
    if parent:
        normalised["page_id"] = parent
    return normalised


def hydrate_confluence(normalised: dict[str, Any]) -> dict[str, Any]:
    """Confluence events never carry bodies; fetch whichever one applies."""
    return hydrate_confluence_page(hydrate_confluence_comment(normalised))


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
    # Jira emits BOTH assigned:issue and updated:issue for one assignment, so
    # only the specific event counts or every assignment dispatches twice.
    if (
        normalised.get("event_type") == "avi:jira:assigned:issue"
        and normalised.get("assignee_account_id") == app_account_id
    ):
        return True
    return False


CODING_GRAPH = "agent"
PM_GRAPH = "pm"
TRIAGE_GRAPH = "triage"

# One surface item maps to one agent thread, the same rule every entry point
# follows, so follow-up comments continue the same conversation.
_THREAD_NAMESPACE = uuid.UUID("6f0a26b5-2f52-5a2e-9f1e-0a4b6a1c9d31")


def surface_key(normalised: dict[str, Any]) -> str:
    if normalised.get("issue_key"):
        return f"jira:{normalised['issue_key']}"
    return f"confluence:{normalised.get('page_id')}"


def langgraph_thread_id(normalised: dict[str, Any]) -> str:
    return str(uuid.uuid5(_THREAD_NAMESPACE, f"loupfeed-atlassian:{surface_key(normalised)}"))


def route(normalised: dict[str, Any]) -> str:
    """Assignment means do the work; a mention means answer, triage, or draft.

    Assigning an issue to the app is the "bug to PR" contract, so it goes to
    the coding agent. A mention on an issue whose Jira project the surface
    registry maps to a repository is a bug report, and goes to triage: that
    mapping is the deployment saying "this project holds defects in this app".
    Everything else that names us is a question or a drafting request and
    belongs to the pm agent.

    Routing on the project rather than on the wording is deliberate. Sniffing a
    comment for triage-like intent makes the entry point unpredictable, and a
    person who wants something else from a bug still gets a useful answer from
    triage, which reads the ticket either way.
    """
    if "assignee" in (normalised.get("changed_fields") or []):
        return CODING_GRAPH
    if normalised.get("issue_key") and surface_for_issue(normalised["issue_key"]):
        return TRIAGE_GRAPH
    return PM_GRAPH


def build_prompt(normalised: dict[str, Any], graph: str) -> str:
    where = (
        f"Jira issue {normalised['issue_key']}"
        if normalised.get("issue_key")
        else f"Confluence page {normalised.get('page_id')}"
    )
    title = normalised.get("title") or ""
    asked = normalised.get("text") or ""
    if graph == CODING_GRAPH:
        return (
            f"You have been assigned {where}"
            + (f' ("{title}")' if title else "")
            + ".\n\nStart from a clean base: in the repository, `git fetch origin` and "
            "create your branch from `origin/main` (`git checkout -B <branch> origin/main`). "
            "The sandbox may still hold work from an earlier task, and your pull request must "
            "contain ONLY your own commits: if `git log origin/main..HEAD` shows anything you "
            "did not write, you branched from the wrong place, so start over from origin/main.\n\n"
            "Read the issue with your Atlassian tools, implement the fix in the "
            "bound repository, and open a pull request whose title carries the issue key. "
            "Then summarise what you did in one short paragraph as your final message: "
            "the platform posts it as a comment on the issue for you, so write it for the "
            "reporter, not for a log, and do NOT comment on the issue yourself. "
            "Never claim a check or test passed unless you read its actual result.\n"
            + (f"\nThe request said: {asked}\n" if asked else "")
        )
    if graph == TRIAGE_GRAPH:
        return (
            f"Triage {where}"
            + (f' ("{title}")' if title else "")
            + ".\n\nRead the ticket and its comments with your Atlassian tools first, then work "
            "through your procedure: prior triage, anchor, pin, window, diffs. Your final "
            "message is posted as a comment on the issue for you, so write the report for the "
            "developer who picks this up and do NOT comment on the issue yourself.\n\n"
            "Name no suspect commit whose diff you have not read, and blame no line at any ref "
            "other than the release it was reported from.\n"
            + (f"\nThe request said: {asked}\n" if asked else "")
        )
    return (
        f"You were mentioned on {where}"
        + (f' ("{title}")' if title else "")
        + ".\n\nRead it with your Atlassian tools before answering, and write a "
        "chat-length answer: lead with the answer or the action you took.\n\n"
        "Do NOT post a comment yourself. Your final message is posted for you, as a "
        "comment on that item, by the platform. Commenting as well produces two "
        "answers, which happened in testing.\n"
        + (f"\nThe request said: {asked}\n" if asked else "")
    )


def _atlassian_auth() -> tuple[str, str] | None:
    email = os.environ.get("ATLASSIAN_EMAIL", "")
    token = os.environ.get("ATLASSIAN_API_TOKEN", "")
    return (email, token) if email and token else None


def _site_base() -> str:
    return os.environ.get("ATLASSIAN_SITE_URL", "https://dinolabgmbh.atlassian.net").rstrip("/")


def reply_webtrigger_url() -> str:
    """Where the entry app listens for replies, when configured."""
    return os.environ.get("ATLASSIAN_APP_REPLY_URL", "").strip()


def _reply_via_app(normalised: dict[str, Any], text: str) -> bool:
    """Ask the entry app to post the comment, so the APP authors it.

    Preferred over our own credential: the comment shows as the app, and a
    deployment needs no Atlassian token at all. Returns False if the app has
    no reply URL configured or the call fails, so the caller can fall back.
    """
    if not use_app_for(attributed=True):
        return False
    url = reply_webtrigger_url()
    secret = shared_secret()
    if not url or not secret:
        return False
    # Render here rather than in the app: the app forwards what it is given, and
    # a flat `text` reply is what made reports arrive as one paragraph with
    # `**bold**` showing literally.
    payload: dict[str, Any] = {"text": text}
    if normalised.get("issue_key"):
        payload["issueKey"] = normalised["issue_key"]
        payload["bodyAdf"] = markdown_to_adf(text)
    else:
        payload["pageId"] = normalised.get("page_id")
        payload["bodyStorage"] = markdown_to_storage(text)
    try:
        r = requests.post(
            url,
            headers={"Content-Type": "application/json", "X-Loupfeed-Secret": secret},
            json=payload,
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("atlassian app reply failed: %s: %s", type(exc).__name__, exc)
        return False
    if r.status_code >= 400:
        logger.warning("atlassian app reply failed: %s %s", r.status_code, r.text[:200])
        return False
    logger.info("atlassian reply posted by the app for %s", surface_key(normalised))
    return True


def post_reply(normalised: dict[str, Any], text: str) -> bool:
    """Comment the agent's answer back onto the issue or page.

    Prefers the entry app (comment authored by the app, no credential here);
    falls back to this deployment's own Atlassian credential.
    """
    clean = redact_internals(text)[:4000]
    if _reply_via_app(normalised, clean):
        return True
    auth = _atlassian_auth()
    if not auth:
        logger.warning("atlassian reply skipped: no app reply URL and no Atlassian credential")
        return False
    try:
        if normalised.get("issue_key"):
            r = requests.post(
                f"{_site_base()}/rest/api/3/issue/{normalised['issue_key']}/comment",
                auth=auth,
                json={"body": markdown_to_adf(clean)},
                timeout=_TIMEOUT,
            )
        else:
            r = requests.post(
                f"{_site_base()}/wiki/api/v2/footer-comments",
                auth=auth,
                json={
                    "pageId": normalised.get("page_id"),
                    "body": {
                        "representation": "storage",
                        "value": markdown_to_storage(clean),
                    },
                },
                timeout=_TIMEOUT,
            )
    except requests.RequestException as exc:
        logger.warning("atlassian reply failed: %s: %s", type(exc).__name__, exc)
        return False
    if r.status_code >= 400:
        logger.warning("atlassian reply failed: %s %s", r.status_code, r.text[:200])
        return False
    logger.info("atlassian reply posted with the deployment credential")
    return True


async def dispatch(normalised: dict[str, Any]) -> None:
    graph = route(normalised)
    thread_id = langgraph_thread_id(normalised)
    logger.info("atlassian dispatch: %s -> %s thread=%s", surface_key(normalised), graph, thread_id)
    client = get_client(url=LANGGRAPH_URL)
    await client.threads.create(thread_id=thread_id, if_exists="do_nothing")
    configurable: dict[str, Any] = {
        "source": "atlassian",
        "atlassian_surface": surface_key(normalised),
        "requester_atlassian_account_id": normalised.get("requester_account_id"),
    }
    if graph == TRIAGE_GRAPH:
        surface = surface_for_issue(normalised.get("issue_key"))
        if surface:
            configurable["triage_surface"] = surface["key"]
    try:
        result = await client.runs.wait(
            thread_id,
            graph,
            input={"messages": [{"role": "user", "content": build_prompt(normalised, graph)}]},
            config={"configurable": configurable},
        )
    except Exception:
        logger.exception("atlassian dispatch: run failed for %s", surface_key(normalised))
        await asyncio.to_thread(
            post_reply,
            normalised,
            "Something went wrong while working on that. Giles should check the platform logs.",
        )
        return
    await asyncio.to_thread(post_reply, normalised, last_ai_text(result) or "(no reply produced)")


def last_ai_text(result: object) -> str:
    messages = (result or {}).get("messages", []) if isinstance(result, dict) else []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("type") != "ai":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            if text:
                return text
    return ""


@router.post("/webhooks/atlassian")
async def atlassian_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    if not _authorised(request):
        logger.warning("atlassian webhook: rejected unauthenticated request")
        return Response(status_code=401)

    payload = await request.json()
    event = payload.get("event") or payload
    # blockbuster forbids blocking I/O in async routes: hydration does HTTP.
    normalised = await asyncio.to_thread(hydrate_confluence, normalise(event))
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
    if addressed:
        # Runs take minutes; acknowledge now, work in the background.
        background_tasks.add_task(dispatch, normalised)
    return Response(status_code=202)
