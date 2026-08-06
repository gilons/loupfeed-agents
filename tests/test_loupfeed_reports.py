"""Reading anchored reports: the release, the resolved line, and the first bad build.

What triage needs out of a loupfeed report is small and specific: which build
the reporter ran, which line failed in that build, and for a crash, the oldest
release it appears in, which brackets the change that introduced it.
"""

from __future__ import annotations

import json

import pytest

from agent import surfaces
from agent.tools import loupfeed_reports as lr

REGISTRY = [
    {
        "key": "acme-webapp",
        "repo": "acme/acme",
        "path_root": "apps/webapp",
        "loupfeed": {
            "api": "https://loupfeed.acme.dev",
            "org": "acme",
            "project": "acme",
            "token_env": "ACME_TOKEN",
        },
    },
    {"key": "acme-admin", "repo": "acme/admin"},
]


class _Resp:
    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _registry(tmp_path, monkeypatch):
    path = tmp_path / "surfaces.json"
    path.write_text(json.dumps(REGISTRY))
    surfaces._cache = None
    monkeypatch.setattr(surfaces, "SURFACES_FILE", str(path))
    monkeypatch.setenv("ACME_TOKEN", "t0ken")
    yield
    surfaces._cache = None


def test_feedback_report_carries_the_release_and_the_resolved_line(monkeypatch):
    body = {
        "threadId": "th_1",
        "resolvedSource": {"src": "app/routes/export.tsx", "line": 42},
        "events": [
            {
                "text": "Export gives me an empty file",
                "release": "acme-webapp@bad2222",
                "status": "open",
                "request": {"route": "/candidates/export"},
                "element": {"selector": "button.export"},
                "replay": {"replayId": "rp_9"},
                "breadcrumbs": [{"m": "click"}],
            },
            {"text": "again today", "release": "acme-webapp@bad1111"},
        ],
        "comments": [{"author": "Ada", "text": "looking"}],
    }
    captured = {}

    def _get(url, **kw):
        captured["url"] = url
        captured["auth"] = kw["headers"]["Authorization"]
        return _Resp(200, body)

    monkeypatch.setattr(lr.requests, "get", _get)
    out = lr.loupfeed_report("acme-webapp", "feedback", "th_1")

    assert out["release"] == "acme-webapp@bad2222"
    assert out["resolved_source"] == "app/routes/export.tsx:42"
    assert out["route"] == "/candidates/export"
    assert out["replay_id"] == "rp_9"
    assert out["occurrences"] == 2
    assert captured["url"] == "https://loupfeed.acme.dev/api/acme/acme/threads/th_1"
    assert captured["auth"] == "Bearer t0ken"


def test_crash_report_brackets_the_first_bad_release(monkeypatch):
    """Occurrences come back newest first, so the oldest one dates the regression."""
    body = {
        "groupId": "cg_1",
        "events": [
            {
                "receivedAt": "2026-08-05T10:00:00Z",
                "release": "acme-webapp@bad3333",
                "request": {"route": "/export"},
                "exception": {
                    "type": "TypeError",
                    "value": "rows is not iterable",
                    "mechanism": {"handled": False},
                    "stacktrace": {
                        "frames": [
                            {"filename": "vendor.js", "lineno": 9, "in_app": False},
                            {
                                "filename": "app/routes/export.tsx",
                                "lineno": 42,
                                "function": "buildRows",
                                "in_app": True,
                            },
                        ]
                    },
                },
            },
            {
                "receivedAt": "2026-08-01T09:00:00Z",
                "release": "acme-webapp@bad2222",
                "request": {"route": "/export"},
                "exception": {"type": "TypeError", "value": "rows is not iterable"},
            },
        ],
    }
    monkeypatch.setattr(lr.requests, "get", lambda url, **kw: _Resp(200, body))
    out = lr.loupfeed_report("acme-webapp", "crash", "cg_1")

    assert out["first_seen_release"] == "acme-webapp@bad2222"
    assert out["release"] == "acme-webapp@bad3333"
    assert out["releases"] == ["acme-webapp@bad2222", "acme-webapp@bad3333"]
    assert out["occurrences"] == 2
    # The product's own frame leads: the top frame is usually framework noise.
    assert out["frames"][0] == "app/routes/export.tsx:42 (buildRows)"
    assert out["exception"]["type"] == "TypeError"
    # vendor.js is a source-looking relative path, so this stack is blameable.
    assert out["frames_kind"] == "source"


def test_a_minified_stack_is_labelled_so_its_frames_are_not_blamed(monkeypatch):
    """Production web crashes carry bundle URLs, not source paths (seen live)."""
    body = {
        "groupId": "cg_2",
        "events": [
            {
                "receivedAt": "2026-08-06T18:33:26Z",
                "release": "acme-webapp@8792aa2",
                "exception": {
                    "type": "Error",
                    "value": "Script error.",
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": "https://app.acme.dev/assets/main-BLd9wxkg.js",
                                "lineno": 2,
                                "function": "o",
                                "in_app": True,
                            }
                        ]
                    },
                },
            }
        ],
    }
    monkeypatch.setattr(lr.requests, "get", lambda url, **kw: _Resp(200, body))
    out = lr.loupfeed_report("acme-webapp", "crash", "cg_2")
    assert out["frames_kind"] == "minified"
    # The raw frame is still reported: it is evidence, just not a source path.
    assert "main-BLd9wxkg.js:2" in out["frames"][0]


def test_find_reports_filters_by_wording(monkeypatch):
    def _get(url, **kw):
        if url.endswith("/threads"):
            return _Resp(
                200,
                {
                    "threads": [
                        {"threadId": "a", "text": "export is empty", "route": "/export"},
                        {"threadId": "b", "text": "login loops", "route": "/sign-in"},
                    ]
                },
            )
        return _Resp(200, {"crashes": [{"groupId": "c", "type": "TypeError", "value": "rows"}]})

    monkeypatch.setattr(lr.requests, "get", _get)
    out = lr.loupfeed_find_reports("acme-webapp", kind="all", contains="export")
    assert [row["id"] for row in out["feedback"]] == ["a"]
    assert out["crashes"] == []


def test_a_surface_without_an_instance_says_so(monkeypatch):
    out = lr.loupfeed_report("acme-admin", "crash", "cg_1")
    assert out["ok"] is False
    assert "no loupfeed instance" in out["error"]


def test_a_missing_token_names_the_variable_but_not_a_value(monkeypatch):
    monkeypatch.delenv("ACME_TOKEN", raising=False)
    out = lr.loupfeed_report("acme-webapp", "crash", "cg_1")
    assert out["ok"] is False
    assert "ACME_TOKEN" in out["error"]


def test_unknown_surface_is_refused(monkeypatch):
    out = lr.loupfeed_report("nope", "crash", "cg_1")
    assert out["ok"] is False
    assert "unknown surface" in out["error"]


def test_instance_errors_do_not_leak_status_codes_as_findings(monkeypatch):
    monkeypatch.setattr(lr.requests, "get", lambda url, **kw: _Resp(500, {}))
    out = lr.loupfeed_report("acme-webapp", "feedback", "th_1")
    assert out["ok"] is False
    assert "refused the read" in out["error"]


# --- surfaces reporting into more than one instance ------------------------

MULTI = [
    {
        "key": "acme-webapp",
        "repo": "acme/acme",
        "loupfeed": [
            {
                "name": "production",
                "api": "https://prod.acme.dev",
                "org": "p",
                "project": "p",
                "token_env": "ACME_TOKEN",
            },
            {
                "name": "stages",
                "api": "https://dev.acme.dev",
                "org": "d",
                "project": "d",
                "token_env": "ACME_TOKEN",
            },
        ],
    }
]


@pytest.fixture
def multi_instance(tmp_path, monkeypatch):
    path = tmp_path / "multi.json"
    path.write_text(json.dumps(MULTI))
    surfaces._cache = None
    monkeypatch.setattr(surfaces, "SURFACES_FILE", str(path))
    monkeypatch.setenv("ACME_TOKEN", "t0ken")
    yield
    surfaces._cache = None


def test_a_report_missing_from_production_is_looked_for_in_the_stage_instance(
    multi_instance, monkeypatch
):
    """Both instances share a release prefix, so one surface covers both."""
    seen = []

    def _get(url, **kw):
        seen.append(url)
        if url.startswith("https://prod"):
            return _Resp(404, {"error": "not found"})
        return _Resp(200, {"groupId": "cg_1", "events": [{"receivedAt": "t", "release": "r"}]})

    monkeypatch.setattr(lr.requests, "get", _get)
    out = lr.loupfeed_report("acme-webapp", "crash", "cg_1")
    assert out["ok"] is True
    assert out["instance"] == "stages"
    assert len(seen) == 2


def test_a_search_covers_every_instance(multi_instance, monkeypatch):
    def _get(url, **kw):
        which = "prod" if url.startswith("https://prod") else "dev"
        return _Resp(200, {"crashes": [{"groupId": which, "type": "TypeError", "value": "rows"}]})

    monkeypatch.setattr(lr.requests, "get", _get)
    out = lr.loupfeed_find_reports("acme-webapp", kind="crash")
    assert sorted(row["id"] for row in out["crashes"]) == ["dev", "prod"]
    assert {row["instance"] for row in out["crashes"]} == {"production", "stages"}


def test_a_partial_search_says_it_was_partial(multi_instance, monkeypatch):
    """ "Nothing found" and "nothing found where I could look" are different answers."""

    def _get(url, **kw):
        if url.startswith("https://prod"):
            return _Resp(200, {"crashes": []})
        return _Resp(503, {})

    monkeypatch.setattr(lr.requests, "get", _get)
    out = lr.loupfeed_find_reports("acme-webapp", kind="crash")
    assert out["crashes"] == []
    assert out["incomplete"]
    assert "stages" in out["incomplete"][0]
