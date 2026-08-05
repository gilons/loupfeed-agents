"""Filing tool: prefixed date chains, space-unique titles, clean failures.

Folder titles are unique per space in Confluence, so every level of a date
tree must carry the purpose slug (``standups 2026-08-05``); a bare ``2026``
collides with the GitHub Discussions tree that already owns that title.
"""

from __future__ import annotations

import urllib.parse
from unittest.mock import MagicMock, patch

from agent.tools.confluence_file_capture import confluence_file_capture


def test_rejects_bad_date():
    out = confluence_file_capture("1", "Standups", "yesterday")
    assert out["ok"] is False
    assert "2026-08-05" in out["reason"]


def test_no_credentials_is_a_plain_message():
    with patch.dict("os.environ", {}, clear=True):
        out = confluence_file_capture("1", "Standups", "2026-08-05")
    assert out["ok"] is False
    assert "ATLASSIAN" not in out["reason"]
    assert "access" in out["reason"]


def _resp(status=200, json_body=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_body or {}
    r.text = text
    return r


@patch.dict(
    "os.environ",
    {"ATLASSIAN_EMAIL": "bot@dino-lab.io", "ATLASSIAN_API_TOKEN": "t"},
)
def test_files_page_through_prefixed_chain():
    calls = {"created": [], "moved": []}

    def fake_get(url, **kw):
        if "/wiki/api/v2/spaces?" in url:
            return _resp(json_body={"results": [{"id": "274399234"}]})
        # folder searches: purpose folder exists, date folders do not
        if 'title="Standups"' in urllib.parse.unquote(url):
            return _resp(json_body={"results": [{"id": "274300930", "title": "Standups"}]})
        return _resp(json_body={"results": []})

    def fake_post(url, json=None, **kw):
        calls["created"].append(json["title"])
        return _resp(json_body={"id": f"id-{json['title']}"})

    def fake_put(url, **kw):
        calls["moved"].append(url)
        return _resp()

    with (
        patch("agent.tools.confluence_file_capture.requests.get", side_effect=fake_get),
        patch("agent.tools.confluence_file_capture.requests.post", side_effect=fake_post),
        patch("agent.tools.confluence_file_capture.requests.put", side_effect=fake_put),
    ):
        out = confluence_file_capture("286261250", "Standups", "2026-08-05")

    assert out == {"ok": True, "filed_under": "Standups/standups 2026-08-05"}
    assert calls["created"] == ["standups 2026", "standups 2026-08", "standups 2026-08-05"]
    assert calls["moved"] and "move/append/id-standups 2026-08-05" in calls["moved"][0]


@patch.dict(
    "os.environ",
    {"ATLASSIAN_EMAIL": "bot@dino-lab.io", "ATLASSIAN_API_TOKEN": "t"},
)
def test_title_collision_falls_back_to_lookup():
    """A 400 duplicate-title response resolves to the existing folder."""

    def fake_get(url, **kw):
        if "/wiki/api/v2/spaces?" in url:
            return _resp(json_body={"results": [{"id": "274399234"}]})
        return _resp(json_body={"results": [{"id": "existing", "title": "x"}]})

    with (
        patch("agent.tools.confluence_file_capture.requests.get", side_effect=fake_get),
        patch(
            "agent.tools.confluence_file_capture.requests.post",
            return_value=_resp(400, text="A folder exists with the same title in this space"),
        ),
        patch("agent.tools.confluence_file_capture.requests.put", return_value=_resp()),
    ):
        out = confluence_file_capture("1", "Standups", "2026-08-05")
    assert out["ok"] is True
