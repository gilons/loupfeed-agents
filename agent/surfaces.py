"""Surface registry: from a report's release string to a repository and a commit.

Every loupfeed event carries a release, and a loupfeed release is
``<surface>@<commit>`` (``@loupfeed/build``'s build identity). So a report from
an instrumented build already names the exact tree the reporter was running:
the surface half says which app it was, the commit half says which sha built
it. Triage starts from that instead of guessing which repository is at fault.

The surface-to-repository mapping is deployment data, so it lives in a JSON
file (``LOUPFEED_SURFACES_FILE``), not in this module and not in the
environment: the platform stays free of any product's names, and a deployment
describes its own apps. Only credentials stay in the environment, named per
surface by ``token_env``.

One entry per built surface::

    [
      {
        "key": "acme-webapp",
        "repo": "acme/acme",
        "path_root": "apps/webapp",
        "jira_projects": ["BUG"],
        "loupfeed": {
          "api": "https://loupfeed.acme.dev",
          "org": "acme",
          "project": "acme",
          "token_env": "LOUPFEED_DASHBOARD_TOKEN"
        }
      }
    ]

``path_root`` is what makes monorepos work: the id-to-source manifest records
paths relative to the *build* root, not to the repository, so a manifest entry
``app/routes/x.tsx`` is ``apps/webapp/app/routes/x.tsx`` in git.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SURFACES_FILE = os.environ.get("LOUPFEED_SURFACES_FILE", "/etc/loupfeed/surfaces.json")

# Release strings that name no commit: a local build, or a build whose tree was
# dirty. Neither can be pinned, and pretending otherwise blames the wrong sha.
_UNPINNABLE = frozenset({"", "dev", "local", "unknown", "development"})

_DIRTY_SUFFIX = "-dirty"


@dataclass(frozen=True)
class Release:
    """A parsed loupfeed release string."""

    raw: str
    surface_key: str
    commit: str
    dirty: bool

    @property
    def pinnable(self) -> bool:
        """True when this release identifies a real commit to reason about."""
        return bool(self.commit) and not self.dirty and self.commit not in _UNPINNABLE

    def why_not_pinnable(self) -> str:
        if self.dirty:
            return (
                f"the build {self.raw!r} was made from a modified working tree "
                "(-dirty), so its line numbers match no commit"
            )
        if not self.commit or self.commit in _UNPINNABLE:
            return f"the release {self.raw!r} carries no commit sha"
        return ""


def parse_release(release: str | None) -> Release:
    """Split ``<surface>@<commit>`` into its parts, tolerating non-builds."""
    raw = (release or "").strip()
    surface_key, separator, commit = raw.rpartition("@")
    if not separator:
        # No '@' at all: 'dev' from a local run, or an app that sets a bare version.
        return Release(raw=raw, surface_key=raw, commit="", dirty=False)
    dirty = commit.endswith(_DIRTY_SUFFIX)
    if dirty:
        commit = commit[: -len(_DIRTY_SUFFIX)]
    return Release(raw=raw, surface_key=surface_key, commit=commit, dirty=dirty)


_cache: tuple[str, float, list[dict[str, Any]]] | None = None


def load_surfaces(path: str | None = None) -> list[dict[str, Any]]:
    """Read the registry. Missing or malformed file means "no surfaces known"."""
    global _cache
    file = path or SURFACES_FILE
    try:
        mtime = os.path.getmtime(file)
    except OSError:
        return []
    if _cache is not None and _cache[0] == file and _cache[1] == mtime:
        return _cache[2]
    try:
        raw = json.loads(Path(file).read_text())
    except (OSError, ValueError):
        logger.warning("surface registry unreadable: %s", file)
        return []
    entries = raw.get("surfaces") if isinstance(raw, dict) else raw
    surfaces = [
        entry
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict) and entry.get("key") and entry.get("repo")
    ]
    if not surfaces:
        logger.warning("surface registry has no usable entries: %s", file)
    _cache = (file, mtime, surfaces)
    return surfaces


def surface_for_key(key: str, path: str | None = None) -> dict[str, Any] | None:
    return next((s for s in load_surfaces(path) if s.get("key") == key), None)


def surface_for_release(
    release: str | None, path: str | None = None
) -> tuple[dict[str, Any] | None, Release]:
    """The surface a release belongs to, plus the parsed release itself."""
    parsed = parse_release(release)
    return surface_for_key(parsed.surface_key, path), parsed


def jira_project_of(issue_key: str | None) -> str:
    """``SPB-12`` -> ``SPB``."""
    key = (issue_key or "").strip()
    return key.split("-", 1)[0].upper() if "-" in key else key.upper()


def surface_for_jira_project(project_key: str, path: str | None = None) -> dict[str, Any] | None:
    wanted = (project_key or "").strip().upper()
    if not wanted:
        return None
    for surface in load_surfaces(path):
        projects = surface.get("jira_projects")
        if isinstance(projects, list) and wanted in {str(p).upper() for p in projects}:
            return surface
    return None


def surface_for_issue(issue_key: str | None, path: str | None = None) -> dict[str, Any] | None:
    return surface_for_jira_project(jira_project_of(issue_key), path)


def repo_owner_name(surface: dict[str, Any]) -> tuple[str, str]:
    """``{"repo": "owner/name"}`` -> ``("owner", "name")``."""
    owner, _, name = str(surface.get("repo") or "").partition("/")
    return owner, name


def is_source_path(src: str) -> bool:
    """Whether this location is a source path at all, rather than a served URL.

    Manifest entries are repository-relative source paths, but a web crash's
    stack frames are runtime URLs of minified bundles
    (``https://app.example.com/assets/main-BLd9wxkg.js:2``). Those name no file
    in any repository, so they must never be turned into one.
    """
    candidate = (src or "").strip()
    if not candidate or "://" in candidate or candidate.startswith("//"):
        return False
    # An absolute filesystem path is somebody's build machine, not the repo.
    return not candidate.startswith("/")


def repo_path(surface: dict[str, Any], src: str) -> str | None:
    """Turn a manifest source path into a repository path via ``path_root``.

    Returns None when ``src`` is not a source path (a bundle URL, an absolute
    path). Prefixing those with the build root invents a file that does not
    exist, which then gets blamed, searched for, or reported as a location.
    """
    if not is_source_path(src):
        return None
    clean = src.strip().removeprefix("./").lstrip("/")
    root = str(surface.get("path_root") or "").strip("/")
    if not root or clean.startswith(f"{root}/"):
        return clean
    return f"{root}/{clean}"


def _target(config: dict[str, Any], index: int) -> dict[str, str] | None:
    api = str(config.get("api") or "").rstrip("/")
    org = str(config.get("org") or "")
    project = str(config.get("project") or "")
    if not api or not org or not project:
        return None
    token_env = str(config.get("token_env") or "LOUPFEED_DASHBOARD_TOKEN")
    return {
        "name": str(config.get("name") or f"instance {index + 1}"),
        "api": api,
        "org": org,
        "project": project,
        "token": os.environ.get(token_env, ""),
        "token_env": token_env,
    }


def loupfeed_targets(surface: dict[str, Any]) -> list[dict[str, str]]:
    """Every loupfeed instance a surface reports into, in search order.

    One app can report to more than one instance: production builds to the
    production instance, stage builds to a development one. Both carry the same
    release prefix, so they belong to one surface rather than to two, and a
    lookup tries them in the order the registry lists them.

    Tokens are read from the environment variable each entry names, so the
    mapping stays config and the secret does not.
    """
    config = surface.get("loupfeed")
    entries = config if isinstance(config, list) else [config]
    targets = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        target = _target(entry, index)
        if target:
            targets.append(target)
    return targets


def loupfeed_target(surface: dict[str, Any]) -> dict[str, str] | None:
    """The surface's first loupfeed instance, or None when it has none."""
    targets = loupfeed_targets(surface)
    return targets[0] if targets else None


def registry_summary(path: str | None = None) -> str:
    """The registry rendered for the triage prompt."""
    surfaces = load_surfaces(path)
    if not surfaces:
        return "- (no surfaces configured — triage cannot map a report to a repository)"
    lines = []
    for surface in surfaces:
        parts = [f"- `{surface['key']}` -> repo `{surface['repo']}`"]
        if surface.get("path_root"):
            parts.append(f"build root `{surface['path_root']}/`")
        projects = surface.get("jira_projects")
        if isinstance(projects, list) and projects:
            parts.append("Jira " + ", ".join(str(p) for p in projects))
        if loupfeed_target(surface):
            parts.append("loupfeed reports available")
        lines.append(", ".join(parts))
    return "\n".join(lines)
