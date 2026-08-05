"""Atlassian entry-app webhook: auth, normalisation, and the mention gate."""

from __future__ import annotations

from unittest.mock import patch

from agent.atlassian_adapter import _adf_text, is_addressed_to_us, normalise

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

    client = TestClient(app)
    with patch.dict("os.environ", {"ATLASSIAN_APP_SHARED_SECRET": "s3cret"}, clear=False):
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
