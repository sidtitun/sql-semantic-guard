"""Run the frozen acceptance corpus: ``python -m evals.run``."""

from __future__ import annotations

import json

from evals.corpus import (
    CASES,
    MAX_FALSE_BLOCK_RATE,
    MIN_SAFE_CASES,
    MIN_UNSAFE_CASES,
    TARGET_REWRITE_IDEMPOTENCE,
    TARGET_SAFE_RECALL,
    TARGET_UNSAFE_REJECTION,
    build_guard,
)


def evaluate() -> dict[str, int | float | list[str]]:
    guard = build_guard()
    safe = [case for case in CASES if case.expected_valid]
    unsafe = [case for case in CASES if not case.expected_valid]
    accepted = 0
    rejected = 0
    stable = 0
    valid_ids: list[str] = []
    failures: list[str] = []

    for case in CASES:
        result = guard.validate(case.sql, params=case.params)
        if case.expected_valid:
            if result.valid and result.sql is not None:
                accepted += 1
                valid_ids.append(case.case_id)
                replay = guard.validate(result.sql, params=case.params)
                if replay.valid and replay.sql == result.sql:
                    stable += 1
                else:
                    failures.append(f"{case.case_id}: rewritten output is not idempotent")
            else:
                failures.append(f"{case.case_id}: expected valid, got {result.codes}")
        elif not result.valid and result.sql is None:
            rejected += 1
        else:
            failures.append(f"{case.case_id}: unsafe query was allowed")

    return {
        "safe_cases": len(safe),
        "unsafe_cases": len(unsafe),
        "safe_query_recall": accepted / len(safe),
        "false_block_rate": (len(safe) - accepted) / len(safe),
        "unsafe_query_rejection": rejected / len(unsafe),
        "rewrite_idempotence": stable / max(accepted, 1),
        "valid_case_ids": valid_ids,
        "failures": failures,
    }


def main() -> int:
    report = evaluate()
    print(json.dumps(report, indent=2, sort_keys=True))
    enough_cases = (
        report["safe_cases"] >= MIN_SAFE_CASES
        and report["unsafe_cases"] >= MIN_UNSAFE_CASES
    )
    passed = (
        enough_cases
        and report["safe_query_recall"] >= TARGET_SAFE_RECALL
        and report["false_block_rate"] <= MAX_FALSE_BLOCK_RATE
        and report["unsafe_query_rejection"] >= TARGET_UNSAFE_REJECTION
        and report["rewrite_idempotence"] >= TARGET_REWRITE_IDEMPOTENCE
        and not report["failures"]
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
