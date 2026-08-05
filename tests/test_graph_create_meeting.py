"""Meeting tool: organizer-owned events, plain failures, no leaked internals."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent.tools.graph_create_meeting import graph_create_meeting


def test_rejects_missing_organizer():
    out = graph_create_meeting("", "Standup", "2026-08-06T11:00", "2026-08-06T11:30")
    assert out["ok"] is False


def test_rejects_non_local_times():
    out = graph_create_meeting("giles@dino-lab.io", "Standup", "tomorrow 11am", "2026-08-06T11:30")
    assert out["ok"] is False
    assert "2026-08-06T11:00" in out["reason"]


@patch("agent.tools.graph_create_meeting.get_graph_app_token", return_value="tok")
def test_creates_event_on_organizers_calendar(_tok):
    resp = MagicMock()
    resp.status_code = 201
    resp.json.return_value = {
        "id": "ev1",
        "webLink": "https://outlook.office365.com/calendar/item/ev1",
        "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/x"},
    }
    with patch("agent.tools.graph_create_meeting.requests.post", return_value=resp) as post:
        out = graph_create_meeting(
            "giles@dino-lab.io",
            "Planning sync",
            "2026-08-06T11:00",
            "2026-08-06T11:30",
            attendee_emails=["gael@dino-lab.io", "giles@dino-lab.io"],
        )
    assert out["ok"] is True
    assert out["join_url"].startswith("https://teams.microsoft.com/")
    url = post.call_args[0][0]
    assert "/users/giles@dino-lab.io/events" in url
    payload = post.call_args[1]["json"]
    assert payload["isOnlineMeeting"] is True
    assert payload["start"]["timeZone"] == "Europe/Berlin"
    # the organizer is not also an attendee
    addrs = [a["emailAddress"]["address"] for a in payload["attendees"]]
    assert addrs == ["gael@dino-lab.io"]


@patch("agent.tools.graph_create_meeting.get_graph_app_token", return_value="tok")
def test_missing_consent_fails_without_internals(_tok):
    resp = MagicMock()
    resp.status_code = 403
    resp.text = (
        '{"error":{"code":"ErrorAccessDenied","message":"Access is denied. Check credentials"}}'
    )
    with patch("agent.tools.graph_create_meeting.requests.post", return_value=resp):
        out = graph_create_meeting(
            "giles@dino-lab.io", "Standup", "2026-08-06T11:00", "2026-08-06T11:30"
        )
    assert out["ok"] is False
    assert "access" in out["reason"].lower()
    for leaked in ("403", "ErrorAccessDenied", "Calendars.ReadWrite"):
        assert leaked not in out["reason"]
