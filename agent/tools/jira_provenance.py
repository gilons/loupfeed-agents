"""Tool: establish where a bug report actually came from, from evidence.

A bug's description often narrates its own origin ("reported by X in the support
chat"), and a triage agent that repeats that narration is reporting hearsay as a
finding. Live on SPB-7 the agent stated a report "came from support chat" when
the issue had no linked ticket at all and the service desk was not even
reachable, so the claim was unfalsifiable.

Provenance has exactly one reliable source in Jira: a link to a ticket in a
support project. This tool reports what the links actually say and, critically,
distinguishes three different answers that all look like silence:

- a linked support ticket exists and was read: the report came from a customer;
- no link exists at all: whatever the description says about its origin is
  second-hand and must be attributed, not asserted;
- a link or support project exists but could not be read: unknown, because a
  Jira query over a project you cannot see returns empty rather than an error.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..surfaces import jira_project_of, support_projects, surface_for_issue
from ..utils.atlassian_api import atlassian_request

logger = logging.getLogger(__name__)

# Phrases a description uses to narrate its own origin. Matching one is not
# evidence of anything; it is a claim to attribute to whoever wrote it.
_ORIGIN_CLAIM = re.compile(
    r"(support chat|service desk|helpdesk|help desk|reported by|customer (?:said|reported|wrote)"
    r"|raised by|via (?:chat|email|phone|teams|slack)|escalat)",
    re.IGNORECASE,
)


def _flatten(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text") or "")
        return " ".join(_flatten(c) for c in node.get("content") or [])
    if isinstance(node, list):
        return " ".join(_flatten(c) for c in node)
    return ""


def _project_readable(project_key: str) -> bool:
    r = atlassian_request("jira", "GET", f"/rest/api/3/project/{project_key}")
    return bool(r.ok)


def jira_report_provenance(issue_key: str) -> dict[str, Any]:
    """Where did this report come from? Answer from links, never from the prose.

    Call this before saying anything about how a bug reached you. The
    description's own account of its origin is not evidence: only a link to a
    ticket in a support project is.

    Read ``provenance`` and report exactly what it says:

    - ``customer_via_support``: a linked support ticket was read. Cite its key,
      and its reporter is the customer.
    - ``no_linked_ticket``: nothing is linked. Any origin the description claims
      is second-hand, so attribute it ("the description says it came from the
      support chat") and never state it as your own finding.
    - ``unknown_unreadable``: something is linked, or a support project is
      configured, but it could not be read. Say the provenance is unknown and
      why. Do NOT conclude that no support ticket exists: a Jira search over a
      project you cannot see returns empty, exactly like a project with nothing
      in it.

    Args:
        issue_key: The issue being triaged, e.g. ``BUG-7``.

    Returns:
        ``{ok, provenance, links, support_tickets, unreadable, description_claims}``.
    """
    key = (issue_key or "").strip().upper()
    if not key:
        return {"ok": False, "error": "issue_key is required"}

    surface = surface_for_issue(key)
    configured = support_projects(surface) if surface else []

    r = atlassian_request("jira", "GET", f"/rest/api/3/issue/{key}?fields=issuelinks,description")
    if not r.ok:
        return {
            "ok": False,
            "error": f"could not read {key} to inspect its links",
            "provenance": "unknown_unreadable",
        }
    fields = (r.json() or {}).get("fields") or {}

    links: list[dict[str, Any]] = []
    for link in fields.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        other = link.get("inwardIssue") or link.get("outwardIssue") or {}
        other_key = str(other.get("key") or "")
        if not other_key:
            continue
        links.append(
            {
                "key": other_key,
                "project": jira_project_of(other_key),
                "relation": ((link.get("type") or {}).get("name") or "linked"),
                "summary": ((other.get("fields") or {}).get("summary") or None),
            }
        )

    support, unreadable = [], []
    for link in links:
        if configured and link["project"] not in configured:
            continue
        detail = atlassian_request(
            "jira", "GET", f"/rest/api/3/issue/{link['key']}?fields=summary,reporter,created"
        )
        if not detail.ok:
            # A support ticket we cannot open is not an absent support ticket.
            unreadable.append({**link, "why": f"read returned {detail.status_code}"})
            continue
        detail_fields = (detail.json() or {}).get("fields") or {}
        support.append(
            {
                **link,
                "summary": detail_fields.get("summary"),
                "reporter": (detail_fields.get("reporter") or {}).get("displayName"),
                "created": detail_fields.get("created"),
            }
        )

    text = _flatten(fields.get("description"))
    claims = sorted({m.group(0).lower() for m in _ORIGIN_CLAIM.finditer(text)})

    # Jira omits links whose target the caller cannot browse, so "no links" and
    # "a link into a project I have no access to" are the same response. Absence
    # is therefore only meaningful when the support projects are reachable.
    blind_to = [p for p in configured if not _project_readable(p)]

    if support:
        provenance = "customer_via_support"
    elif unreadable or blind_to:
        provenance = "unknown_unreadable"
    else:
        provenance = "no_linked_ticket"

    result: dict[str, Any] = {
        "ok": True,
        "issue_key": key,
        "provenance": provenance,
        "links": links,
        "support_tickets": support,
        "unreadable": unreadable,
        "support_projects_configured": configured,
        "support_projects_unreadable": blind_to,
        "description_claims": claims,
    }
    if blind_to and not support:
        result["warning"] = (
            f"No support link is visible, but {', '.join(blind_to)} cannot be read by this "
            "deployment, and Jira hides links to issues you cannot browse. So a support ticket "
            "may well exist. Report the provenance as unknown for that reason; do not report "
            "that none exists."
        )
    if claims and provenance != "customer_via_support" and "warning" not in result:
        result["warning"] = (
            "The description narrates an origin but no support ticket backs it. "
            "Attribute that to the description; do not state it as a finding."
        )
    return result
