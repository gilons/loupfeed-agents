"""Tool: ``transcribe_recording`` — transcribe a Teams meeting recording.

Teams' own transcript is reachable through Graph only for private-chat meetings,
and only via metered beta APIs for channel meetings. The recording itself, on the
other hand, sits in the channel's SharePoint library and is readable with the
tenant ``Sites.Read.All`` grant this app already holds. So for channel meetings —
which is where standups happen — the reliable route is: find the ``.mp4``, hand
its pre-authenticated download URL to a speech-to-text service, transcribe.

Audio goes to AssemblyAI's **EU** endpoint (AWS eu-west-1, Dublin). Meeting audio
is personal data under the DSGVO; the SDK/API default is North America, so the
region is pinned explicitly here and must not be made configurable per call.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

# EU data-residency endpoint. Deliberately not configurable: see module docstring.
_ASSEMBLYAI_EU_BASE = "https://api.eu.assemblyai.com"
_TIMEOUT = 30
_POLL_INTERVAL_SECONDS = 5
_POLL_TIMEOUT_SECONDS = 900
_MAX_TRANSCRIPT_CHARS = 120_000


def _api_key() -> str:
    import os

    return os.environ.get("ASSEMBLY_AI_API_KEY", "")


def _fail(reason: str, *, detail: str = "") -> dict[str, Any]:
    if detail:
        logger.warning("transcribe_recording failed: %s", detail)
    return {"ok": False, "reason": reason}


def _format_utterances(utterances: list[dict[str, Any]]) -> str:
    lines = []
    for u in utterances:
        start_ms = int(u.get("start") or 0)
        stamp = f"{start_ms // 60000:02d}:{(start_ms // 1000) % 60:02d}"
        speaker = str(u.get("speaker") or "?")
        lines.append(f"[{stamp}] Speaker {speaker}: {str(u.get('text') or '').strip()}")
    return "\n".join(lines)


def transcribe_recording(audio_url: str, speakers_expected: int = 0) -> dict[str, Any]:
    """Transcribe a meeting recording from its download URL, with speaker turns.

    Pair this with ``graph_find_recording``, which returns the ``audio_url`` for a
    channel meeting's recording. Use it when you need what was actually said in a
    call and Teams' own transcript is not reachable — e.g. standups held in a
    channel.

    Speaker turns come back as ``Speaker A`` / ``Speaker B``, not names: voices are
    separated but not identified. To attribute them, read the call's participant
    list first (the channel message carrying ``callEndedEventMessageDetail`` lists
    ``callParticipants``) and name the candidates rather than guessing — e.g.
    "Speaker A (Giles or Ewi)".

    Audio is processed in the EU. Never name or describe the transcription
    provider, endpoint or model in a reply — say you transcribed the recording.
    Do not use this on recordings that should not leave our tenant without asking.

    Args:
        audio_url: A URL the service can fetch the media from (the
            pre-authenticated ``audio_url`` from ``graph_find_recording``).
        speakers_expected: Optional hint for how many people spoke; improves
            speaker separation when you know the number.

    Returns:
        ``{"ok": True, "transcript": str, "duration_seconds": int, "speakers": int}``
        on success; ``{"ok": False, "reason": str}`` on failure.
    """
    if not isinstance(audio_url, str) or not audio_url.startswith("http"):
        return _fail("I need the recording's download link to transcribe it.")
    key = _api_key()
    if not key:
        return _fail(
            "Transcription isn't set up on my side yet, so I can't turn this recording into text.",
            detail="ASSEMBLY_AI_API_KEY is not set",
        )

    headers = {"authorization": key}
    payload: dict[str, Any] = {
        "audio_url": audio_url,
        "speaker_labels": True,
        "language_detection": True,
        "punctuate": True,
        "format_text": True,
    }
    if speakers_expected and speakers_expected > 1:
        payload["speakers_expected"] = speakers_expected

    try:
        created = requests.post(
            f"{_ASSEMBLYAI_EU_BASE}/v2/transcript",
            json=payload,
            headers=headers,
            timeout=_TIMEOUT,
        )
        if created.status_code >= 400:
            return _fail(
                "I couldn't start transcribing this recording.",
                detail=f"POST /v2/transcript -> {created.status_code}: {created.text[:500]}",
            )
        transcript_id = created.json().get("id")
        if not transcript_id:
            return _fail(
                "I couldn't start transcribing this recording.",
                detail=f"no transcript id in response: {created.text[:300]}",
            )

        deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
        while True:
            if time.monotonic() > deadline:
                return _fail(
                    "Transcribing this recording is taking longer than I can wait. "
                    "Ask me again in a few minutes.",
                    detail=f"poll timeout for transcript {transcript_id}",
                )
            time.sleep(_POLL_INTERVAL_SECONDS)
            poll = requests.get(
                f"{_ASSEMBLYAI_EU_BASE}/v2/transcript/{transcript_id}",
                headers=headers,
                timeout=_TIMEOUT,
            )
            if poll.status_code >= 400:
                return _fail(
                    "I lost track of the transcription job.",
                    detail=f"GET /v2/transcript -> {poll.status_code}: {poll.text[:500]}",
                )
            body = poll.json()
            status = str(body.get("status") or "")
            if status == "error":
                return _fail(
                    "The recording couldn't be transcribed.",
                    detail=f"service reported error: {str(body.get('error'))[:300]}",
                )
            if status == "completed":
                break

        utterances = body.get("utterances") or []
        text = _format_utterances(utterances) if utterances else str(body.get("text") or "")
        if not text.strip():
            return _fail("The recording transcribed as empty — there may be no speech in it.")
        speakers = len({u.get("speaker") for u in utterances if u.get("speaker")})
        truncated = len(text) > _MAX_TRANSCRIPT_CHARS
        return {
            "ok": True,
            "transcript": text[:_MAX_TRANSCRIPT_CHARS],
            "duration_seconds": int(body.get("audio_duration") or 0),
            "speakers": speakers,
            **({"truncated": True} if truncated else {}),
        }
    except requests.RequestException as exc:
        return _fail(
            "I couldn't reach the transcription service. Worth retrying shortly.",
            detail=f"{type(exc).__name__}: {exc}",
        )
