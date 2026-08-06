"""The internal log: two reports of one defect must land on one fingerprint.

Dedup is the point of the log. Reporters describe the same defect with different
ids, counts and line numbers, so those are stripped before hashing; without that
the fingerprint never matches and every repeat pays for a fresh investigation.
"""

from __future__ import annotations

from agent.triage_store import fingerprint


def test_the_same_defect_described_twice_fingerprints_once():
    first = fingerprint("acme-webapp", "Export gives an empty file for candidate 4821")
    second = fingerprint("acme-webapp", "export gives an empty file for candidate 9137!")
    assert first == second


def test_shas_and_counts_do_not_split_a_fingerprint():
    assert fingerprint("acme-webapp", "crash on export, 12 occurrences, build a1b2c3d4") == (
        fingerprint("acme-webapp", "crash on export, 340 occurrences, build 9f8e7d6c")
    )


def test_different_defects_stay_apart():
    assert fingerprint("acme-webapp", "export is empty") != fingerprint(
        "acme-webapp", "login loops forever"
    )


def test_the_same_words_on_another_surface_is_another_defect():
    assert fingerprint("acme-webapp", "export is empty") != fingerprint(
        "acme-admin", "export is empty"
    )


def test_the_source_path_separates_two_defects_that_read_alike():
    assert fingerprint("acme-webapp", "export is empty", "apps/webapp/app/pdf.tsx") != fingerprint(
        "acme-webapp", "export is empty", "apps/webapp/app/csv.tsx"
    )
