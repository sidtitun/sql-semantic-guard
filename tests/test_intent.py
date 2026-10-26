"""Independent QueryIntent checks catch semantically wrong but valid SQL."""

from __future__ import annotations

import pytest

from sqlguard import (
    DimensionIntent,
    FilterIntent,
    MeasureIntent,
    OrderIntent,
    Policy,
    PolicyOverlay,
    PolicySet,
    QueryIntent,
    SQLGuard,
)
from sqlguard.errors import PolicyError, ValidationFailed


def _guard(catalog, enforcement: str = "block", *, default_limit=None) -> SQLGuard:
    return SQLGuard(
        catalog,
        Policy(
            default_limit=default_limit,
            on_missing_stats="ignore",
            intent_enforcement=enforcement,
        ),
        dialect="postgres",
    )


def _revenue_intent() -> QueryIntent:
    return QueryIntent(
        measures=[MeasureIntent("sum", "amount", alias="revenue")],
        dimensions=[DimensionIntent("created_at", alias="month", time_grain="month")],
        filters=[FilterIntent("status", values=("paid",))],
        ordering=[OrderIntent("revenue", "desc")],
        limit=5,
        expected_grain=["month"],
    )


def test_complete_intent_matches_final_sql(catalog):
    result = _guard(catalog).validate(
        """
        SELECT DATE_TRUNC('month', created_at) AS month,
               SUM(amount) AS revenue
        FROM orders
        WHERE status = :status
        GROUP BY 1
        ORDER BY revenue DESC
        LIMIT 5
        """,
        {"status": "paid"},
        intent=_revenue_intent(),
    )

    assert result.valid
    assert result.stats.intent is not None
    assert result.stats.intent.matched
    assert len(result.stats.intent.confirmed) == 6
    assert "intent_checks" in result.stats.checks_run


@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        (
            "SELECT DATE_TRUNC('month', created_at) AS month, SUM(amount) AS revenue "
            "FROM orders GROUP BY 1 ORDER BY revenue DESC LIMIT 5",
            "filter:status:eq",
        ),
        (
            "SELECT DATE_TRUNC('month', created_at) AS month, AVG(amount) AS revenue "
            "FROM orders WHERE status = :status GROUP BY 1 ORDER BY revenue DESC LIMIT 5",
            "measure:revenue",
        ),
        (
            "SELECT DATE_TRUNC('week', created_at) AS month, SUM(amount) AS revenue "
            "FROM orders WHERE status = :status GROUP BY 1 ORDER BY revenue DESC LIMIT 5",
            "dimension:month",
        ),
        (
            "SELECT DATE_TRUNC('month', created_at) AS month, SUM(amount) AS revenue "
            "FROM orders WHERE status = :status GROUP BY 1 ORDER BY revenue ASC LIMIT 5",
            "order:revenue:desc",
        ),
        (
            "SELECT DATE_TRUNC('month', created_at) AS month, SUM(amount) AS revenue "
            "FROM orders WHERE status = :status GROUP BY 1 ORDER BY revenue DESC LIMIT 10",
            "limit:5",
        ),
    ],
)
def test_semantic_mutations_are_blocked(catalog, sql, constraint):
    result = _guard(catalog).validate(
        sql, {"status": "paid"}, intent=_revenue_intent()
    )

    assert not result.valid
    assert result.stats.intent is not None
    assert constraint in result.stats.intent.missing + result.stats.intent.conflicting


def test_filter_value_conflict_does_not_leak_values(catalog):
    result = _guard(catalog).validate(
        "SELECT id FROM orders WHERE status = :status",
        {"status": "cancelled-secret"},
        intent=QueryIntent(filters=[FilterIntent("status", values=("paid-secret",))]),
    )

    assert not result.valid
    payload = str(result.to_dict()) + result.feedback()
    assert "cancelled-secret" not in payload
    assert "paid-secret" not in payload
    assert result.violations[0].code.value == "intent_constraint_conflict"


def test_distinct_count_and_grouping_are_checked(catalog):
    intent = QueryIntent(
        measures=[MeasureIntent("count", "customer_id", alias="buyers", distinct=True)],
        dimensions=[DimensionIntent("status")],
        distinct=False,
    )
    matching = _guard(catalog).validate(
        "SELECT status, COUNT(DISTINCT customer_id) AS buyers "
        "FROM orders GROUP BY status",
        intent=intent,
    )
    missing_group = _guard(catalog).validate(
        "SELECT status, COUNT(DISTINCT customer_id) AS buyers FROM orders",
        intent=intent,
    )

    assert matching.valid
    assert not missing_group.valid
    assert "dimension:status" in missing_group.stats.intent.conflicting


def test_count_star_and_select_distinct(catalog):
    result = _guard(catalog).validate(
        "SELECT DISTINCT COUNT(*) AS rows FROM orders",
        intent=QueryIntent(
            measures=[MeasureIntent("count", alias="rows")], distinct=True
        ),
    )

    assert result.valid
    assert result.stats.intent.matched


@pytest.mark.parametrize(
    ("predicate", "filter_intent", "params"),
    [
        (
            "status IN (:statuses)",
            FilterIntent("status", "in", ("paid", "pending")),
            {"statuses": ["paid", "pending"]},
        ),
        (
            "amount BETWEEN :low AND :high",
            FilterIntent("amount", "between", (10, 20)),
            {"low": 10, "high": 20},
        ),
        ("notes IS NULL", FilterIntent("notes", "is_null"), {}),
        ("notes IS NOT NULL", FilterIntent("notes", "not_null"), {}),
    ],
)
def test_supported_filter_shapes(catalog, predicate, filter_intent, params):
    result = _guard(catalog).validate(
        f"SELECT id FROM orders WHERE {predicate}",
        params,
        intent=QueryIntent(filters=[filter_intent]),
    )

    assert result.valid
    assert result.stats.intent.matched


def test_physical_table_identity_matches_a_sql_alias(catalog):
    result = _guard(catalog).validate(
        "SELECT o.id FROM orders AS o WHERE o.status = :status",
        {"status": "paid"},
        intent=QueryIntent(
            dimensions=[DimensionIntent("id", table="orders")],
            filters=[FilterIntent("status", values=("paid",), table="orders")],
        ),
    )

    assert result.valid
    assert result.stats.intent.matched


def test_warning_off_and_block_modes_are_explicit(catalog):
    intent = QueryIntent(filters=[FilterIntent("status", values=("paid",))])
    sql = "SELECT id FROM orders"

    warning = _guard(catalog, "warn").validate(sql, intent=intent)
    off = _guard(catalog, "off").validate(sql, intent=intent)
    blocked = _guard(catalog, "block").validate(sql, intent=intent)

    assert warning.valid and warning.warnings and not warning.errors
    assert warning.stats.intent is not None and not warning.stats.intent.matched
    assert off.valid and off.stats.intent is None
    assert "intent_checks" in off.stats.checks_skipped
    assert not blocked.valid and blocked.errors


def test_unsupported_outer_shape_is_never_reported_as_a_match(catalog):
    sql = "SELECT id FROM orders UNION SELECT id FROM orders"
    intent = QueryIntent(dimensions=[DimensionIntent("id")])

    blocked = _guard(catalog).validate(sql, intent=intent)
    warning = _guard(catalog, "warn").validate(sql, intent=intent)

    assert not blocked.valid
    assert blocked.stats.intent.undecidable == ["outer_query_shape"]
    assert any(v.code.value == "intent_undecidable" for v in blocked.errors)
    assert warning.valid
    assert any(v.code.value == "intent_undecidable" for v in warning.warnings)


def test_default_limit_is_compared_after_safe_rewrites(catalog):
    result = _guard(catalog, default_limit=5).validate(
        "SELECT id FROM orders", intent=QueryIntent(limit=5)
    )

    assert result.valid
    assert result.stats.intent.matched
    assert result.stats.intent.observed["limit"] == 5


def test_validate_or_raise_accepts_intent(catalog):
    with pytest.raises(ValidationFailed):
        _guard(catalog).validate_or_raise(
            "SELECT id FROM orders",
            intent=QueryIntent(filters=[FilterIntent("status", values=("paid",))]),
        )


def test_role_overlay_may_only_tighten_intent_enforcement():
    resolved = PolicySet(
        Policy(intent_enforcement="warn"),
        {"strict": PolicyOverlay(intent_enforcement="block")},
    ).resolve("strict")
    assert resolved.intent_enforcement == "block"

    with pytest.raises(PolicyError, match="would loosen"):
        PolicySet(
            Policy(intent_enforcement="block"),
            {"weak": PolicyOverlay(intent_enforcement="warn")},
        )


def test_intent_serialization_is_private_by_default():
    intent = QueryIntent(filters=[FilterIntent("status", values=("private",))])

    assert "values" not in intent.to_dict()["filters"][0]
    assert intent.to_dict()["filters"][0]["value_count"] == 1
    assert intent.to_dict(include_values=True)["filters"][0]["values"] == ["private"]


def test_invalid_intent_contracts_fail_at_construction():
    with pytest.raises(TypeError, match="wrap a scalar"):
        FilterIntent("status", values="paid")
    with pytest.raises(TypeError, match="sequence of field names"):
        QueryIntent(expected_grain="status")
    with pytest.raises(ValueError, match="2 value"):
        FilterIntent("amount", "between", (10,))
