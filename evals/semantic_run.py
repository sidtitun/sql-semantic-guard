"""Run the semantic intent and multi-instance evaluation smoke corpus."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from evals.semantic_corpus import build_semantic_guard, intent_from_dict, load_semantic_corpus


def compare_rows(
    expected: Sequence[tuple[Any, ...]],
    actual: Sequence[tuple[Any, ...]],
    comparator: str,
) -> bool:
    """Compare query results without collapsing bag semantics accidentally."""
    if comparator == "ordered":
        return list(expected) == list(actual)
    if comparator == "set":
        return set(expected) == set(actual)
    if comparator == "multiset":
        return Counter(expected) == Counter(actual)
    raise ValueError(f"unknown result comparator: {comparator}")


def _execute(
    setup_sql: Sequence[str], sql: str, params: Mapping[str, Any]
) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in setup_sql:
            connection.execute(statement)
        return [tuple(row) for row in connection.execute(sql, dict(params)).fetchall()]
    finally:
        connection.close()


def evaluate_semantic() -> dict[str, Any]:
    """Evaluate deterministic intent mutations and counterfactual instances."""
    corpus = load_semantic_corpus()
    guard = build_semantic_guard()
    report: dict[str, Any] = {
        "schema_version": corpus.schema_version,
        "corpus_id": corpus.corpus_id,
        "oracle": corpus.oracle,
        "case_count": len(corpus.cases),
        "adjudication": Counter(),
        "answerability": Counter(),
        "accepted_sql_total": 0,
        "accepted_sql_passed": 0,
        "deterministic_mutations_total": 0,
        "deterministic_mutations_detected": 0,
        "known_gaps_total": 0,
        "known_gaps_observed": 0,
        "mutation_classes": {},
        "required_ambiguities": 0,
        "execution_pairs": 0,
        "set_only_matches": 0,
        "data_only_coincidences": 0,
        "failures": [],
    }

    for case in corpus.cases:
        report["adjudication"][case["adjudication"]["status"]] += 1
        report["answerability"][case["answerability"]] += 1
        report["required_ambiguities"] += sum(
            1 for issue in case.get("ambiguities", []) if issue.get("required")
        )
        if case["adjudication"]["status"] != "accepted" or case["answerability"] != "answerable":
            continue

        intent = intent_from_dict(case["intent"])
        params = case.get("params", {})
        for sql in case["accepted_sql"]:
            report["accepted_sql_total"] += 1
            result = guard.validate(sql, params, intent=intent)
            if result.valid and result.stats.intent is not None and result.stats.intent.matched:
                report["accepted_sql_passed"] += 1
            else:
                report["failures"].append(
                    f"{case['case_id']}: accepted SQL did not match intent"
                )

        for mutation in case.get("mutations", []):
            result = guard.validate(
                mutation["sql"], mutation.get("params", params), intent=intent
            )
            mutation_class = mutation["class"]
            class_report = report["mutation_classes"].setdefault(
                mutation_class, {"total": 0, "detected": 0, "known_gap": 0}
            )
            class_report["total"] += 1
            codes = {violation.code.value for violation in result.violations}
            if mutation["expected_guard"] == "blocked":
                report["deterministic_mutations_total"] += 1
                expected_codes = set(mutation.get("expected_codes", []))
                detected = not result.valid and expected_codes <= codes
                if detected:
                    report["deterministic_mutations_detected"] += 1
                    class_report["detected"] += 1
                else:
                    report["failures"].append(
                        f"{case['case_id']}/{mutation['mutation_id']}: mutation was not blocked as expected"
                    )
            else:
                report["known_gaps_total"] += 1
                class_report["known_gap"] += 1
                if result.valid:
                    report["known_gaps_observed"] += 1
                else:
                    report["failures"].append(
                        f"{case['case_id']}/{mutation['mutation_id']}: known-gap baseline changed"
                    )

            execution = case.get("execution_oracle")
            if execution is None:
                continue
            per_instance: list[dict[str, bool]] = []
            accepted_sql = case["accepted_sql"][0]
            mutation_params = mutation.get("params", params)
            for instance_id in execution["instance_ids"]:
                setup_sql = corpus.instances[instance_id]
                expected_rows = _execute(setup_sql, accepted_sql, params)
                actual_rows = _execute(setup_sql, mutation["sql"], mutation_params)
                comparisons = {
                    comparator: compare_rows(expected_rows, actual_rows, comparator)
                    for comparator in execution["comparators"]
                }
                report["execution_pairs"] += 1
                if comparisons.get("set") and comparisons.get("multiset") is False:
                    report["set_only_matches"] += 1
                per_instance.append(comparisons)
            if (
                len(per_instance) > 1
                and per_instance[0].get("multiset") is True
                and any(item.get("multiset") is False for item in per_instance[1:])
            ):
                report["data_only_coincidences"] += 1

    report["adjudication"] = dict(report["adjudication"])
    report["answerability"] = dict(report["answerability"])
    total = report["accepted_sql_total"]
    report["accepted_sql_recall"] = report["accepted_sql_passed"] / max(total, 1)
    mutations = report["deterministic_mutations_total"]
    report["deterministic_mutation_recall"] = (
        report["deterministic_mutations_detected"] / max(mutations, 1)
    )
    return report


def main() -> int:
    report = evaluate_semantic()
    print(json.dumps(report, indent=2, sort_keys=True))
    passed = (
        report["accepted_sql_recall"] == 1.0
        and report["deterministic_mutation_recall"] == 1.0
        and report["known_gaps_observed"] == report["known_gaps_total"]
        and report["set_only_matches"] >= 1
        and report["data_only_coincidences"] >= 1
        and not report["failures"]
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
