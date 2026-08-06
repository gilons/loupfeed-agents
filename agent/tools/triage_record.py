"""Tools: check for a prior investigation, and log this one.

Two calls bracket a triage run. ``find_prior_triage`` first, so a repeat report
costs a lookup instead of an investigation, and ``record_triage`` last, so the
next repeat is cheap too.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..reviewer_findings import get_thread_id_from_runtime
from ..triage_store import find_prior, fingerprint, record_result


def find_prior_triage(surface: str, symptom: str, path: str | None = None) -> dict[str, Any]:
    """Has this defect been triaged before? Call this before investigating.

    Matching is on a fingerprint of the surface, the symptom's wording (with
    ids, numbers and shas stripped) and the source path when known. A hit means
    the work is already done: report the earlier verdict and its suspects rather
    than repeating them, and say which ticket it came from.

    Args:
        surface: Surface key from the registry.
        symptom: The report in a few words, as the reporter framed it.
        path: The resolved source path, when a report anchor gave you one.

    Returns:
        ``{fingerprint, prior: [record, ...]}``. An empty list means this is new.
    """
    key = fingerprint(surface, symptom, path)
    thread_id = get_thread_id_from_runtime()
    prior = asyncio.run(find_prior(key, exclude_thread_id=thread_id))
    return {"fingerprint": key, "prior": prior, "count": len(prior)}


def record_triage(
    surface: str,
    symptom: str,
    verdict: str,
    confidence: str,
    hypothesis: str,
    next_test: str,
    repo: str | None = None,
    path: str | None = None,
    release: str | None = None,
    suspect_commits: list[str] | None = None,
    anchored: bool = False,
    ruled_out: list[str] | None = None,
    ticket: str | None = None,
) -> dict[str, Any]:
    """Log this triage internally, so a later report of the same defect matches it.

    Call this once, after the investigation and before writing the reply. Record
    what you actually established, including a negative result: "not reproduced
    from the report, no anchor found" is a useful record and a dishonest
    "confirmed" is worse than nothing.

    Args:
        surface: Surface key from the registry.
        symptom: The report in a few words.
        verdict: One of ``confirmed``, ``probable``, ``unclear``, ``not_a_bug``,
            ``duplicate``.
        confidence: ``high``, ``medium`` or ``low``.
        hypothesis: The mechanism, in causal terms: what happens, in what order,
            that produces this symptom.
        next_test: The cheapest check that would confirm or kill the hypothesis.
        repo: ``owner/name`` the defect lives in, when established.
        path: Repository path of the suspect code.
        release: The release the report came from.
        suspect_commits: Suspect shas, most likely first. Only shas whose diff
            you read.
        anchored: True when a loupfeed report gave you a release and a resolved
            source; False when this came from prose and code search.
        ruled_out: Things checked and eliminated, so the next person skips them.
        ticket: The issue key this triage was about.

    Returns:
        ``{ok, fingerprint}``.
    """
    key = fingerprint(surface, symptom, path)
    record: dict[str, Any] = {
        "fingerprint": key,
        "surface": surface,
        "symptom": symptom,
        "verdict": verdict,
        "confidence": confidence,
        "hypothesis": hypothesis,
        "next_test": next_test,
        "repo": repo,
        "path": path,
        "release": release,
        "suspect_commits": [s for s in (suspect_commits or []) if isinstance(s, str)],
        "anchored": bool(anchored),
        "ruled_out": [r for r in (ruled_out or []) if isinstance(r, str)],
        "ticket": ticket,
    }
    thread_id = get_thread_id_from_runtime()
    asyncio.run(record_result(thread_id, record))
    return {"ok": True, "fingerprint": key}
