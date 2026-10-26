"""Quality gates for the versioned semantic evaluation foundation."""

from __future__ import annotations

import json

import pytest
from evals.semantic_corpus import CORPUS_PATH, INSTANCES_PATH, load_semantic_corpus
from evals.semantic_run import compare_rows, evaluate_semantic


def test_v1_corpus_is_governed_and_covers_initial_slices():
    corpus = load_semantic_corpus()

    assert corpus.schema_version == 1
    assert len(corpus.cases) >= 12
    assert len(corpus.instances) >= 2
    assert {case["adjudication"]["status"] for case in corpus.cases} >= {
        "accepted",
        "disputed",
    }
    assert {case["answerability"] for case in corpus.cases} == {
        "answerable",
        "ambiguous",
        "unanswerable",
    }
    mutation_classes = {
        mutation["class"]
        for case in corpus.cases
        for mutation in case.get("mutations", [])
    }
    assert mutation_classes >= {
        "missing_filter",
        "swapped_filter",
        "wrong_aggregation",
        "wrong_time_grain",
        "missing_distinct",
        "wrong_top_k_order",
        "wrong_limit",
        "missing_group_dimension",
        "fanout",
    }


def test_set_and_multiset_comparators_remain_distinct():
    expected = [("paid",), ("pending",)]
    duplicate = [("paid",), ("paid",), ("pending",)]

    assert compare_rows(expected, duplicate, "set")
    assert not compare_rows(expected, duplicate, "multiset")
    assert not compare_rows(expected, list(reversed(expected)), "ordered")
    with pytest.raises(ValueError, match="unknown result comparator"):
        compare_rows(expected, duplicate, "invented")


def test_semantic_smoke_gate_and_known_gap_baseline():
    report = evaluate_semantic()

    assert report["accepted_sql_recall"] == 1.0
    assert report["deterministic_mutation_recall"] == 1.0
    assert report["known_gaps_total"] == report["known_gaps_observed"] == 1
    assert report["set_only_matches"] >= 1
    assert report["data_only_coincidences"] >= 2
    assert report["required_ambiguities"] >= 3
    assert report["failures"] == []


def test_loader_rejects_duplicate_case_ids(tmp_path):
    raw = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    raw["cases"].append(raw["cases"][0])
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate semantic case_id"):
        load_semantic_corpus(corpus_path, INSTANCES_PATH)
