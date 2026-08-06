"""Commit pinning: blame at the reported commit, and read before you accuse.

The two failure modes these tests exist for are the ones that produce a
confident wrong answer: blaming a line at ``main`` when the line number came
from an older build, and naming a suspect commit without reading its diff.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.tools import git_archaeology as ga

CONFIG = {"configurable": {"triage_github_tokens": {"acme/acme": "t0ken"}}}


class _Resp:
    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _run_config():
    with patch("agent.tools.git_archaeology.get_config", return_value=CONFIG):
        yield


def _blame_payload(start: int, end: int, sha: str = "deadbeef"):
    return {
        "data": {
            "repository": {
                "object": {
                    "blame": {
                        "ranges": [
                            {
                                "startingLine": start,
                                "endingLine": end,
                                "commit": {
                                    "oid": sha,
                                    "messageHeadline": "narrow the export filter",
                                    "committedDate": "2026-07-30T10:00:00Z",
                                    "author": {"name": "Ada", "email": "ada@acme.dev"},
                                },
                            }
                        ]
                    }
                }
            }
        }
    }


def test_blame_names_the_commit_owning_the_reported_line():
    with patch(
        "agent.tools.git_archaeology.requests.post", return_value=_Resp(200, _blame_payload(40, 44))
    ) as post:
        out = ga.git_blame_line("acme/acme", "apps/webapp/app/x.tsx", 42, "abc1234")
    assert out["ok"] is True
    assert out["commit"]["sha"] == "deadbeef"
    assert out["commit"]["author"] == "Ada"
    # The ref must reach GitHub verbatim: blaming at anything else is the bug.
    assert post.call_args.kwargs["json"]["variables"]["ref"] == "abc1234"


def test_blame_without_a_ref_is_refused():
    """No default ref: the release's commit is the only correct one to blame at."""
    out = ga.git_blame_line("acme/acme", "apps/webapp/app/x.tsx", 42, "")
    assert out["ok"] is False
    assert "not at main" in out["error"]


def test_a_line_past_the_end_of_the_file_is_not_blamed_on_the_nearest_commit():
    with patch(
        "agent.tools.git_archaeology.requests.post", return_value=_Resp(200, _blame_payload(1, 10))
    ):
        out = ga.git_blame_line("acme/acme", "apps/webapp/app/x.tsx", 999, "abc1234")
    assert out["ok"] is False
    assert "past the end" in out["error"]


def test_missing_token_is_reported_not_guessed_around():
    with patch("agent.tools.git_archaeology.get_config", return_value={"configurable": {}}):
        out = ga.git_blame_line("acme/acme", "a.ts", 1, "abc")
    assert out["ok"] is False
    assert "no GitHub token" in out["error"]


def test_commits_touching_passes_the_window_through():
    with patch(
        "agent.tools.git_archaeology.requests.get",
        return_value=_Resp(
            200,
            [
                {
                    "sha": "f00",
                    "html_url": "https://github.com/acme/acme/commit/f00",
                    "commit": {
                        "message": "rework export\n\nbody",
                        "author": {"name": "Ada", "date": "2026-07-30T10:00:00Z"},
                    },
                }
            ],
        ),
    ) as get:
        out = ga.git_commits_touching(
            "acme/acme", "apps/webapp/app/x.tsx", since="2026-07-01T00:00:00Z", max_results=5
        )
    assert out["commits"][0] == {
        "sha": "f00",
        "author": "Ada",
        "date": "2026-07-30T10:00:00Z",
        "headline": "rework export",
        "url": "https://github.com/acme/acme/commit/f00",
    }
    params = get.call_args.kwargs["params"]
    assert params["since"] == "2026-07-01T00:00:00Z"
    assert params["path"] == "apps/webapp/app/x.tsx"


def test_commit_diff_returns_the_patch_so_a_suspect_can_be_read():
    payload = {
        "sha": "f00",
        "html_url": "u",
        "commit": {"message": "rework export", "author": {"name": "Ada", "date": "d"}},
        "files": [
            {
                "filename": "apps/webapp/app/x.tsx",
                "status": "modified",
                "additions": 3,
                "deletions": 1,
                "patch": "@@ -40,4 +40,6 @@\n-  if (a) {\n+  if (a && b) {",
            },
            {"filename": "other.ts", "status": "modified", "patch": "@@ -1 +1 @@"},
        ],
    }
    with patch("agent.tools.git_archaeology.requests.get", return_value=_Resp(200, payload)):
        out = ga.git_commit_diff("acme/acme", "f00", path="apps/webapp/app/x.tsx")
    assert out["ok"] is True
    assert len(out["files"]) == 1
    assert "if (a && b)" in out["files"][0]["patch"]


def test_compare_gives_the_introduced_in_window():
    payload = {
        "status": "ahead",
        "ahead_by": 2,
        "commits": [
            {"sha": "a1", "commit": {"message": "one", "author": {"name": "A", "date": "d"}}},
            {"sha": "b2", "commit": {"message": "two", "author": {"name": "B", "date": "d"}}},
        ],
        "files": [
            {"filename": "apps/webapp/app/x.tsx", "status": "modified"},
            {"filename": "docs/readme.md", "status": "modified"},
        ],
    }
    with patch("agent.tools.git_archaeology.requests.get", return_value=_Resp(200, payload)) as get:
        out = ga.git_compare("acme/acme", "good1", "bad2", path_prefix="apps/webapp/")
    assert [c["sha"] for c in out["commits"]] == ["a1", "b2"]
    assert [f["path"] for f in out["files"]] == ["apps/webapp/app/x.tsx"]
    assert "compare/good1...bad2" in get.call_args.args[0]


def test_graphql_errors_surface_as_a_clean_failure():
    with patch(
        "agent.tools.git_archaeology.requests.post",
        return_value=_Resp(200, {"errors": [{"message": "Could not resolve to a Repository"}]}),
    ):
        out = ga.git_blame_line("acme/acme", "a.ts", 1, "abc")
    assert out["ok"] is False
    assert "Could not resolve" in out["error"]
