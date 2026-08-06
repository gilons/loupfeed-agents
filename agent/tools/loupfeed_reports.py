"""Tools: read bug reports out of a loupfeed instance.

A loupfeed report is the anchored kind: it carries the release the reporter was
running, the element or stack frame that failed, and (through the instance's
id-to-source manifest) the ``src:line`` that element compiles from. That is the
difference between triage as lookup and triage as detective work, so these
tools exist to get the anchor before any code is read.

Instance coordinates come from the surface registry; the dashboard token comes
from the environment variable that registry names. Nothing here knows about any
particular product.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from ..surfaces import loupfeed_targets, surface_for_key

logger = logging.getLogger(__name__)

_TIMEOUT = 30
_MAX_BREADCRUMBS = 15
_MAX_FRAMES = 20
_MAX_OCCURRENCES = 5


def _fetch(target: dict[str, str], path: str, params: dict[str, Any] | None) -> dict[str, Any]:
    url = f"{target['api']}/api/{target['org']}/{target['project']}{path}"
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {target['token']}"},
            params=params or {},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("loupfeed request failed: %s: %s", type(exc).__name__, exc)
        return {
            "ok": False,
            "error": f"the {target['name']} loupfeed instance could not be reached",
        }
    if response.status_code == 404:
        return {"ok": False, "error": f"not found on the {target['name']} instance", "absent": True}
    if response.status_code >= 400:
        logger.warning("loupfeed returned %s for %s", response.status_code, path)
        return {
            "ok": False,
            "error": (
                f"the {target['name']} loupfeed instance refused the read ({response.status_code})"
            ),
        }
    try:
        return {"ok": True, "instance": target["name"], "body": response.json()}
    except ValueError:
        return {"ok": False, "error": "the loupfeed instance returned a non-JSON body"}


def _get(surface_key: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read a path from the surface's instances, first one that has it wins.

    A surface can report into several instances (production and stage builds
    carry the same release prefix), so a report id missing from the first is
    looked for in the next rather than reported as absent.
    """
    surface = surface_for_key(surface_key)
    if not surface:
        return {"ok": False, "error": f"unknown surface {surface_key!r}; check the registry"}
    targets = loupfeed_targets(surface)
    if not targets:
        return {
            "ok": False,
            "error": f"surface {surface_key!r} has no loupfeed instance configured",
        }
    missing_tokens = [t for t in targets if not t["token"]]
    usable = [t for t in targets if t["token"]]
    if not usable:
        names = ", ".join(sorted({t["token_env"] for t in missing_tokens}))
        return {
            "ok": False,
            "error": (
                f"no dashboard token for {surface_key!r} (expected in {names}), "
                "so reports cannot be read"
            ),
        }
    last: dict[str, Any] = {"ok": False, "error": "no instance answered"}
    for target in usable:
        last = _fetch(target, path, params)
        if last["ok"]:
            return last
        if not last.get("absent"):
            return last
    return last


def _get_all(
    surface_key: str, path: str, params: dict[str, Any] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Read a path from every instance a surface reports into.

    Listing is a search, so unlike a detail lookup it must cover all instances:
    the twin of a support ticket may have been reported from a stage build.
    Returns the successful reads plus the first failure, if any, so a partly
    reachable registry still yields results and still reports the gap.
    """
    surface = surface_for_key(surface_key)
    if not surface:
        return [], {"ok": False, "error": f"unknown surface {surface_key!r}; check the registry"}
    targets = [t for t in loupfeed_targets(surface) if t["token"]]
    if not targets:
        return [], _get(surface_key, path, params)
    answers, failure = [], None
    for target in targets:
        fetched = _fetch(target, path, params)
        if fetched["ok"]:
            answers.append(fetched)
        elif failure is None:
            failure = fetched
    return answers, failure


def _source_string(resolved: Any) -> str | None:
    if isinstance(resolved, dict) and resolved.get("src"):
        line = resolved.get("line")
        return f"{resolved['src']}:{line}" if line else str(resolved["src"])
    return None


def _route(event: dict[str, Any]) -> str | None:
    request = event.get("request")
    if isinstance(request, dict):
        return request.get("route") or request.get("url")
    return None


def _frames(event: dict[str, Any]) -> list[str]:
    exception = event.get("exception")
    stack = (exception or {}).get("stacktrace") if isinstance(exception, dict) else None
    frames = stack.get("frames") if isinstance(stack, dict) else None
    if not isinstance(frames, list):
        return []
    # in_app frames first: a stack's top frame is usually framework code, and
    # the first line that belongs to the product is where triage starts.
    ordered = sorted(
        (f for f in frames if isinstance(f, dict)),
        key=lambda f: (not f.get("in_app"),),
    )
    out = []
    for frame in ordered[:_MAX_FRAMES]:
        where = f"{frame.get('filename') or '?'}:{frame.get('lineno') or '?'}"
        function = frame.get("function")
        out.append(f"{where} ({function})" if function else where)
    return out


def loupfeed_find_reports(
    surface: str,
    kind: str = "all",
    status: str = "open",
    release: str | None = None,
    contains: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List reports on a surface's loupfeed instance, to find the anchored twin of a ticket.

    Use this when a bug arrives as prose (a support ticket, a chat message) with
    no loupfeed id: search the instance for reports on the same screen or with
    the same wording. A prose ticket that matches a real crash group stops being
    guesswork.

    Args:
        surface: Surface key from the registry, e.g. ``acme-webapp``.
        kind: ``feedback`` (what people reported), ``crash`` (what the app threw),
            or ``all``.
        status: ``open``, ``resolved``, ``wontfix``, or ``any``.
        release: Only reports from this release string.
        contains: Case-insensitive substring filter over report text, route and
            exception message.
        limit: Max rows per kind (capped at 50).

    Returns:
        ``{ok, feedback: [...], crashes: [...]}`` with one summary per row, or
        ``{ok: False, error}``.
    """
    capped = max(1, min(limit, 50))
    params: dict[str, Any] = {}
    if status and status != "any":
        params["status"] = status
    if release:
        params["release"] = release
    needle = (contains or "").strip().lower()
    result: dict[str, Any] = {"ok": True}
    unreachable: list[str] = []

    if kind in ("feedback", "all"):
        answers, failure = _get_all(surface, "/threads", params)
        if not answers:
            return failure or {"ok": False, "error": "no instance answered"}
        if failure:
            unreachable.append(str(failure.get("error")))
        rows = []
        for answer in answers:
            body = answer["body"]
            for thread in body.get("threads", []) if isinstance(body, dict) else []:
                if not isinstance(thread, dict):
                    continue
                text = str(thread.get("text") or thread.get("latestText") or "")
                route = str(thread.get("route") or "")
                if needle and needle not in f"{text} {route}".lower():
                    continue
                rows.append(
                    {
                        "id": thread.get("threadId"),
                        "instance": answer["instance"],
                        "text": text[:300],
                        "route": route or None,
                        "status": thread.get("status"),
                        "release": thread.get("release"),
                        "last_seen": thread.get("lastSeenAt") or thread.get("receivedAt"),
                    }
                )
        result["feedback"] = rows[:capped]

    if kind in ("crash", "all"):
        answers, failure = _get_all(surface, "/crashes", params)
        if not answers:
            return failure or {"ok": False, "error": "no instance answered"}
        if failure:
            unreachable.append(str(failure.get("error")))
        rows = []
        for answer in answers:
            body = answer["body"]
            for group in body.get("crashes", []) if isinstance(body, dict) else []:
                if not isinstance(group, dict):
                    continue
                title = (
                    f"{group.get('type') or ''} {group.get('value') or group.get('title') or ''}"
                )
                if needle and needle not in title.lower():
                    continue
                rows.append(
                    {
                        "id": group.get("groupId"),
                        "instance": answer["instance"],
                        "title": title.strip()[:300],
                        "occurrences": group.get("count") or group.get("occurrences"),
                        "status": group.get("status"),
                        "last_seen": group.get("lastSeenAt"),
                    }
                )
        result["crashes"] = rows[:capped]

    # A partial search must say so: "nothing found" and "nothing found where I
    # could look" are different answers, and only one of them clears a surface.
    if unreachable:
        result["incomplete"] = sorted(set(unreachable))
    return result


def loupfeed_report(surface: str, kind: str, report_id: str) -> dict[str, Any]:
    """Read one report in full, with the release and the resolved source line.

    This is the anchor. ``release`` names the exact build the reporter ran
    (``<surface>@<commit>``), and ``resolved_source`` is the ``src:line`` the
    failing element compiles from **in that build** — so blame those lines at
    that commit, never at the head of the default branch.

    For a crash, ``first_seen_release`` is the oldest release the group appears
    in, which brackets the change that introduced it.

    Args:
        surface: Surface key from the registry.
        kind: ``feedback`` for a reported thread, ``crash`` for a crash group.
        report_id: The thread id or crash group id.

    Returns:
        A normalised report, or ``{ok: False, error}``.
    """
    if kind == "feedback":
        fetched = _get(surface, f"/threads/{report_id}")
        if not fetched["ok"]:
            return fetched
        body = fetched["body"]
        events = [e for e in body.get("events", []) if isinstance(e, dict)]
        if not events:
            return {"ok": False, "error": "that thread has no events"}
        newest = events[0]
        releases = sorted({str(e.get("release")) for e in events if e.get("release")})
        return {
            "ok": True,
            "kind": "feedback",
            "id": body.get("threadId"),
            "instance": fetched.get("instance"),
            "status": newest.get("status"),
            "text": str(newest.get("text") or "")[:2000],
            "route": _route(newest),
            "release": newest.get("release"),
            "releases": releases,
            "resolved_source": _source_string(body.get("resolvedSource")),
            "element": (newest.get("element") or {}).get("selector"),
            "occurrences": len(events),
            "replay_id": (newest.get("replay") or {}).get("replayId"),
            "breadcrumbs": list(newest.get("breadcrumbs") or [])[-_MAX_BREADCRUMBS:],
            "replies": [
                {"author": c.get("author"), "text": str(c.get("text") or "")[:500]}
                for c in body.get("comments", [])
                if isinstance(c, dict)
            ],
        }

    if kind == "crash":
        fetched = _get(surface, f"/crashes/{report_id}")
        if not fetched["ok"]:
            return fetched
        body = fetched["body"]
        # The endpoint sorts occurrences newest first, so the last one is the
        # first time this crash was ever seen — and its release brackets the
        # change that introduced it.
        events = [e for e in body.get("events", []) if isinstance(e, dict)]
        if not events:
            return {"ok": False, "error": "that crash group has no occurrences"}
        newest, oldest = events[0], events[-1]
        exception = newest.get("exception") if isinstance(newest.get("exception"), dict) else {}
        releases = sorted({str(e.get("release")) for e in events if e.get("release")})
        return {
            "ok": True,
            "kind": "crash",
            "id": body.get("groupId"),
            "instance": fetched.get("instance"),
            "status": newest.get("status"),
            "exception": {
                "type": exception.get("type"),
                "value": str(exception.get("value") or "")[:1000],
                "handled": (exception.get("mechanism") or {}).get("handled"),
            },
            "frames": _frames(newest),
            "route": _route(newest),
            "release": newest.get("release"),
            "first_seen_release": oldest.get("release"),
            "releases": releases,
            "occurrences": len(events),
            "first_seen": oldest.get("receivedAt"),
            "last_seen": newest.get("receivedAt"),
            "routes": sorted({r for r in (_route(e) for e in events) if r})[:10],
            "replay_id": (newest.get("replay") or {}).get("replayId"),
            "breadcrumbs": list(newest.get("breadcrumbs") or [])[-_MAX_BREADCRUMBS:],
            "recent_occurrences": [
                {"at": e.get("receivedAt"), "release": e.get("release"), "route": _route(e)}
                for e in events[:_MAX_OCCURRENCES]
            ],
        }

    return {"ok": False, "error": "kind must be 'feedback' or 'crash'"}
