"""Versioned corpus model for intent fidelity and semantic evaluation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlguard import (
    Catalog,
    DimensionIntent,
    FilterIntent,
    MeasureIntent,
    OrderIntent,
    Policy,
    QueryIntent,
    SQLGuard,
)

ROOT = Path(__file__).parent / "semantic"
CORPUS_PATH = ROOT / "corpus-v1.json"
INSTANCES_PATH = ROOT / "instances-v1.json"
SCHEMA_PATH = ROOT / "schema-v1.json"

ANSWERABILITY = frozenset({"answerable", "ambiguous", "unanswerable"})
ADJUDICATION = frozenset({"accepted", "disputed", "excluded"})
EXPECTED_GUARD = frozenset({"blocked", "known_gap"})
COMPARATORS = frozenset({"ordered", "set", "multiset"})


@dataclass(frozen=True)
class SemanticCorpus:
    schema_version: int
    corpus_id: str
    oracle: dict[str, str]
    cases: tuple[dict[str, Any], ...]
    instances: dict[str, tuple[str, ...]]


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return value


def _validate_case(case: Any, seen: set[str]) -> dict[str, Any]:
    if not isinstance(case, dict):
        raise ValueError("every semantic corpus case must be an object")
    case_id = _require_string(case.get("case_id"), "case_id")
    if case_id in seen:
        raise ValueError(f"duplicate semantic case_id: {case_id}")
    seen.add(case_id)
    _require_string(case.get("question"), f"{case_id}.question")
    answerability = case.get("answerability")
    if answerability not in ANSWERABILITY:
        raise ValueError(f"{case_id}.answerability must be one of {sorted(ANSWERABILITY)}")

    adjudication = case.get("adjudication")
    if not isinstance(adjudication, dict) or adjudication.get("status") not in ADJUDICATION:
        raise ValueError(f"{case_id}.adjudication.status is invalid")
    reviewers = adjudication.get("reviewer_count")
    if not isinstance(reviewers, int) or reviewers < 1:
        raise ValueError(f"{case_id}.adjudication.reviewer_count must be positive")
    _require_string(adjudication.get("rationale"), f"{case_id}.adjudication.rationale")
    _require_string(adjudication.get("source"), f"{case_id}.adjudication.source")

    ambiguities = case.get("ambiguities", [])
    if not isinstance(ambiguities, list):
        raise ValueError(f"{case_id}.ambiguities must be a list")
    issue_ids: set[str] = set()
    for issue in ambiguities:
        if not isinstance(issue, dict):
            raise ValueError(f"{case_id}.ambiguities entries must be objects")
        issue_id = _require_string(issue.get("issue_id"), f"{case_id}.ambiguity.issue_id")
        if issue_id in issue_ids:
            raise ValueError(f"{case_id} has duplicate ambiguity issue {issue_id}")
        issue_ids.add(issue_id)
        if issue.get("lifecycle") not in {"planned", "asked", "answered", "dropped"}:
            raise ValueError(f"{case_id}.{issue_id}.lifecycle is invalid")

    if answerability == "answerable" and adjudication.get("status") == "accepted":
        if not isinstance(case.get("intent"), dict):
            raise ValueError(f"{case_id}.intent is required")
        accepted = case.get("accepted_sql")
        if not isinstance(accepted, list) or not accepted:
            raise ValueError(f"{case_id}.accepted_sql must not be empty")
        for index, sql in enumerate(accepted):
            _require_string(sql, f"{case_id}.accepted_sql[{index}]")
    elif case.get("accepted_sql"):
        raise ValueError(f"{case_id} cannot have accepted_sql for this outcome")

    mutation_ids: set[str] = set()
    for mutation in case.get("mutations", []):
        if not isinstance(mutation, dict):
            raise ValueError(f"{case_id}.mutations entries must be objects")
        mutation_id = _require_string(
            mutation.get("mutation_id"), f"{case_id}.mutation_id"
        )
        if mutation_id in mutation_ids:
            raise ValueError(f"{case_id} has duplicate mutation {mutation_id}")
        mutation_ids.add(mutation_id)
        _require_string(mutation.get("class"), f"{case_id}.{mutation_id}.class")
        _require_string(mutation.get("sql"), f"{case_id}.{mutation_id}.sql")
        if mutation.get("expected_guard") not in EXPECTED_GUARD:
            raise ValueError(f"{case_id}.{mutation_id}.expected_guard is invalid")

    execution = case.get("execution_oracle")
    if execution is not None:
        if not isinstance(execution, dict):
            raise ValueError(f"{case_id}.execution_oracle must be an object")
        comparators = execution.get("comparators", [])
        if not comparators or not set(comparators) <= COMPARATORS:
            raise ValueError(f"{case_id}.execution_oracle.comparators is invalid")
        if not execution.get("instance_ids"):
            raise ValueError(f"{case_id}.execution_oracle.instance_ids must not be empty")
    return case


def load_semantic_corpus(
    corpus_path: Path = CORPUS_PATH,
    instances_path: Path = INSTANCES_PATH,
) -> SemanticCorpus:
    """Load and validate the checked-in v1 semantic corpus."""
    raw = json.loads(corpus_path.read_text(encoding="utf-8"))
    instance_raw = json.loads(instances_path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1 or instance_raw.get("schema_version") != 1:
        raise ValueError("unsupported semantic corpus schema version")
    corpus_id = _require_string(raw.get("corpus_id"), "corpus_id")
    oracle = raw.get("oracle")
    if not isinstance(oracle, dict):
        raise ValueError("oracle must be an object")
    for field in ("id", "revision", "adjudication_source"):
        _require_string(oracle.get(field), f"oracle.{field}")
    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list):
        raise ValueError("cases must be a list")
    seen: set[str] = set()
    cases = tuple(_validate_case(case, seen) for case in cases_raw)

    instances: dict[str, tuple[str, ...]] = {}
    for item in instance_raw.get("instances", []):
        if not isinstance(item, dict):
            raise ValueError("database instances must be objects")
        instance_id = _require_string(item.get("instance_id"), "instance_id")
        if instance_id in instances:
            raise ValueError(f"duplicate database instance: {instance_id}")
        statements = item.get("setup_sql")
        if not isinstance(statements, list) or not statements:
            raise ValueError(f"{instance_id}.setup_sql must not be empty")
        instances[instance_id] = tuple(
            _require_string(statement, f"{instance_id}.setup_sql")
            for statement in statements
        )
    for case in cases:
        execution = case.get("execution_oracle")
        if execution:
            missing = set(execution["instance_ids"]) - set(instances)
            if missing:
                raise ValueError(f"{case['case_id']} references missing instances: {missing}")
    return SemanticCorpus(1, corpus_id, dict(oracle), cases, instances)


def intent_from_dict(data: dict[str, Any]) -> QueryIntent:
    """Decode the JSON representation used by the evaluation corpus."""
    return QueryIntent(
        measures=[MeasureIntent(**item) for item in data.get("measures", [])],
        dimensions=[DimensionIntent(**item) for item in data.get("dimensions", [])],
        filters=[FilterIntent(**item) for item in data.get("filters", [])],
        ordering=[OrderIntent(**item) for item in data.get("ordering", [])],
        distinct=data.get("distinct"),
        limit=data.get("limit"),
        expected_grain=data.get("expected_grain", []),
    )


def build_semantic_guard() -> SQLGuard:
    """Guard used by the public semantic smoke corpus."""
    catalog = Catalog.from_dict(
        {
            "orders": {
                "columns": {
                    "id": "bigint",
                    "customer_id": "bigint",
                    "amount": "decimal(10,2)",
                    "status": "varchar(32)",
                    "created_at": "timestamp",
                },
                "foreign_keys": [
                    {
                        "columns": ["customer_id"],
                        "ref_table": "customers",
                        "ref_columns": ["id"],
                    }
                ],
            },
            "customers": {"columns": {"id": "bigint", "name": "text"}},
            "line_items": {
                "columns": {"id": "bigint", "order_id": "bigint", "quantity": "integer"},
                "foreign_keys": [
                    {"columns": ["order_id"], "ref_table": "orders", "ref_columns": ["id"]}
                ],
            },
        }
    )
    return SQLGuard(
        catalog,
        Policy(
            default_limit=None,
            on_missing_stats="ignore",
            intent_enforcement="block",
        ),
        dialect="postgres",
    )
