"""Aggregation rules catch runtime SQL errors before a query reaches an engine."""

from sqlguard import Catalog, Policy, SQLGuard
from sqlguard.violations import Code

CATALOG = Catalog.from_dict(
    {
        "orders": {
            "columns": {
                "id": "bigint",
                "status": "text",
                "amount": "decimal(10,2)",
                "customer_id": "bigint",
            },
            "primary_key": ["id"],
        }
    }
)


def _validate(sql: str, dialect: str = "postgres", **policy: object):
    guard = SQLGuard(
        CATALOG,
        Policy(on_missing_stats="ignore", default_limit=None, **policy),
        dialect=dialect,
    )
    return guard.validate(sql)


def test_mixed_bare_column_and_aggregate_requires_group_by() -> None:
    result = _validate("SELECT status, SUM(amount) FROM orders")

    assert not result.valid
    violation = next(v for v in result.errors if v.code is Code.GROUP_BY_VIOLATION)
    assert violation.column == "status"
    assert "GROUP BY" in (violation.hint or "")


def test_explicit_group_by_is_clean() -> None:
    result = _validate("SELECT status, SUM(amount) FROM orders GROUP BY status")
    assert result.valid


def test_group_by_ordinal_and_alias_are_resolved() -> None:
    ordinal = _validate("SELECT status, SUM(amount) FROM orders GROUP BY 1")
    alias = _validate("SELECT status AS state, SUM(amount) FROM orders GROUP BY state")

    assert ordinal.valid
    assert alias.valid


def test_postgres_primary_key_functional_dependency() -> None:
    postgres = _validate("SELECT id, status, SUM(amount) FROM orders GROUP BY id")
    athena = _validate(
        "SELECT id, status, SUM(amount) FROM orders GROUP BY id", dialect="athena"
    )

    assert postgres.valid
    assert not athena.valid
    assert any(v.code is Code.GROUP_BY_VIOLATION for v in athena.errors)


def test_window_function_does_not_create_grouping_context() -> None:
    result = _validate("SELECT status, SUM(amount) OVER (PARTITION BY status) FROM orders")
    assert result.valid


def test_aggregate_in_where_is_rejected_with_having_hint() -> None:
    result = _validate("SELECT status FROM orders WHERE SUM(amount) > 10 GROUP BY status")

    assert not result.valid
    violation = next(v for v in result.errors if v.code is Code.AGGREGATE_IN_WHERE)
    assert violation.hint == "Move aggregate filters to HAVING."


def test_aggregate_in_join_condition_is_rejected() -> None:
    result = _validate(
        "SELECT o.id FROM orders o JOIN orders x ON SUM(o.amount) = x.amount GROUP BY o.id"
    )
    assert any(v.code is Code.AGGREGATE_IN_WHERE for v in result.errors)


def test_having_aggregate_is_clean_but_bare_column_is_checked() -> None:
    clean = _validate(
        "SELECT status, COUNT(*) FROM orders GROUP BY status HAVING COUNT(*) > 1"
    )
    bad = _validate(
        "SELECT status, COUNT(*) FROM orders GROUP BY status HAVING customer_id > 1"
    )

    assert clean.valid
    assert any(v.code is Code.GROUP_BY_VIOLATION for v in bad.errors)


def test_cte_scopes_are_checked_independently() -> None:
    result = _validate(
        "WITH totals AS (SELECT status, SUM(amount) AS total FROM orders) "
        "SELECT status, total FROM totals"
    )
    assert any(v.code is Code.GROUP_BY_VIOLATION for v in result.errors)


def test_check_can_be_disabled_for_compatibility() -> None:
    result = _validate(
        "SELECT status, SUM(amount) FROM orders",
        check_aggregation=False,
    )
    assert result.valid
    assert "aggregation_checks" in result.stats.checks_skipped
