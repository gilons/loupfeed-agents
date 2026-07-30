"""Tests for the meeting-recording transcription tool.

Two things matter beyond the happy path: audio must go to the EU endpoint, and
failures must not leak provider or endpoint details into the reason the agent
relays.
"""

from __future__ import annotations

import pytest

from importlib import import_module

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
