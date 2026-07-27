"""Tests for the read-only Microsoft Graph tools and the Teams Graph context."""

from __future__ import annotations

import importlib
import io
import json

import pytest

from agent.teams_adapter import _graph_context
from agent.tools.graph_api import graph_api, graph_file_content, graph_meeting_transcript

graph_api_module = importlib.import_module("agent.tools.graph_api")


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else (json.dumps(payload) if payload else "")
        self.content = self.text.encode()

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def _graph_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(graph_api_module, "get_graph_app_token", lambda: "tok")


def test_graph_api_rejects_relative_path() -> None:
    result = graph_api("chats/1/messages")
    assert result["status"] == 0
    assert "must start with '/'" in result["body"]


def test_graph_api_rejects_disallowed_root(_graph_token) -> None:
    result = graph_api("/applications/abc")
    assert result["status"] == 0
    assert "not allowed" in result["body"]


def test_graph_api_reports_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph_api_module, "get_graph_app_token", lambda: None)
    result = graph_api("/chats/1/messages")
    assert result["status"] == 0
    assert "not configured" in result["body"]


def test_graph_api_get_success(_graph_token, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.update(url=url, headers=headers, params=params)
        return _FakeResponse(payload={"value": [{"id": "m1"}]})

    monkeypatch.setattr(graph_api_module.requests, "get", fake_get)
    result = graph_api("/chats/19:abc@thread.v2/messages", params={"$top": 5})
    assert result == {"status": 200, "body": {"value": [{"id": "m1"}]}}
    assert seen["url"].startswith("https://graph.microsoft.com/v1.0/chats/")
    assert seen["params"] == {"$top": 5}
    assert seen["headers"]["Authorization"] == "Bearer tok"


def test_graph_api_truncates_large_bodies(_graph_token, monkeypatch: pytest.MonkeyPatch) -> None:
    big = {"blob": "x" * (graph_api_module._MAX_RESPONSE_CHARS + 10)}
    monkeypatch.setattr(
        graph_api_module.requests, "get", lambda *a, **k: _FakeResponse(payload=big)
    )
    result = graph_api("/chats/1/messages")
    assert result["truncated"] is True
    assert len(result["body"]) == graph_api_module._MAX_RESPONSE_CHARS


def test_meeting_transcript_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = {
        "/chats/19:meeting_x@thread.v2": _FakeResponse(
            payload={
                "topic": "Sprint sync",
                "onlineMeetingInfo": {
                    "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/xyz",
                    "organizer": {"id": "org-1"},
                },
            }
        ),
        "/users/org-1/onlineMeetings": _FakeResponse(
            payload={"value": [{"id": "mtg-1", "subject": "Sprint sync"}]}
        ),
        "/users/org-1/onlineMeetings/mtg-1/transcripts": _FakeResponse(
            payload={
                "value": [
                    {"id": "t-old", "createdDateTime": "2026-07-27T09:00:00Z"},
                    {"id": "t-new", "createdDateTime": "2026-07-27T10:00:00Z"},
                ]
            }
        ),
        "/users/org-1/onlineMeetings/mtg-1/transcripts/t-new/content": _FakeResponse(
            text="WEBVTT\n\nGiles: let's file this as an SPI idea"
        ),
    }

    monkeypatch.setattr(graph_api_module, "_get", lambda path, params=None: responses[path])
    result = graph_meeting_transcript("19:meeting_x@thread.v2")
    assert result["ok"] is True
    assert result["subject"] == "Sprint sync"
    assert "SPI idea" in result["transcript"]
    assert result["created"] == "2026-07-27T10:00:00Z"


def test_meeting_transcript_no_meeting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        graph_api_module,
        "_get",
        lambda path, params=None: _FakeResponse(payload={"topic": "just a chat"}),
    )
    result = graph_meeting_transcript("19:plainchat@thread.v2")
    assert result["ok"] is False
    assert "no online meeting" in result["reason"]


def test_graph_context_meeting_chat() -> None:
    activity = {
        "conversation": {
            "id": "19:meeting_abc@thread.v2",
            "conversationType": "groupChat",
            "tenantId": "tenant-1",
        },
        "channelData": {"tenant": {"id": "tenant-1"}, "meeting": {"id": "meet-1"}},
    }
    context = _graph_context(activity)
    assert context["teams_conversation_id"] == "19:meeting_abc@thread.v2"
    assert context["teams_conversation_type"] == "groupChat"
    assert context["teams_tenant_id"] == "tenant-1"
    assert context["teams_meeting_id"] == "meet-1"


def test_graph_context_channel_thread_strips_messageid() -> None:
    activity = {
        "conversation": {
            "id": "19:chan@thread.tacv2;messageid=1690000000000",
            "conversationType": "channel",
        },
        "channelData": {
            "tenant": {"id": "tenant-1"},
            "team": {"aadGroupId": "group-1"},
        },
    }
    context = _graph_context(activity)
    assert context["teams_conversation_id"] == "19:chan@thread.tacv2"
    assert context["teams_team_group_id"] == "group-1"
    assert "teams_meeting_id" not in context


# --- graph_file_content -----------------------------------------------------


def _make_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (i, obj))
    xref_pos = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objs) + 1))
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref_pos)
    )
    return out.getvalue()


def _make_docx(paragraphs: list[str]) -> bytes:
    from docx import Document

    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class _FakeBinaryResponse:
    def __init__(self, status_code: int = 200, payload=None, content: bytes = b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content or (json.dumps(payload).encode() if payload else b"")
        self.text = self.content.decode("utf-8", errors="replace")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _drive_routes(item: dict, content: bytes):
    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/content"):
            return _FakeBinaryResponse(content=content)
        if "/shares/" in url or "/items/" in url:
            return _FakeBinaryResponse(payload=item)
        raise AssertionError(f"unexpected url {url}")

    return fake_get


def test_graph_file_content_requires_a_target(_graph_token) -> None:
    result = graph_file_content()
    assert result["ok"] is False


def test_graph_file_content_pdf_via_share_url(_graph_token, monkeypatch) -> None:
    item = {
        "id": "item1",
        "name": "notes.pdf",
        "size": 1234,
        "file": {"mimeType": "application/pdf"},
        "parentReference": {"driveId": "d1"},
    }
    pdf = _make_pdf("Roadmap idea: faster onboarding")
    monkeypatch.setattr(graph_api_module.requests, "get", _drive_routes(item, pdf))
    result = graph_file_content(url="https://tenant.sharepoint.com/sites/x/notes.pdf")
    assert result["ok"] is True
    assert result["kind"] == "pdf"
    assert "Roadmap idea: faster onboarding" in result["text"]


def test_graph_file_content_docx_by_ids(_graph_token, monkeypatch) -> None:
    item = {
        "id": "item2",
        "name": "spec.docx",
        "size": 5678,
        "file": {
            "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        },
    }
    payload = _make_docx(["SPI: candidate sheet next buttons", "Owner: Sam"])
    monkeypatch.setattr(graph_api_module.requests, "get", _drive_routes(item, payload))
    result = graph_file_content(drive_id="d1", item_id="item2")
    assert result["ok"] is True
    assert result["kind"] == "docx"
    assert "SPI: candidate sheet next buttons" in result["text"]
    assert "Owner: Sam" in result["text"]


def test_graph_file_content_plain_text(_graph_token, monkeypatch) -> None:
    item = {"id": "i", "name": "ideas.md", "size": 10, "file": {"mimeType": "text/markdown"}}
    monkeypatch.setattr(
        graph_api_module.requests, "get", _drive_routes(item, b"- idea one\n- idea two")
    )
    result = graph_file_content(drive_id="d1", item_id="i")
    assert result["ok"] is True
    assert result["kind"] == "text"
    assert "idea two" in result["text"]


def test_graph_file_content_unsupported_type(_graph_token, monkeypatch) -> None:
    item = {"id": "i", "name": "deck.pptx", "size": 10, "file": {"mimeType": "application/zip"}}
    monkeypatch.setattr(graph_api_module.requests, "get", _drive_routes(item, b"PK\x03\x04"))
    result = graph_file_content(drive_id="d1", item_id="i")
    assert result["ok"] is False
    assert "unsupported" in result["reason"]


def test_graph_file_content_denied_hint(_graph_token, monkeypatch) -> None:
    monkeypatch.setattr(
        graph_api_module.requests,
        "get",
        lambda *a, **k: _FakeBinaryResponse(status_code=403, payload={"error": "accessDenied"}),
    )
    result = graph_file_content(url="https://onedrive.example/personal/x.docx")
    assert result["ok"] is False
    assert "403" in result["reason"]


def test_graph_file_content_too_large(_graph_token, monkeypatch) -> None:
    item = {
        "id": "i",
        "name": "big.pdf",
        "size": graph_api_module._MAX_FILE_BYTES + 1,
        "file": {"mimeType": "application/pdf"},
    }
    monkeypatch.setattr(graph_api_module.requests, "get", _drive_routes(item, b""))
    result = graph_file_content(drive_id="d1", item_id="i")
    assert result["ok"] is False
    assert "too large" in result["reason"]
