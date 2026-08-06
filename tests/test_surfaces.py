"""Surface registry: release parsing, lookups, and the monorepo path join.

A loupfeed release is ``<surface>@<commit>``, and the whole triage chain hangs
off reading it correctly: the wrong commit half blames the wrong sha, and a
build that cannot be pinned at all must be recognised rather than guessed at.
"""

from __future__ import annotations

import json

import pytest

from agent import surfaces

REGISTRY = [
    {
        "key": "acme-webapp",
        "repo": "acme/acme",
        "path_root": "apps/webapp",
        "jira_projects": ["BUG", "SUP"],
        "loupfeed": {
            "api": "https://loupfeed.acme.dev/",
            "org": "acme",
            "project": "acme",
            "token_env": "ACME_TOKEN",
        },
    },
    {"key": "acme-admin", "repo": "acme/admin", "jira_projects": ["ADM"]},
]


@pytest.fixture
def registry_file(tmp_path):
    path = tmp_path / "surfaces.json"
    path.write_text(json.dumps(REGISTRY))
    surfaces._cache = None
    yield str(path)
    surfaces._cache = None


def test_release_splits_into_surface_and_commit():
    release = surfaces.parse_release("acme-webapp@0f1e2d3c4b5a")
    assert release.surface_key == "acme-webapp"
    assert release.commit == "0f1e2d3c4b5a"
    assert release.pinnable is True


def test_a_dirty_build_is_not_pinnable():
    """A build from a modified tree matches no commit, so its lines cannot be blamed."""
    release = surfaces.parse_release("acme-webapp@0f1e2d3-dirty")
    assert release.dirty is True
    assert release.commit == "0f1e2d3"
    assert release.pinnable is False
    assert "modified working tree" in release.why_not_pinnable()


@pytest.mark.parametrize("raw", ["dev", "unknown", "", None])
def test_releases_without_a_commit_are_not_pinnable(raw):
    release = surfaces.parse_release(raw)
    assert release.pinnable is False
    assert release.why_not_pinnable()


def test_surface_resolves_from_a_release(registry_file):
    surface, release = surfaces.surface_for_release("acme-webapp@abc1234", registry_file)
    assert surface is not None
    assert surface["repo"] == "acme/acme"
    assert release.commit == "abc1234"


def test_unknown_surface_is_not_invented(registry_file):
    surface, release = surfaces.surface_for_release("other-app@abc1234", registry_file)
    assert surface is None
    assert release.commit == "abc1234"


def test_jira_project_maps_to_its_surface(registry_file):
    assert surfaces.surface_for_issue("BUG-42", registry_file)["key"] == "acme-webapp"
    assert surfaces.surface_for_issue("adm-7", registry_file)["key"] == "acme-admin"
    # A project nobody claimed must not be triaged as if it were a bug project.
    assert surfaces.surface_for_issue("IDEA-1", registry_file) is None


def test_manifest_paths_are_joined_onto_the_build_root(registry_file):
    """Manifest paths are relative to the build root, not the repository."""
    surface = surfaces.surface_for_key("acme-webapp", registry_file)
    assert surfaces.repo_path(surface, "app/routes/export.tsx") == (
        "apps/webapp/app/routes/export.tsx"
    )
    # Already rooted, and a surface without a build root: both left alone.
    assert surfaces.repo_path(surface, "apps/webapp/app/x.tsx") == "apps/webapp/app/x.tsx"
    admin = surfaces.surface_for_key("acme-admin", registry_file)
    assert surfaces.repo_path(admin, "src/main.ts") == "src/main.ts"


def test_instance_token_comes_from_the_named_env_var(registry_file, monkeypatch):
    monkeypatch.setenv("ACME_TOKEN", "t0ken")
    surface = surfaces.surface_for_key("acme-webapp", registry_file)
    target = surfaces.loupfeed_target(surface)
    assert target["api"] == "https://loupfeed.acme.dev"
    assert target["token"] == "t0ken"
    # No loupfeed block at all: no reports for that surface, and no crash.
    assert surfaces.loupfeed_target(surfaces.surface_for_key("acme-admin", registry_file)) is None


def test_a_missing_registry_is_empty_not_fatal(tmp_path):
    surfaces._cache = None
    assert surfaces.load_surfaces(str(tmp_path / "nope.json")) == []
    assert "no surfaces configured" in surfaces.registry_summary(str(tmp_path / "nope.json"))


def test_registry_summary_lists_what_triage_can_reach(registry_file, monkeypatch):
    monkeypatch.setenv("ACME_TOKEN", "t0ken")
    summary = surfaces.registry_summary(registry_file)
    assert "acme-webapp" in summary and "acme/acme" in summary
    assert "BUG" in summary
