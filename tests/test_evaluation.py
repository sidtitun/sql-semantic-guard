"""Quality gates over the versioned SQL behavior corpus."""

from __future__ import annotations

from evals.corpus import (
    CASES,
    MAX_FALSE_BLOCK_RATE,
    MIN_SAFE_CASES,
    MIN_UNSAFE_CASES,
    TARGET_REWRITE_IDEMPOTENCE,
    TARGET_SAFE_RECALL,
    TARGET_UNSAFE_REJECTION,
)
from evals.run import evaluate


def test_frozen_corpus_has_minimum_coverage() -> None:
    safe = [case for case in CASES if case.expected_valid]
    unsafe = [case for case in CASES if not case.expected_valid]
    assert len(safe) >= MIN_SAFE_CASES
    assert len(unsafe) >= MIN_UNSAFE_CASES
    assert len({case.case_id for case in CASES}) == len(CASES)


def test_release_quality_thresholds() -> None:
    report = evaluate()
    assert report["safe_query_recall"] >= TARGET_SAFE_RECALL, report
    assert report["false_block_rate"] <= MAX_FALSE_BLOCK_RATE, report
    assert report["unsafe_query_rejection"] >= TARGET_UNSAFE_REJECTION, report
    assert report["rewrite_idempotence"] >= TARGET_REWRITE_IDEMPOTENCE, report
    assert report["failures"] == [], report
