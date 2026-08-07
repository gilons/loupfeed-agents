"""Provenance from links, not from the ticket's own narrative.

Live on SPB-7 the agent reported that a bug "came from support chat". Nothing was
linked to the issue and the service desk was not even readable, so the claim came
from the description's prose and could not have been checked. These tests pin the
three answers that must stay distinguishable: a customer ticket that was read,
nothing linked at all, and something that exists but could not be read.
"""

from __future__ import annotations

import json as jsonlib

import pytest

from agent import surfaces
from agent.tools.jira_provenance import jira_report_provenance

REGISTRY = [
    {
        "key": "acme-webapp",
        "repo": "acme/acme",
        "jira_projects": ["BUG"],
        "support_projects": ["SUP"],
    }
]


class _Resp:
    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self.text = jsonlib.dumps(payload)

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self):
        return jsonlib.loads(self.text)


def _issue(links, description_text=""):
    return {
        "fields": {
            "issuelinks": links,
            "description": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": description_text}]}
                ],
            },
        }
    }


def _link(key, summary=None):
    other = {"key": key}
    if summary:
        other["fields"] = {"summary": summary}
    return {"type": {"name": "Escalates"}, "outwardIssue": other}


@pytest.fixture(autouse=True)
def _registry(tmp_path, monkeypatch):
    path = tmp_path / "surfaces.json"
    path.write_text(jsonlib.dumps(REGISTRY))
    surfaces._cache = None
    monkeypatch.setattr(surfaces, "SURFACES_FILE", str(path))
    yield
    surfaces._cache = None


def test_a_linked_support_ticket_is_evidence_of_a_customer(monkeypatch):
    def _request(product, method, path, body=None, *, attributed=False):
        if path.startswith("/rest/api/3/issue/BUG-7"):
            return _Resp(200, _issue([_link("SUP-3")]))
        return _Resp(
            200,
            {
                "fields": {
                    "summary": "Cannot paste my own text",
                    "reporter": {"displayName": "Sabrina"},
                    "created": "2026-08-05T09:00:00Z",
                }
            },
        )

    monkeypatch.setattr("agent.tools.jira_provenance.atlassian_request", _request)
    out = jira_report_provenance("BUG-7")
    assert out["provenance"] == "customer_via_support"
    assert out["support_tickets"][0]["key"] == "SUP-3"
    assert out["support_tickets"][0]["reporter"] == "Sabrina"
    assert "warning" not in out


def test_no_links_means_the_descriptions_story_is_second_hand(monkeypatch):
    """The exact SPB-7 shape: prose claims support chat, nothing is linked."""

    def _request(product, method, path, body=None, *, attributed=False):
        return _Resp(
            200, _issue([], "Reported by Sabrina (Deliveru support chat, 5 Aug): she could not...")
        )

    monkeypatch.setattr("agent.tools.jira_provenance.atlassian_request", _request)
    out = jira_report_provenance("BUG-7")
    assert out["provenance"] == "no_linked_ticket"
    assert out["support_tickets"] == []
    # The claim is surfaced as a claim, with an explicit instruction not to assert it.
    assert "support chat" in out["description_claims"]
    assert "reported by" in out["description_claims"]
    assert "do not state it as a finding" in out["warning"]


def test_an_unreadable_support_ticket_is_not_an_absent_one(monkeypatch):
    """A project you cannot see answers exactly like a project with nothing in it."""

    def _request(product, method, path, body=None, *, attributed=False):
        if path.startswith("/rest/api/3/issue/BUG-7"):
            return _Resp(200, _issue([_link("SUP-9")]))
        return _Resp(404, {"errorMessages": ["Issue does not exist or you do not have permission"]})

    monkeypatch.setattr("agent.tools.jira_provenance.atlassian_request", _request)
    out = jira_report_provenance("BUG-7")
    assert out["provenance"] == "unknown_unreadable"
    assert out["unreadable"][0]["key"] == "SUP-9"
    assert "404" in out["unreadable"][0]["why"]


def test_being_blind_to_the_support_project_is_never_reported_as_no_ticket(monkeypatch):
    """Jira hides links to issues you cannot browse, so absence proves nothing.

    Live, the deployment's service account could see zero service desks, so a
    bug escalated from support looks exactly like a bug nobody escalated.
    """

    def _request(product, method, path, body=None, *, attributed=False):
        if path.startswith("/rest/api/3/project/SUP"):
            return _Resp(404, {})  # cannot browse the service desk
        return _Resp(200, _issue([], "Reported by Sabrina in the support chat"))

    monkeypatch.setattr("agent.tools.jira_provenance.atlassian_request", _request)
    out = jira_report_provenance("BUG-7")
    assert out["provenance"] == "unknown_unreadable"
    assert out["support_projects_unreadable"] == ["SUP"]
    assert "may well exist" in out["warning"]
    assert "do not report" in out["warning"]


def test_links_outside_the_support_projects_do_not_imply_a_customer(monkeypatch):
    def _request(product, method, path, body=None, *, attributed=False):
        return _Resp(200, _issue([_link("BUG-2"), _link("DEV-4")]))

    monkeypatch.setattr("agent.tools.jira_provenance.atlassian_request", _request)
    out = jira_report_provenance("BUG-7")
    assert out["provenance"] == "no_linked_ticket"
    assert {link["key"] for link in out["links"]} == {"BUG-2", "DEV-4"}
    assert out["support_tickets"] == []


def test_an_unreadable_issue_reports_unknown_rather_than_guessing(monkeypatch):
    def _request(product, method, path, body=None, *, attributed=False):
        return _Resp(403, {})

    monkeypatch.setattr("agent.tools.jira_provenance.atlassian_request", _request)
    out = jira_report_provenance("BUG-7")
    assert out["ok"] is False
    assert out["provenance"] == "unknown_unreadable"


def test_support_projects_come_from_the_registry():
    surface = {"support_projects": ["dus", " SUP "]}
    assert surfaces.support_projects(surface) == ["DUS", "SUP"]
    assert surfaces.support_projects({}) == []
    assert surfaces.support_projects(None) == []
