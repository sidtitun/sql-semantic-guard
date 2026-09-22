"""Expression-based row-level security rules."""

from __future__ import annotations

import pytest
from sqlglot import exp, parse_one

from sqlguard import Code, Policy, RewriteKind, RLSRule, SQLGuard
from sqlguard.errors import PolicyError


def _guard(catalog, predicate: str, **policy_kwargs) -> SQLGuard:
    return SQLGuard(
        catalog,
        Policy(
            rls=[RLSRule(table="customers", predicate=predicate)],
            default_limit=None,
            on_missing_stats="ignore",
            **policy_kwargs,
        ),
        dialect="postgres",
    )


def test_multi_condition_expression_is_injected(catalog):
    guard = _guard(catalog, "region IN :regions AND tier <> :blocked")

    result = guard.validate(
        "SELECT id FROM customers",
        params={"regions": ["US", "EU"], "blocked": "suspended"},
    )

    assert result.valid
    assert "customers.region IN ('US', 'EU')" in result.sql
    assert "customers.tier <> 'suspended'" in result.sql
    assert any(r.kind == RewriteKind.RLS_FILTER_ADDED for r in result.rewrites)


def test_expression_columns_are_qualified_with_query_alias(catalog):
    guard = _guard(catalog, "region = :region")

    result = guard.validate(
        "SELECT c.id FROM customers AS c", params={"region": "APAC"}
    )

    assert result.valid
    assert "c.region = 'APAC'" in result.sql
    assert "customers.region" not in result.sql


def test_list_parameter_expands_inside_parenthesized_in(catalog):
    guard = _guard(catalog, "region IN (:regions)")

    result = guard.validate(
        "SELECT id FROM customers", params={"regions": ("US", "EU")}
    )

    assert result.valid
    assert "region IN ('US', 'EU')" in result.sql


def test_scalar_parameter_in_in_becomes_single_item(catalog):
    guard = _guard(catalog, "region IN :regions")

    result = guard.validate("SELECT id FROM customers", params={"regions": "US"})

    assert result.valid
    assert "region IN ('US')" in result.sql


def test_set_parameter_has_deterministic_order(catalog):
    guard = _guard(catalog, "region IN :regions")

    result = guard.validate(
        "SELECT id FROM customers", params={"regions": {"US", "EU"}}
    )

    assert result.valid
    assert "region IN ('EU', 'US')" in result.sql


def test_missing_one_of_multiple_parameters_fails_closed(catalog):
    guard = _guard(catalog, "region = :region AND tier = :tier")

    result = guard.validate("SELECT id FROM customers", params={"region": "US"})

    assert not result.valid
    assert result.sql is None
    assert any(
        error.code == Code.RLS_PARAM_MISSING and "tier" in error.message
        for error in result.errors
    )


def test_empty_list_parameter_fails_closed(catalog):
    guard = _guard(catalog, "region IN :regions")

    result = guard.validate("SELECT id FROM customers", params={"regions": []})

    assert not result.valid
    assert any(error.code == Code.RLS_CONFIG_ERROR for error in result.errors)


def test_list_parameter_outside_in_fails_closed(catalog):
    guard = _guard(catalog, "region = :region")

    result = guard.validate("SELECT id FROM customers", params={"region": ["US"]})

    assert not result.valid
    assert any(error.code == Code.RLS_CONFIG_ERROR for error in result.errors)


def test_expression_is_not_duplicated_when_already_present(catalog):
    guard = _guard(catalog, "region IN :regions AND tier <> :blocked")

    result = guard.validate(
        "SELECT id FROM customers "
        "WHERE region IN ('US', 'EU') AND tier <> 'suspended'",
        params={"regions": ["US", "EU"], "blocked": "suspended"},
    )

    assert result.valid
    assert result.sql.count("region IN ('US', 'EU')") == 1
    assert any(r.kind == RewriteKind.RLS_FILTER_PRESENT for r in result.rewrites)


def test_left_join_expression_goes_into_on_clause(catalog):
    guard = _guard(catalog, "region = :region AND tier <> :blocked")

    result = guard.validate(
        "SELECT o.id, c.name FROM orders o "
        "LEFT JOIN customers c ON c.id = o.customer_id",
        params={"region": "US", "blocked": "suspended"},
    )

    assert result.valid
    tree = parse_one(result.sql, read="postgres")
    join = tree.find(exp.Join)
    assert "c.region = 'US'" in join.args["on"].sql()
    assert "c.tier <> 'suspended'" in join.args["on"].sql()
    where = tree.find(exp.Where)
    assert where is None or "c.region" not in where.sql()


def test_subquery_strategy_wraps_with_expression(catalog):
    guard = _guard(
        catalog,
        "region = :region AND tier <> :blocked",
        rls_strategy="subquery",
    )

    result = guard.validate(
        "SELECT id FROM customers",
        params={"region": "US", "blocked": "suspended"},
    )

    assert result.valid
    assert "SELECT * FROM customers" in result.sql
    assert "customers.region = 'US'" in result.sql


def test_expression_applies_to_each_self_join_reference(catalog):
    guard = _guard(catalog, "region = :region")

    result = guard.validate(
        "SELECT a.id FROM customers a JOIN customers b ON a.id = b.id",
        params={"region": "US"},
    )

    assert result.valid
    assert "a.region = 'US'" in result.sql
    assert "b.region = 'US'" in result.sql


def test_parameterized_expression_preserves_named_placeholder(catalog):
    guard = _guard(catalog, "region = :region", rls_parameterize=True)

    result = guard.validate("SELECT id FROM customers")

    assert result.valid
    assert "customers.region = %(region)s" in result.sql


def test_parameterless_expression_is_supported(catalog):
    guard = _guard(catalog, "region IS NOT NULL")

    result = guard.validate("SELECT id FROM customers")

    assert result.valid
    assert "customers.region IS NOT NULL" in result.sql


def test_unknown_predicate_column_is_policy_error_at_build(catalog):
    with pytest.raises(PolicyError, match="missing on matched table"):
        _guard(catalog, "deleted_at IS NULL")


def test_expression_require_strategy_is_rejected(catalog):
    with pytest.raises(PolicyError, match="require"):
        _guard(catalog, "region = :region", rls_strategy="require")


def test_forbidden_function_in_expression_is_policy_error(catalog):
    with pytest.raises(PolicyError, match="blocked by policy"):
        _guard(catalog, "pg_sleep(1) IS NULL")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"column": "customer_id", "predicate": "region = :region"},
        {"predicate": "other.region = :region"},
        {"predicate": "region = :region; DELETE FROM customers"},
        {"predicate": "region IN (SELECT region FROM customers)"},
        {"predicate": "COUNT(*) > 0"},
        {"predicate": "region = ?"},
        {"predicate": "region = :region", "param": "tenant"},
    ],
)
def test_invalid_expression_rule_configuration(kwargs):
    with pytest.raises(PolicyError):
        RLSRule(table="customers", **kwargs)


def test_wildcard_expression_can_skip_tables_missing_columns(catalog):
    guard = SQLGuard(
        catalog,
        Policy(
            rls=[
                RLSRule(
                    table="*",
                    predicate="region = :region",
                    on_missing_column="skip",
                )
            ],
            default_limit=None,
            on_missing_stats="ignore",
        ),
    )

    result = guard.validate("SELECT id FROM orders", params={"region": "US"})

    assert result.valid
    assert "region" not in result.sql


def test_expression_rls_renders_for_athena(athena_catalog):
    guard = SQLGuard(
        athena_catalog,
        Policy(
            rls=[
                RLSRule(
                    table="analytics.events",
                    predicate="event_type IN :types AND tenant_id = :tenant",
                )
            ],
            default_limit=None,
            on_missing_stats="ignore",
            require_partition_filter=False,
        ),
        dialect="athena",
    )

    result = guard.validate(
        "SELECT event_id FROM analytics.events",
        params={"types": ["click", "view"], "tenant": 7},
    )

    assert result.valid
    assert "events.event_type IN ('click', 'view')" in result.sql
    assert "events.tenant_id = 7" in result.sql
