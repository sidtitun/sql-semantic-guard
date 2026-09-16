"""Derived-table stars resolve to catalog-backed output columns."""

from __future__ import annotations

import pytest

from sqlguard import Catalog, Code, Policy, SQLGuard


@pytest.fixture
def guard() -> SQLGuard:
    catalog = Catalog.from_dict(
        {
            "orders": {"id": "bigint", "customer_id": "bigint", "amount": "decimal"},
            "customers": {"id": "bigint", "name": "text"},
        }
    )
    return SQLGuard(
        catalog,
        Policy(default_limit=None, on_missing_stats="ignore"),
        dialect="postgres",
    )


def test_derived_star_rejects_unknown_output(guard: SQLGuard) -> None:
    valid = guard.validate("SELECT sub.id FROM (SELECT * FROM orders) sub")
    invalid = guard.validate("SELECT sub.amont FROM (SELECT * FROM orders) sub")

    assert valid.valid, [str(v) for v in valid.errors]
    violation = next(v for v in invalid.errors if v.code is Code.UNKNOWN_COLUMN)
    assert violation.hint is not None and "amount" in violation.hint


def test_qualified_star_merges_with_explicit_outputs(guard: SQLGuard) -> None:
    valid = guard.validate("SELECT sub.amount, sub.marker FROM "
                           "(SELECT o.*, 1 AS marker FROM orders o) sub")
    invalid = guard.validate("SELECT sub.missing FROM "
                             "(SELECT o.*, 1 AS marker FROM orders o) sub")

    assert valid.valid, [str(v) for v in valid.errors]
    assert any(v.code is Code.UNKNOWN_COLUMN for v in invalid.errors)


def test_star_over_join_and_nested_stars_resolve(guard: SQLGuard) -> None:
    joined = guard.validate(
        "SELECT j.name FROM (SELECT * FROM orders o "
        "JOIN customers c ON o.customer_id = c.id) j"
    )
    nested = guard.validate(
        "SELECT outer_q.amount FROM "
        "(SELECT * FROM (SELECT * FROM orders) inner_q) outer_q"
    )
    invalid = guard.validate(
        "SELECT outer_q.missing FROM "
        "(SELECT * FROM (SELECT * FROM orders) inner_q) outer_q"
    )

    assert joined.valid, [str(v) for v in joined.errors]
    assert nested.valid, [str(v) for v in nested.errors]
    assert any(v.code is Code.UNKNOWN_COLUMN for v in invalid.errors)


def test_opaque_table_function_does_not_create_false_positive(guard: SQLGuard) -> None:
    result = guard.validate(
        "SELECT sub.anything FROM "
        "(SELECT * FROM unnest(ARRAY[1, 2]) AS value) sub"
    )
    assert not any(v.code is Code.UNKNOWN_COLUMN for v in result.errors)


def test_recursive_cte_is_bounded(guard: SQLGuard) -> None:
    result = guard.validate(
        "WITH RECURSIVE chain AS ("
        "SELECT id FROM orders UNION ALL "
        "SELECT c.id FROM chain c JOIN orders o ON c.id = o.id"
        ") SELECT chain.id FROM chain"
    )
    assert not any(v.code is Code.INTERNAL_ERROR for v in result.errors)
