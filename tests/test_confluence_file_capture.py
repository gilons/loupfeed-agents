"""Filing tool: prefixed date chains, space-unique titles, clean failures.

Folder titles are unique per space in Confluence, so every level of a date
tree must carry the purpose slug (``standups 2026-08-05``); a bare ``2026``
collides with the GitHub Discussions tree that already owns that title.

All requests go through ``utils.atlassian_api``, so these tests drive that
seam rather than HTTP: writes must be marked attributed so the entry app
owns them, while lookups stay unattributed.
"""

from __future__ import annotations

import json as jsonlib
import urllib.parse
from unittest.mock import patch

from agent.tools.confluence_file_capture import confluence_file_capture


class _Resp:
    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self.text = jsonlib.dumps(payload) if not isinstance(payload, str) else payload

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self):
        return jsonlib.loads(self.text)


def test_rejects_bad_date():
    out = confluence_file_capture("1", "Standups", "yesterday")
    assert out["ok"] is False
    assert "2026-08-05" in out["reason"]


def test_missing_space_reports_plainly_without_internals():
    with patch(
        "agent.tools.confluence_file_capture.atlassian_request",
        return_value=_Resp(503, ""),
    ):
        out = confluence_file_capture("1", "Standups", "2026-08-05")
    assert out["ok"] is False
    assert "SPRAW" in out["reason"]
    assert "503" not in out["reason"]


def test_files_page_through_prefixed_chain_marking_writes_attributed():
    calls: list[dict] = []

    def _request(product, method, path, body=None, *, attributed=False):
        calls.append({"method": method, "path": path, "body": body, "attributed": attributed})
        if "/spaces?" in path:
            return _Resp(200, {"results": [{"id": "274399234"}]})
        if "content/search" in path:
            decoded = urllib.parse.unquote(path)
            if 'title="Standups"' in decoded:
                return _Resp(200, {"results": [{"id": "274300930"}]})
            return _Resp(200, {"results": []})
        if path == "/wiki/api/v2/folders":
            return _Resp(200, {"id": f"id-{body['title']}"})
        return _Resp(200, {"pageId": "1"})

    with patch("agent.tools.confluence_file_capture.atlassian_request", side_effect=_request):
        out = confluence_file_capture("286261250", "Standups", "2026-08-05")

    assert out == {"ok": True, "filed_under": "Standups/standups 2026-08-05"}

    created = [c["body"]["title"] for c in calls if c["path"] == "/wiki/api/v2/folders"]
    assert created == ["standups 2026", "standups 2026-08", "standups 2026-08-05"]

    move = next(c for c in calls if c["method"] == "PUT")
    assert "move/append/id-standups 2026-08-05" in move["path"]

    # Writes are the app's to own; lookups need no identity.
    assert all(c["attributed"] for c in calls if c["method"] in ("POST", "PUT"))
    assert not any(c["attributed"] for c in calls if c["method"] == "GET")


def test_title_collision_falls_back_to_lookup():
    """A duplicate-title 400 resolves to the existing folder."""

    def _request(product, method, path, body=None, *, attributed=False):
        if "/spaces?" in path:
            return _Resp(200, {"results": [{"id": "274399234"}]})
        if "content/search" in path:
            return _Resp(200, {"results": [{"id": "existing"}]})
        if path == "/wiki/api/v2/folders":
            return _Resp(400, "A folder exists with the same title in this space")
        return _Resp(200, {})

    with patch("agent.tools.confluence_file_capture.atlassian_request", side_effect=_request):
        out = confluence_file_capture("1", "Standups", "2026-08-05")
    assert out["ok"] is True


def test_a_failed_move_is_reported_not_swallowed():
    def _request(product, method, path, body=None, *, attributed=False):
        if "/spaces?" in path:
            return _Resp(200, {"results": [{"id": "274399234"}]})
        if "content/search" in path:
            return _Resp(200, {"results": [{"id": "found"}]})
        if path == "/wiki/api/v2/folders":
            return _Resp(200, {"id": "folder"})
        return _Resp(403, "nope")

    with patch("agent.tools.confluence_file_capture.atlassian_request", side_effect=_request):
        out = confluence_file_capture("1", "Standups", "2026-08-05")
    assert out["ok"] is False
    assert "move the page" in out["reason"]
