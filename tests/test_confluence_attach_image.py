"""Tests for copying a Teams image onto a Confluence page."""

from __future__ import annotations

from importlib import import_module

import pytest

mod = import_module("agent.tools.confluence_attach_image")

PAGE = "278331393"
HOSTED = "https://graph.microsoft.com/v1.0/chats/19:abc@thread.v2/messages/17/hostedContents/xyz"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 512


class _Resp:
    def __init__(self, status: int, content: bytes = b"", headers: dict | None = None, text=""):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self.text = text


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setenv("ATLASSIAN_EMAIL", "bot@dino-lab.io")
    monkeypatch.setenv("ATLASSIAN_API_TOKEN", "token")
    monkeypatch.setattr(mod, "get_graph_app_token", lambda: "graph-token")


def test_requires_page_and_image():
    assert mod.confluence_attach_image("", HOSTED)["ok"] is False
    assert mod.confluence_attach_image(PAGE, "")["ok"] is False


def test_missing_atlassian_credentials_is_plain(monkeypatch):
    monkeypatch.delenv("ATLASSIAN_API_TOKEN", raising=False)
    result = mod.confluence_attach_image(PAGE, HOSTED)
    assert result["ok"] is False
    assert "ATLASSIAN" not in result["reason"]


def test_uploads_and_returns_embed_markup(monkeypatch):
    seen: dict[str, object] = {}

    def _get(url, headers=None, timeout=None):
        seen["get_url"] = url
        return _Resp(200, PNG, {"Content-Type": "image/png"})

    def _post(url, headers=None, auth=None, files=None, timeout=None):
        seen["post_url"] = url
        seen["filename"] = files["file"][0]
        seen["nocheck"] = headers.get("X-Atlassian-Token")
        return _Resp(200, b"{}")

    monkeypatch.setattr(mod.requests, "get", _get)
    monkeypatch.setattr(mod.requests, "post", _post)

    result = mod.confluence_attach_image(PAGE, HOSTED, filename="standup screenshot")
    assert result["ok"] is True
    assert seen["get_url"].endswith("/$value"), seen["get_url"]
    assert f"/content/{PAGE}/child/attachment" in seen["post_url"]
    assert seen["nocheck"] == "no-check"  # Confluence rejects uploads without it
    assert result["filename"] == "standup-screenshot.png"
    assert 'ri:filename="standup-screenshot.png"' in result["embed_markup"]


def test_non_image_content_is_refused(monkeypatch):
    monkeypatch.setattr(
        mod.requests, "get", lambda *a, **k: _Resp(200, b"<html>", {"Content-Type": "text/html"})
    )
    result = mod.confluence_attach_image(PAGE, HOSTED)
    assert result["ok"] is False
    assert "isn't an image" in result["reason"]


def test_graph_failure_reason_has_no_status_code(monkeypatch):
    monkeypatch.setattr(mod.requests, "get", lambda *a, **k: _Resp(403, b"", {}, "Forbidden"))
    result = mod.confluence_attach_image(PAGE, HOSTED)
    assert result["ok"] is False
    assert "403" not in result["reason"]
