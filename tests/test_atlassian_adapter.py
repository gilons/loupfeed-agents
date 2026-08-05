"""Atlassian entry-app webhook: auth, normalisation, and the mention gate."""

from __future__ import annotations

import json as jsonlib
from unittest.mock import MagicMock, patch

from agent.atlassian_adapter import _adf_text, is_addressed_to_us, normalise


class _Resp:
    """Stand-in for AtlassianResponse from the shared Atlassian client."""

    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self.text = jsonlib.dumps(payload)
        self.via_app = True

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self):
        return jsonlib.loads(self.text)


APP = "712020:3b1d36e0-61af-4066-bd92-8c391b60657d"
HUMAN = "712020:55719db7-9d3c-4a20-a490-dfd2b9baab4c"


def _adf(text: str, mention: str | None = None) -> dict:
    content: list[dict] = [{"type": "text", "text": text}]
    if mention:
        content.append({"type": "mention", "attrs": {"id": mention, "text": "@loupfeed"}})
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": content}]}


# Shapes recorded from real events during the 2026-08-05 spike.
COMMENT_EVENT = {
    "eventType": "avi:jira:commented:issue",
    "issue": {"key": "SPB-3", "fields": {"summary": "Broken export", "assignee": None}},
    "comment": {"body": _adf("please pick this up ", APP)},
    "atlassianId": HUMAN,
}
ASSIGN_EVENT = {
    "eventType": "avi:jira:assigned:issue",
    "issue": {
        "key": "SPB-3",
        "fields": {"summary": "Broken export", "assignee": {"accountId": APP}},
    },
    "atlassianId": HUMAN,
    "changelog": {"items": [{"field": "assignee"}]},
}
CHATTER_EVENT = {
    "eventType": "avi:jira:commented:issue",
    "issue": {"key": "SPB-4", "fields": {"summary": "Other bug", "assignee": None}},
    "comment": {"body": _adf("just thinking out loud here")},
    "atlassianId": HUMAN,
}


def test_adf_flattening():
    assert _adf_text(_adf("hello world")) == "hello world"


def test_comment_mention_is_ours():
    n = normalise(COMMENT_EVENT)
    assert n["issue_key"] == "SPB-3"
    assert n["product"] == "jira"
    assert APP in n["mentions"]
    assert n["requester_account_id"] == HUMAN
    assert is_addressed_to_us(n, APP) is True


def test_assignment_to_us_is_ours():
    n = normalise(ASSIGN_EVENT)
    assert n["assignee_account_id"] == APP
    assert "assignee" in n["changed_fields"]
    assert is_addressed_to_us(n, APP) is True


def test_ordinary_project_chatter_is_ignored():
    n = normalise(CHATTER_EVENT)
    assert is_addressed_to_us(n, APP) is False


def test_assignment_to_someone_else_is_ignored():
    event = {
        **ASSIGN_EVENT,
        "issue": {"key": "SPB-3", "fields": {"summary": "x", "assignee": {"accountId": HUMAN}}},
    }
    assert is_addressed_to_us(normalise(event), APP) is False


def test_confluence_comment_normalises():
    n = normalise(
        {
            "eventType": "avi:confluence:created:comment",
            "content": {"id": "288522242", "title": "Re: Form Standardization Phase 1 PRD"},
            "atlassianId": HUMAN,
        }
    )
    assert n["product"] == "confluence"
    assert n["page_id"] == "288522242"


def test_unauthenticated_request_is_rejected():
    from fastapi.testclient import TestClient

    from agent.webapp import app

    async def _no_dispatch(_normalised):
        return None

    client = TestClient(app)
    with (
        patch.dict("os.environ", {"ATLASSIAN_APP_SHARED_SECRET": "s3cret"}, clear=False),
        patch("agent.atlassian_adapter.dispatch", _no_dispatch),
    ):
        assert client.post("/webhooks/atlassian", json={"event": COMMENT_EVENT}).status_code == 401
        ok = client.post(
            "/webhooks/atlassian",
            json={"event": COMMENT_EVENT, "appAccountId": APP},
            headers={"X-Loupfeed-Secret": "s3cret"},
        )
        assert ok.status_code == 202


def test_missing_configured_secret_rejects_everything():
    from fastapi.testclient import TestClient

    from agent.webapp import app

    client = TestClient(app)
    with patch.dict("os.environ", {"ATLASSIAN_APP_SHARED_SECRET": ""}, clear=False):
        r = client.post(
            "/webhooks/atlassian",
            json={"event": COMMENT_EVENT},
            headers={"X-Loupfeed-Secret": ""},
        )
        assert r.status_code == 401


# --- dispatch: routing, threads, prompts, replies -------------------------


def test_assignment_routes_to_the_coding_agent():
    from agent.atlassian_adapter import CODING_GRAPH, route

    assert route(normalise(ASSIGN_EVENT)) == CODING_GRAPH


def test_mention_routes_to_the_pm_agent():
    from agent.atlassian_adapter import PM_GRAPH, route

    assert route(normalise(COMMENT_EVENT)) == PM_GRAPH


def test_thread_id_is_stable_per_item_and_distinct_across_items():
    from agent.atlassian_adapter import langgraph_thread_id

    a = langgraph_thread_id(normalise(COMMENT_EVENT))
    b = langgraph_thread_id(normalise(ASSIGN_EVENT))  # same issue, other event
    c = langgraph_thread_id(normalise(CHATTER_EVENT))  # different issue
    assert a == b, "follow-ups on one issue must continue one thread"
    assert a != c


def test_prompts_carry_context_and_the_reply_contract():
    from agent.atlassian_adapter import CODING_GRAPH, PM_GRAPH, build_prompt

    coding = build_prompt(normalise(ASSIGN_EVENT), CODING_GRAPH)
    assert "SPB-3" in coding and "pull request" in coding and "issue key" in coding
    # A stale sandbox clone put an already-merged commit into PR #2494.
    assert "origin/main" in coding and "only your own commits" in coding.lower()
    # It also claimed checks passed when none had run.
    assert "read its actual result" in coding
    pm = build_prompt(normalise(COMMENT_EVENT), PM_GRAPH)
    assert "SPB-3" in pm and "posted for you" in pm
    # It answered twice in testing: once with its own tools, once via the adapter.
    assert "Do NOT post a comment yourself" in pm
    assert "do NOT comment on the issue yourself" in coding
    assert "please pick this up" in pm, "the human's own words must reach the agent"


def test_reply_posts_adf_to_jira_and_redacts_internals():
    from agent.atlassian_adapter import post_reply

    captured = {}

    class _R:
        status_code = 201

    def _post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        return _R()

    env = {"ATLASSIAN_EMAIL": "a@b.c", "ATLASSIAN_API_TOKEN": "t"}
    with (
        patch.dict("os.environ", env, clear=False),
        patch("agent.atlassian_adapter.requests.post", side_effect=_post),
    ):
        assert post_reply(normalise(COMMENT_EVENT), "Done. Chat.Read.All was needed.") is True
    assert "/rest/api/3/issue/SPB-3/comment" in captured["url"]
    text = captured["json"]["body"]["content"][0]["content"][0]["text"]
    assert "Chat.Read.All" not in text, "internals must not leak into a public comment"


def test_reply_posts_storage_format_to_confluence():
    from agent.atlassian_adapter import post_reply

    captured = {}

    class _R:
        status_code = 201

    def _post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        return _R()

    n = normalise(
        {
            "eventType": "avi:confluence:created:comment",
            "content": {"id": "123", "title": "Re: PRD"},
            "atlassianId": HUMAN,
        }
    )
    env = {"ATLASSIAN_EMAIL": "a@b.c", "ATLASSIAN_API_TOKEN": "t"}
    with (
        patch.dict("os.environ", env, clear=False),
        patch("agent.atlassian_adapter.requests.post", side_effect=_post),
    ):
        assert post_reply(n, "Drafted the section.") is True
    assert "/wiki/api/v2/footer-comments" in captured["url"]
    assert captured["json"]["pageId"] == "123"


def test_reply_without_credentials_is_a_clean_no():
    from agent.atlassian_adapter import post_reply

    with patch.dict("os.environ", {"ATLASSIAN_EMAIL": "", "ATLASSIAN_API_TOKEN": ""}, clear=False):
        assert post_reply(normalise(COMMENT_EVENT), "hi") is False


def test_last_ai_text_prefers_the_final_ai_message():
    from agent.atlassian_adapter import last_ai_text

    result = {
        "messages": [
            {"type": "human", "content": "do it"},
            {"type": "ai", "content": "first"},
            {"type": "ai", "content": [{"type": "text", "text": "final answer"}]},
        ]
    }
    assert last_ai_text(result) == "final answer"


def test_confluence_comment_body_is_hydrated_so_mentions_are_visible():
    """Confluence events carry only the comment id: fetch the body or the
    mention gate can never fire (found live, 2026-08-05)."""
    from agent.atlassian_adapter import hydrate_confluence_comment, is_addressed_to_us

    n = normalise(
        {
            "eventType": "avi:confluence:created:comment",
            "content": {"id": "288686083", "title": "Re: probe"},
            "atlassianId": HUMAN,
        }
    )
    assert is_addressed_to_us(n, APP) is False, "no body yet, nothing to match"

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return {
                "pageId": "288194578",
                "body": {
                    "storage": {
                        "value": '<p>Hi <ac:link><ri:user ri:account-id="%s" /></ac:link>, '
                        "what is this page for?</p>" % APP
                    }
                },
            }

    env = {"ATLASSIAN_EMAIL": "a@b.c", "ATLASSIAN_API_TOKEN": "t"}
    with (
        patch.dict("os.environ", env, clear=False),
        patch(
            "agent.atlassian_adapter.atlassian_request",
            return_value=_Resp(200, _R.json()),
        ),
    ):
        hydrated = hydrate_confluence_comment(n)

    assert APP in hydrated["mentions"]
    assert "what is this page for?" in hydrated["text"]
    assert hydrated["page_id"] == "288194578", "reply belongs on the page, not the comment"
    assert is_addressed_to_us(hydrated, APP) is True


def test_async_paths_never_block():
    """`langgraph dev`'s blockbuster raises on blocking I/O inside async
    functions, which is how the first live Confluence dispatch 500'd. Both
    HTTP-doing helpers must be reached through asyncio.to_thread."""
    import inspect

    from agent import atlassian_adapter as mod

    webhook = inspect.getsource(mod.atlassian_webhook)
    assert "asyncio.to_thread(hydrate_confluence" in webhook
    dispatch_src = inspect.getsource(mod.dispatch)
    assert "post_reply" in dispatch_src
    assert "asyncio.to_thread(\n            post_reply" in dispatch_src or (
        "asyncio.to_thread(post_reply" in dispatch_src
    )
    assert "\n    post_reply(" not in dispatch_src, "post_reply must not be called directly"


def test_mention_written_into_a_page_body_is_detected():
    """A mention in the page text itself, not a comment: the page-updated
    event carries no body either, so it has to be fetched (2026-08-05)."""
    from agent.atlassian_adapter import hydrate_confluence, is_addressed_to_us

    n = normalise(
        {
            "eventType": "avi:confluence:updated:page",
            "content": {"id": "281477123", "title": "Form Standardization Phase 1 PRD"},
            "atlassianId": HUMAN,
        }
    )
    assert is_addressed_to_us(n, APP) is False

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return {
                "title": "Form Standardization Phase 1 PRD",
                "body": {
                    "storage": {
                        "value": '<p><ac:link><ri:user ri:account-id="%s" /></ac:link> please '
                        "draft the TechSpec skeleton from the call transcripts.</p>" % APP
                    }
                },
            }

    env = {"ATLASSIAN_EMAIL": "a@b.c", "ATLASSIAN_API_TOKEN": "t"}
    with (
        patch.dict("os.environ", env, clear=False),
        patch(
            "agent.atlassian_adapter.atlassian_request",
            return_value=_Resp(200, _R.json()),
        ),
    ):
        hydrated = hydrate_confluence(n)

    assert APP in hydrated["mentions"]
    assert hydrated["page_id"] == "281477123", "the page is already the reply surface"
    assert is_addressed_to_us(hydrated, APP) is True


def test_page_hydration_leaves_jira_events_alone():
    from agent.atlassian_adapter import hydrate_confluence

    n = normalise(COMMENT_EVENT)
    with patch("agent.atlassian_adapter.atlassian_request") as req:
        out = hydrate_confluence(n)
    req.assert_not_called()
    assert out["issue_key"] == "SPB-3"


def test_one_assignment_dispatches_once():
    """Jira emits assigned:issue AND updated:issue for a single assignment;
    acting on both ran the coding agent twice (seen live on SPB-5)."""
    from agent.atlassian_adapter import is_addressed_to_us

    assigned = normalise(ASSIGN_EVENT)
    updated = normalise({**ASSIGN_EVENT, "eventType": "avi:jira:updated:issue"})
    assert is_addressed_to_us(assigned, APP) is True
    assert is_addressed_to_us(updated, APP) is False


def test_a_mention_on_an_issue_assigned_to_us_still_works():
    """The narrower assignment rule must not swallow mentions."""
    from agent.atlassian_adapter import is_addressed_to_us

    event = {
        "eventType": "avi:jira:commented:issue",
        "issue": {"key": "SPB-3", "fields": {"summary": "x", "assignee": {"accountId": APP}}},
        "comment": {"body": _adf("one more thing ", APP)},
        "atlassianId": HUMAN,
    }
    assert is_addressed_to_us(normalise(event), APP) is True


def test_reply_prefers_the_app_so_the_app_authors_the_comment():
    """With a reply URL configured the deployment must not use its own
    Atlassian credential: the comment should be authored by the app."""
    from agent.atlassian_adapter import post_reply

    calls = []

    class _R:
        status_code = 200
        text = ""

    def _post(url, **kw):
        calls.append((url, kw.get("headers", {}), kw.get("json")))
        return _R()

    env = {
        "ATLASSIAN_APP_REPLY_URL": "https://app.example/x1/tok",
        "ATLASSIAN_APP_SHARED_SECRET": "s3cret",
        "ATLASSIAN_EMAIL": "a@b.c",
        "ATLASSIAN_API_TOKEN": "t",
    }
    with (
        patch.dict("os.environ", env, clear=False),
        patch("agent.atlassian_adapter.requests.post", side_effect=_post),
    ):
        assert post_reply(normalise(COMMENT_EVENT), "Done.") is True

    assert len(calls) == 1, "exactly one call: to the app, not to Atlassian"
    url, headers, body = calls[0]
    assert url == "https://app.example/x1/tok"
    assert headers.get("X-Loupfeed-Secret") == "s3cret"
    assert body == {"text": "Done.", "issueKey": "SPB-3"}


def test_reply_falls_back_to_the_deployment_credential():
    """If the app path is unavailable, the old credential still works."""
    from agent.atlassian_adapter import post_reply

    urls = []

    class _Fail:
        status_code = 502
        text = "nope"

    class _Ok:
        status_code = 201
        text = ""

    def _post(url, **kw):
        urls.append(url)
        return _Fail() if "app.example" in url else _Ok()

    env = {
        "ATLASSIAN_APP_REPLY_URL": "https://app.example/x1/tok",
        "ATLASSIAN_APP_SHARED_SECRET": "s3cret",
        "ATLASSIAN_EMAIL": "a@b.c",
        "ATLASSIAN_API_TOKEN": "t",
    }
    with (
        patch.dict("os.environ", env, clear=False),
        patch("agent.atlassian_adapter.requests.post", side_effect=_post),
    ):
        assert post_reply(normalise(COMMENT_EVENT), "Done.") is True
    assert len(urls) == 2 and "/rest/api/3/issue/SPB-3/comment" in urls[1]


def test_reads_go_through_the_app_when_a_proxy_is_configured():
    """The read paths must prefer the app, so no Atlassian credential is
    needed on the deployment (asApp end state)."""
    from agent.utils import atlassian_api

    captured = {}

    class _P:
        status_code = 200
        text = jsonlib.dumps({"status": 200, "body": jsonlib.dumps({"title": "PRD"})})

        @staticmethod
        def json():
            return jsonlib.loads(_P.text)

    def _post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        captured["headers"] = kw.get("headers")
        return _P()

    env = {
        "ATLASSIAN_APP_PROXY_URL": "https://app.example/x1/tok",
        "ATLASSIAN_APP_SHARED_SECRET": "s3cret",
        "ATLASSIAN_EMAIL": "a@b.c",
        "ATLASSIAN_API_TOKEN": "t",
    }
    with (
        patch.dict("os.environ", env, clear=False),
        patch("agent.utils.atlassian_api.requests.post", side_effect=_post),
        patch("agent.utils.atlassian_api.requests.request") as direct,
    ):
        r = atlassian_api.atlassian_request("confluence", "GET", "/wiki/api/v2/pages/1")

    direct.assert_not_called()
    assert r.via_app is True and r.json()["title"] == "PRD"
    assert captured["headers"]["X-Loupfeed-Secret"] == "s3cret"
    assert captured["json"]["path"] == "/wiki/api/v2/pages/1"


def test_a_refused_path_does_not_silently_fall_back():
    """403 is an allowlist decision by the app, not a transport failure: the
    deployment must not widen its own access by retrying with the token."""
    from agent.utils import atlassian_api

    class _Refused:
        status_code = 403
        text = "not allowed"

    env = {
        "ATLASSIAN_APP_PROXY_URL": "https://app.example/x1/tok",
        "ATLASSIAN_APP_SHARED_SECRET": "s3cret",
        "ATLASSIAN_EMAIL": "a@b.c",
        "ATLASSIAN_API_TOKEN": "t",
    }
    with (
        patch.dict("os.environ", env, clear=False),
        patch("agent.utils.atlassian_api.requests.post", return_value=_Refused()),
        patch("agent.utils.atlassian_api.requests.request") as direct,
    ):
        r = atlassian_api.atlassian_request("jira", "DELETE", "/rest/api/3/issue/SPB-1")
    direct.assert_not_called()
    assert r.status_code == 403


def test_without_a_proxy_the_credential_still_works():
    from agent.utils import atlassian_api

    ok = MagicMock(status_code=200, text="{}")
    with (
        patch.dict(
            "os.environ",
            {"ATLASSIAN_APP_PROXY_URL": "", "ATLASSIAN_EMAIL": "a@b.c", "ATLASSIAN_API_TOKEN": "t"},
            clear=False,
        ),
        patch("agent.utils.atlassian_api.requests.request", return_value=ok) as direct,
    ):
        r = atlassian_api.atlassian_request("confluence", "GET", "/wiki/api/v2/pages/1")
    direct.assert_called_once()
    assert r.via_app is False and r.ok
