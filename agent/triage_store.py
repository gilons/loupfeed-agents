"""The internal triage log: one record per triaged report, deduped by fingerprint.

Triage's public output is a comment on the ticket. This is the other half: a
structured record, so the fifth report of one defect attaches to the first
investigation instead of paying for a fifth. In an organisation with many
reporters that matching is most of triage's value, and prose cannot do it.

Records live on LangGraph thread metadata, the same place reviewer findings live
(see ``reviewer_findings``): durable across sandbox eviction and searchable
across threads, with no new infrastructure.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from langgraph_sdk import get_client

logger = logging.getLogger(__name__)

TRIAGE_THREAD_KIND = "triage"

_NOISE = re.compile(r"[^a-z0-9]+")
_HEX = re.compile(r"\b[0-9a-f]{7,}\b")
_NUMBERS = re.compile(r"\b\d+\b")


def _canonical(text: str) -> str:
    """Reduce a symptom to what two reports of one defect would share.

    Ids, shas and line numbers differ between reports of the same bug, so they
    are stripped before hashing; without that the fingerprint never matches.
    """
    lowered = (text or "").lower()
    lowered = _HEX.sub("", lowered)
    lowered = _NUMBERS.sub("", lowered)
    return _NOISE.sub(" ", lowered).strip()


def fingerprint(surface: str, symptom: str, path: str | None = None) -> str:
    """A stable key for "this defect", stable enough to match a second report."""
    parts = [surface or "", _canonical(symptom)[:200], (path or "").strip()]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


async def record_result(thread_id: str, record: dict[str, Any]) -> None:
    """Persist a triage record on its own thread's metadata."""
    client = get_client()
    await client.threads.update(
        thread_id=thread_id,
        metadata={
            "kind": TRIAGE_THREAD_KIND,
            "triage_fingerprint": record.get("fingerprint"),
            "triage": record,
        },
    )


async def find_prior(
    fingerprint_value: str, exclude_thread_id: str | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """Earlier triage records sharing a fingerprint, newest first."""
    client = get_client()
    try:
        threads = await client.threads.search(
            metadata={"kind": TRIAGE_THREAD_KIND, "triage_fingerprint": fingerprint_value},
            limit=max(1, min(limit, 20)),
        )
    except Exception:
        logger.warning("prior-triage search failed", exc_info=True)
        return []
    prior = []
    for thread in threads if isinstance(threads, list) else []:
        if not isinstance(thread, dict):
            continue
        if exclude_thread_id and thread.get("thread_id") == exclude_thread_id:
            continue
        metadata = thread.get("metadata")
        entry = metadata.get("triage") if isinstance(metadata, dict) else None
        if isinstance(entry, dict):
            prior.append(entry)
    return prior
