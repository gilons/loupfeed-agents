"""Filing loupfeed feedback as Jira bugs."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest

from agent import loupfeed_bugs
from agent.loupfeed_bugs import file_feedback, verify_signature

SECRET = "s3cret"

SURFACES = [
    {
        "key": "deliveru-webapp",
        "repo": "dinolabdev/deliveru",
        "jira_projects": ["SPB"],
        "loupfeed": [
            {
                "name": "production",
                "api": "https://loupfeed.example",
                "org": "deliveru-prod",
                "project": "deliveru-prod",
                "token_env": "T_PROD",
            },
            {
                "name": "stages",
                "api": "https://dev-loupfeed.example",
                "org": "deliveru",
                "project": "deliveru",
                "token_env": "T_DEV",
            },
        ],
    }
]


def delivery(**over: Any) -> dict[str, Any]:
    event = {
        "eventId": "evt-1",
        "threadId": "thr-9",
        "receivedAt": "2026-08-19T10:00:00.000Z",
        "text": "The stage chip is unreadable on the grid",
        "release": "deliveru-webapp@881e0654e",
        "environment": "feat-consent-forms",
        "route": "/candidates",
        "url": "https://feat-x.deliveru.dev/candidates",
        "user": {"email": "recruiter@acme.de"},
        "element": {"text": "Screening"},
    }
    event.update(over.pop("event", {}))
    body = {
        "type": "feedback.created",
        "instance": {"org": "deliveru", "project": "deliveru"},
        "event": event,
    }
    body.update(over)
    return body


class Reply:
    def __init__(self, status: int, text: str = "") -> None:
        self.status, self.text = status, text

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@pytest.fixture(autouse=True)
def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loupfeed_bugs, "surface_for_instance", _lookup)


def _lookup(org: str, project: str, path: str | None = None):
    for surface in SURFACES:
        for target in surface["loupfeed"]:
            if target["org"] == org and target["project"] == project:
                return surface, target
    return None


def _calls(monkeypatch: pytest.MonkeyPatch, replies: list[Reply]) -> list[tuple]:
    seen: list[tuple] = []
    queue = list(replies)

    def fake(product: str, method: str, path: str, body: Any = None, **kw: Any) -> Reply:
        seen.append((method, path, body))
        return queue.pop(0)

    monkeypatch.setattr(loupfeed_bugs, "atlassian_request", fake)
    return seen


# ——— signature ———


def _sign(body: bytes, timestamp: str, secret: str = SECRET) -> str:
    return (
        "sha256="
        + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    )


def test_accepts_a_fresh_signature() -> None:
    body, ts = b'{"a":1}', str(int(time.time() * 1000))
    assert verify_signature(body, ts, _sign(body, ts), SECRET)


def test_rejects_a_wrong_secret_or_tampered_body() -> None:
    body, ts = b'{"a":1}', str(int(time.time() * 1000))
    assert not verify_signature(body, ts, _sign(body, ts, "other"), SECRET)
    assert not verify_signature(b'{"a":2}', ts, _sign(body, ts), SECRET)


def test_rejects_a_replay_from_last_week() -> None:
    body = b'{"a":1}'
    old = str(int((time.time() - 8 * 24 * 3600) * 1000))
    # Correctly signed, and still refused: the age is inside the check.
    assert not verify_signature(body, old, _sign(body, old), SECRET)


def test_rejects_a_missing_or_unparseable_timestamp() -> None:
    body = b'{"a":1}'
    assert not verify_signature(body, "", _sign(body, "0"), SECRET)
    assert not verify_signature(body, "not-a-number", "sha256=x", SECRET)


# ——— filing ———


def test_creates_a_bug_on_the_surfaces_board(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _calls(
        monkeypatch,
        [Reply(200, json.dumps({"issues": []})), Reply(201, json.dumps({"key": "SPB-42"}))],
    )
    result = file_feedback(delivery())
    assert result == {"status": "created", "issue": "SPB-42"}

    _, search_path, _ = seen[0]
    assert "SPB" in search_path and "loupfeed-thr-9" in search_path
    method, path, body = seen[1]
    assert (method, path) == ("POST", "/rest/api/3/issue")
    fields = body["fields"]
    assert fields["project"]["key"] == "SPB"
    assert fields["issuetype"]["name"] == "Bug"
    assert "loupfeed-thr-9" in fields["labels"]
    # The stage the report came from has to survive into the issue, or nobody
    # can tell a prod report from one on somebody's branch.
    rendered = json.dumps(fields["description"])
    assert "feat-consent-forms" in rendered
    assert "881e0654e" in rendered
    assert "recruiter@acme.de" in rendered


def test_a_second_report_on_the_same_thread_comments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _calls(
        monkeypatch,
        [Reply(200, json.dumps({"issues": [{"key": "SPB-42"}]})), Reply(201, "{}")],
    )
    result = file_feedback(delivery(event={"eventId": "evt-2"}))
    assert result == {"status": "commented", "issue": "SPB-42"}
    method, path, _ = seen[1]
    assert (method, path) == ("POST", "/rest/api/3/issue/SPB-42/comment")


def test_an_unregistered_instance_is_dropped_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _calls(monkeypatch, [])
    body = delivery()
    body["instance"] = {"org": "someone-else", "project": "theirs"}
    assert file_feedback(body)["status"] == "ignored"
    assert seen == []


def test_a_failed_search_raises_rather_than_filing_a_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _calls(monkeypatch, [Reply(503)])
    with pytest.raises(RuntimeError):
        file_feedback(delivery())


def test_a_failed_create_raises_so_the_sender_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    _calls(monkeypatch, [Reply(200, json.dumps({"issues": []})), Reply(500, "boom")])
    with pytest.raises(RuntimeError):
        file_feedback(delivery())


def test_falls_back_to_the_event_id_when_there_is_no_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _calls(
        monkeypatch,
        [Reply(200, json.dumps({"issues": []})), Reply(201, json.dumps({"key": "SPB-43"}))],
    )
    body = delivery()
    body["event"].pop("threadId")
    file_feedback(body)
    assert "loupfeed-evt-1" in seen[0][1]
