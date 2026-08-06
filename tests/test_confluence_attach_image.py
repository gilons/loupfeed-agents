"""Tests for copying a Teams image onto a Confluence page."""

from __future__ import annotations

import base64
from importlib import import_module
from unittest.mock import MagicMock, patch

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


def _png(size: int = 64) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"x" * size


def _graph_ok(data: bytes):
    r = MagicMock()
    r.status_code = 200
    r.content = data
    r.headers = {"Content-Type": "image/png"}
    return r


APP_ENV = {
    "ATLASSIAN_APP_ATTACH_URL": "https://app.example/x1/attach",
    "ATLASSIAN_APP_SHARED_SECRET": "s3cret",
    "ATLASSIAN_EMAIL": "a@b.c",
    "ATLASSIAN_API_TOKEN": "t",
    "ATLASSIAN_AUTH_PREFERENCE": "auto",
}


@patch("agent.tools.confluence_attach_image.get_graph_app_token", return_value="tok")
def test_small_images_are_uploaded_by_the_app(_tok):
    """The app owns attachments, so the deployment needs no credential."""
    posted = []

    class _Ok:
        status_code = 200
        text = "{}"

    def _post(url, **kw):
        posted.append((url, kw.get("json"), kw.get("headers", {})))
        return _Ok()

    with (
        patch.dict("os.environ", APP_ENV, clear=False),
        patch("agent.tools.confluence_attach_image.requests.get", return_value=_graph_ok(_png())),
        patch("agent.tools.confluence_attach_image.requests.post", side_effect=_post),
    ):
        out = mod.confluence_attach_image(
            "123", "https://graph/x/hostedContents/1/$value", "shot.png"
        )

    assert out["ok"] is True and out["embed_markup"].startswith("<ac:image>")
    assert len(posted) == 1, "only the app is called; Atlassian is never touched directly"
    url, body, headers = posted[0]
    assert url == "https://app.example/x1/attach"
    assert headers["X-Loupfeed-Secret"] == "s3cret"
    assert body["pageId"] == "123" and body["filename"] == "shot.png"
    assert base64.b64decode(body["dataBase64"]).startswith(b"\x89PNG")


@patch("agent.tools.confluence_attach_image.get_graph_app_token", return_value="tok")
def test_large_images_stay_on_the_deployment_credential(_tok):
    """Forge caps web-trigger payloads, so big files must not silently fail."""
    urls = []

    class _Ok:
        status_code = 200
        text = "{}"

    def _post(url, **kw):
        urls.append(url)
        return _Ok()

    big = _png(700 * 1024)
    with (
        patch.dict("os.environ", APP_ENV, clear=False),
        patch("agent.tools.confluence_attach_image.requests.get", return_value=_graph_ok(big)),
        patch("agent.tools.confluence_attach_image.requests.post", side_effect=_post),
    ):
        out = mod.confluence_attach_image(
            "123", "https://graph/x/hostedContents/1/$value", "big.png"
        )

    assert out["ok"] is True
    assert urls == ["https://dinolabgmbh.atlassian.net/wiki/rest/api/content/123/child/attachment"]


@patch("agent.tools.confluence_attach_image.get_graph_app_token", return_value="tok")
def test_service_account_preference_skips_the_app(_tok):
    urls = []

    class _Ok:
        status_code = 200
        text = "{}"

    def _post(url, **kw):
        urls.append(url)
        return _Ok()

    env = {**APP_ENV, "ATLASSIAN_AUTH_PREFERENCE": "service_account"}
    with (
        patch.dict("os.environ", env, clear=False),
        patch("agent.tools.confluence_attach_image.requests.get", return_value=_graph_ok(_png())),
        patch("agent.tools.confluence_attach_image.requests.post", side_effect=_post),
    ):
        mod.confluence_attach_image("123", "https://graph/x/hostedContents/1/$value", "shot.png")
    assert "app.example" not in urls[0]
