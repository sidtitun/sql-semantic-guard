"""Deterministic, dialect-aware function arity validation."""

from __future__ import annotations

import pytest

from sqlguard import Catalog, Code, Policy, SQLGuard, ValidationResult, Violation


def make_guard(dialect: str = "postgres", **policy: object) -> SQLGuard:
    return SQLGuard(
        Catalog.from_dict(
            {
                "events": {
                    "created_at": "timestamp",
                    "name": "text",
                    "amount": "decimal",
                    "tags": "array<string>",
                }
            }
        ),
        Policy(default_limit=None, on_missing_stats="ignore", **policy),
        dialect=dialect,
    )


def misuse(result: ValidationResult) -> list[Violation]:
    return [v for v in result.violations if v.code is Code.FUNCTION_MISUSE]


def test_postgres_date_trunc_missing_unit_is_blocked() -> None:
    result = make_guard().validate("SELECT date_trunc(created_at) FROM events")

    assert not result.valid
    violation = next(v for v in result.errors if v.code is Code.FUNCTION_MISUSE)
    assert violation.extra["actual_arity"] == 1
    assert violation.extra["failure"] == "arity"
    assert violation.hint == "Expected: date_trunc(unit, timestamp[, time_zone])"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT date_trunc('day', created_at) FROM events",
        "SELECT split_part(name, '.', 2) FROM events",
        "SELECT substring(name, 1) FROM events",
        "SELECT substring(name, 1, 3) FROM events",
        "SELECT coalesce(name, 'unknown', 'n/a') FROM events",
        "SELECT count(*) FROM events",
        "SELECT round(amount, 2) FROM events",
        "SELECT date_trunc('day', created_at, 'UTC') FROM events",
    ],
)
def test_valid_postgres_calls_pass_signature_check(sql: str) -> None:
    result = make_guard().validate(sql)
    assert not misuse(result)


@pytest.mark.parametrize(
    ("sql", "function"),
    [
        ("SELECT split_part(name, '.') FROM events", "split_part"),
        ("SELECT replace(name, 'x') FROM events", "replace"),
        ("SELECT coalesce() FROM events", "coalesce"),
        ("SELECT lower() FROM events", "lower"),
    ],
)
def test_invalid_shared_and_postgres_arities_are_blocked(sql: str, function: str) -> None:
    result = make_guard().validate(sql)
    violations = misuse(result)
    assert len(violations) == 1
    assert violations[0].extra["function"] == function


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT date_parse(name, '%Y-%m-%d') FROM events",
        "SELECT date_trunc('day', created_at) FROM events",
        "SELECT cardinality(tags) FROM events",
        "SELECT array_join(tags, ',', 'NULL') FROM events",
        "SELECT regexp_like(name, '^[a-z]+$') FROM events",
        "SELECT max(amount, 3) FROM events",
        "SELECT format_datetime(created_at, 'yyyy-MM-dd') FROM events",
    ],
)
def test_valid_athena_calls_pass_signature_check(sql: str) -> None:
    result = make_guard("athena", require_partition_filter=False).validate(sql)
    assert not misuse(result)


def test_athena_surface_name_is_used_in_feedback() -> None:
    result = make_guard("athena", require_partition_filter=False).validate(
        "SELECT date_parse(name) FROM events"
    )

    violation = next(v for v in result.errors if v.code is Code.FUNCTION_MISUSE)
    assert violation.extra["function"] == "date_parse"
    assert violation.hint == "Expected: date_parse(string, format)"


def test_unknown_udf_is_not_treated_as_wrong() -> None:
    result = make_guard().validate("SELECT company_score(name, amount) FROM events")
    assert not misuse(result)


def test_signature_check_can_be_disabled() -> None:
    result = make_guard(check_function_signatures=False).validate(
        "SELECT date_trunc(created_at) FROM events"
    )
    assert not misuse(result)
    assert "function_signature_checks" in result.stats.checks_skipped


def test_forbidden_function_does_not_also_report_misuse() -> None:
    result = make_guard(function_denylist=frozenset({"lower"})).validate(
        "SELECT lower(name) FROM events"
    )

    assert any(v.code is Code.FORBIDDEN_FUNCTION for v in result.errors)
    assert not misuse(result)
