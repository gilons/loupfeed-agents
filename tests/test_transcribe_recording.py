"""Tests for the meeting-recording transcription tool.

Two things matter beyond the happy path: audio must go to the EU endpoint, and
failures must not leak provider or endpoint details into the reason the agent
relays.
"""

from __future__ import annotations

from importlib import import_module

import pytest

# The package re-exports the function under the module's own name, so a plain
# `import agent.tools.transcribe_recording` hands back the function.
mod = import_module("agent.tools.transcribe_recording")

AUDIO = "https://example.sharepoint.com/download/recording.mp4?token=abc"


class _Resp:
    def __init__(self, status: int, payload: dict | None = None, text: str = ""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or ""

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("ASSEMBLY_AI_API_KEY", "test-key")
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)


def test_requires_a_url():
    assert mod.transcribe_recording("")["ok"] is False


def test_missing_key_is_reported_without_naming_the_provider(monkeypatch):
    monkeypatch.delenv("ASSEMBLY_AI_API_KEY", raising=False)
    result = mod.transcribe_recording(AUDIO)
    assert result["ok"] is False
    assert "assembly" not in result["reason"].lower()


def test_posts_to_the_eu_endpoint_and_returns_speaker_turns(monkeypatch):
    calls: list[str] = []

    def _post(url, json=None, headers=None, timeout=None):  # noqa: A002
        calls.append(url)
        assert json["speaker_labels"] is True
        return _Resp(200, {"id": "t1"})

    def _get(url, headers=None, timeout=None):
        calls.append(url)
        return _Resp(
            200,
            {
                "status": "completed",
                "audio_duration": 719,
                "utterances": [
                    {"start": 0, "speaker": "A", "text": "Morning."},
                    {"start": 65000, "speaker": "B", "text": "I finished the exposé fix."},
                ],
            },
        )

    monkeypatch.setattr(mod.requests, "post", _post)
    monkeypatch.setattr(mod.requests, "get", _get)

    result = mod.transcribe_recording(AUDIO, speakers_expected=3)
    assert result["ok"] is True
    assert all(u.startswith("https://api.eu.assemblyai.com/") for u in calls), calls
    assert result["speakers"] == 2
    assert result["duration_seconds"] == 719
    assert "[00:00] Speaker A: Morning." in result["transcript"]
    assert "[01:05] Speaker B: I finished the exposé fix." in result["transcript"]


def test_service_error_reason_stays_provider_free(monkeypatch):
    monkeypatch.setattr(
        mod.requests, "post", lambda *a, **k: _Resp(401, text="Unauthorized: bad api key")
    )
    result = mod.transcribe_recording(AUDIO)
    assert result["ok"] is False
    reason = result["reason"].lower()
    for leak in ("assembly", "401", "unauthorized", "api.eu", "/v2/transcript"):
        assert leak not in reason, reason


def test_error_status_from_the_job_is_reported_plainly(monkeypatch):
    monkeypatch.setattr(mod.requests, "post", lambda *a, **k: _Resp(200, {"id": "t2"}))
    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *a, **k: _Resp(200, {"status": "error", "error": "download failed: 403"}),
    )
    result = mod.transcribe_recording(AUDIO)
    assert result["ok"] is False
    assert "403" not in result["reason"]


def test_channel_meeting_resolves_the_link_at_submit_time(monkeypatch):
    """The fused tool must fetch the download link itself.

    A link fetched in an earlier turn expires (SharePoint tempauth), which is what
    made the two-step flow fail with a 401 download error in production.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        mod, "_submit_and_poll", lambda url, n: {"ok": True, "transcript": "x", "_url": url}
    )

    def _find(team, channel, on_or_before="", subject_contains=""):
        calls.append(f"{team}/{channel}")
        return {
            "ok": True,
            "name": "standup.mp4",
            "created": "2026-07-30T09:06:18Z",
            "audio_url": "https://sp/fresh?tempauth=NEW",
        }

    graph_api = import_module("agent.tools.graph_api")
    monkeypatch.setattr(graph_api, "graph_find_recording", _find)
    out = mod.transcribe_channel_meeting("team-1", "Dev team", subject_contains="Daily Standup")
    assert out["ok"] is True
    assert out["_url"].endswith("tempauth=NEW")
    assert out["recording"] == "standup.mp4"
    assert calls == ["team-1/Dev team"]


def test_channel_meeting_reports_a_find_failure_plainly(monkeypatch):
    graph_api = import_module("agent.tools.graph_api")
    monkeypatch.setattr(
        graph_api,
        "graph_find_recording",
        lambda *a, **k: {"ok": False, "reason": "I couldn't find a recording matching that."},
    )
    out = mod.transcribe_channel_meeting("team-1", "Nope")
    assert out["ok"] is False
    assert "couldn't find" in out["reason"]
