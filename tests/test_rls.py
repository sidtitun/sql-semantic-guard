"""Row-level security: the check that keeps tenant A from reading tenant B."""

from __future__ import annotations

import pytest
from sqlglot import exp, parse_one

from sqlguard import Catalog, Code, Policy, RLSRule, SQLGuard
from sqlguard.errors import PolicyError


@pytest.fixture
def rls_guard(catalog: Catalog) -> SQLGuard:
    policy = Policy(
        rls=[RLSRule(table="orders", column="customer_id", param="tenant")],
        on_missing_stats="ignore",
    )
    return SQLGuard(catalog, policy, dialect="postgres")


def test_filter_injected_on_bare_table(rls_guard):
    result = rls_guard.validate("SELECT id FROM orders", params={"tenant": 42})
    assert result.valid
    assert "customer_id = 42" in result.sql.replace('"', "")


def test_missing_param_fails_closed(rls_guard):
    """No tenant value => refuse to run, don't silently expose all rows."""
    result = rls_guard.validate("SELECT id FROM orders", params={})
    assert not result.valid
    assert any(v.code == Code.RLS_PARAM_MISSING for v in result.errors)
    assert result.sql is None


def test_none_param_fails_closed(rls_guard):
    result = rls_guard.validate("SELECT id FROM orders", params={"tenant": None})
    assert not result.valid
    assert any(v.code == Code.RLS_PARAM_MISSING for v in result.errors)


def test_filter_applied_to_every_scope(rls_guard):
    """A CTE and the outer query both reference orders: both get filtered."""
    sql = """
        WITH big AS (SELECT id, customer_id, amount FROM orders WHERE amount > 100)
        SELECT o.id FROM orders o WHERE o.status = 'shipped'
    """
    result = rls_guard.validate(sql, params={"tenant": 7})
    assert result.valid
    # Two references => the filter must appear twice.
    assert result.sql.count("customer_id = 7") == 2


def test_self_join_filters_both_instances(rls_guard):
    sql = "SELECT a.id FROM orders a JOIN orders b ON a.id = b.id"
    result = rls_guard.validate(sql, params={"tenant": 7})
    assert result.valid
    assert "a.customer_id = 7" in result.sql
    assert "b.customer_id = 7" in result.sql


def test_left_join_filter_goes_in_on_not_where(rls_guard):
    """Putting the filter in WHERE would turn the LEFT JOIN into an INNER join."""
    sql = "SELECT c.id, o.id FROM customers c LEFT JOIN orders o ON o.customer_id = c.id"
    result = rls_guard.validate(sql, params={"tenant": 7})
    assert result.valid

    tree = parse_one(result.sql, read="postgres")
    join = tree.find(exp.Join)
    assert "customer_id = 7" in join.args["on"].sql()
    where = tree.find(exp.Where)
    assert where is None or "customer_id = 7" not in where.sql()


def test_inner_join_filter_in_where(rls_guard):
    sql = "SELECT o.id FROM customers c JOIN orders o ON o.customer_id = c.id"
    result = rls_guard.validate(sql, params={"tenant": 7})
    assert result.valid
    assert "customer_id = 7" in result.sql


def test_existing_correct_filter_not_duplicated(rls_guard):
    result = rls_guard.validate(
        "SELECT id FROM orders WHERE customer_id = 7", params={"tenant": 7}
    )
    assert result.valid
    assert result.sql.count("customer_id = 7") == 1
    assert any(v for v in result.rewrites if "already present" in v.message)


def test_tenant_spoofing_blocked(rls_guard):
    """LLM (or prompt injection) tries to read another tenant: reject."""
    result = rls_guard.validate(
        "SELECT id FROM orders WHERE customer_id = 999", params={"tenant": 7}
    )
    assert not result.valid
    assert any(v.code == Code.TENANT_FILTER_CONFLICT for v in result.errors)


def test_tenant_spoofing_in_or_still_injects(rls_guard):
    """`WHERE customer_id = 7 OR TRUE` must not defeat RLS: filter is ANDed on."""
    result = rls_guard.validate(
        "SELECT id FROM orders WHERE customer_id = 7 OR 1=1", params={"tenant": 7}
    )
    assert result.valid
    # The injected top-level AND guarantees the tenant scope regardless of OR.
    tree = parse_one(result.sql, read="postgres")

    where = tree.find(exp.Where)
    assert isinstance(where.this, exp.And)
    assert "customer_id = 7" in where.this.sql()


def test_subquery_reference_filtered(rls_guard):
    sql = """
        SELECT c.id FROM customers c
        WHERE c.id IN (SELECT customer_id FROM orders WHERE amount > 100)
    """
    result = rls_guard.validate(sql, params={"tenant": 7})
    assert result.valid
    assert "customer_id = 7" in result.sql


def test_string_tenant_value(rls_guard):
    result = rls_guard.validate("SELECT id FROM orders", params={"tenant": "acme"})
    assert result.valid
    assert "'acme'" in result.sql


def test_type_coerced_existing_filter(rls_guard):
    """Existing `customer_id = '7'` matches tenant 7 (value equality, not type)."""
    result = rls_guard.validate(
        "SELECT id FROM orders WHERE customer_id = '7'", params={"tenant": 7}
    )
    assert result.valid
    # Not treated as a conflict, not duplicated.
    assert not any(v.code == Code.TENANT_FILTER_CONFLICT for v in result.violations)


def test_require_strategy_reports_missing(catalog):
    guard = SQLGuard(
        catalog,
        Policy(
            rls=[RLSRule(table="orders", column="customer_id", param="tenant")],
            rls_strategy="require",
            on_missing_stats="ignore",
        ),
        dialect="postgres",
    )
    result = guard.validate("SELECT id FROM orders", params={"tenant": 7})
    assert not result.valid
    assert any(v.code == Code.MISSING_TENANT_FILTER for v in result.errors)


def test_require_strategy_passes_when_present(catalog):
    guard = SQLGuard(
        catalog,
        Policy(
            rls=[RLSRule(table="orders", column="customer_id", param="tenant")],
            rls_strategy="require",
            on_missing_stats="ignore",
        ),
        dialect="postgres",
    )
    result = guard.validate(
        "SELECT id FROM orders WHERE customer_id = 7", params={"tenant": 7}
    )
    assert result.valid


def test_subquery_strategy_wraps(catalog):
    guard = SQLGuard(
        catalog,
        Policy(
            rls=[RLSRule(table="orders", column="customer_id", param="tenant")],
            rls_strategy="subquery",
            on_missing_stats="ignore",
        ),
        dialect="postgres",
    )
    result = guard.validate("SELECT id FROM orders", params={"tenant": 7})
    assert result.valid
    # The physical table is wrapped in a filtered subquery.
    assert "SELECT * FROM orders" in result.sql
    assert "customer_id = 7" in result.sql


def test_parameterized_injection(catalog):
    guard = SQLGuard(
        catalog,
        Policy(
            rls=[RLSRule(table="orders", column="customer_id", param="tenant")],
            rls_parameterize=True,
            on_missing_stats="ignore",
        ),
        dialect="postgres",
    )
    result = guard.validate("SELECT id FROM orders", params={})
    assert result.valid
    assert "%(tenant)s" in result.sql  # placeholder, not a literal


def test_wildcard_rule_skips_tables_without_column(catalog):
    """A `*` rule filters every table that HAS the column, skips the rest."""
    guard = SQLGuard(
        catalog,
        Policy(
            rls=[RLSRule(table="*", column="customer_id", param="tenant", on_missing_column="skip")],
            on_missing_stats="ignore",
        ),
        dialect="postgres",
    )
    # orders and customers have customer_id; line_items does not.
    result = guard.validate(
        "SELECT o.id, li.sku FROM orders o JOIN line_items li ON li.order_id = o.id",
        params={"tenant": 7},
    )
    assert result.valid
    assert "o.customer_id = 7" in result.sql


def test_rls_rule_missing_column_is_config_error_at_build():
    catalog = Catalog.from_dict({"orders": {"id": "bigint"}})
    with pytest.raises(PolicyError):
        SQLGuard(
            catalog,
            Policy(rls=[RLSRule(table="orders", column="tenant_id")]),
            dialect="postgres",
        )


def test_rls_rule_unknown_table_is_config_error_at_build():
    catalog = Catalog.from_dict({"orders": {"id": "bigint", "customer_id": "bigint"}})
    with pytest.raises(PolicyError):
        SQLGuard(
            catalog,
            Policy(rls=[RLSRule(table="nonexistent", column="customer_id")]),
            dialect="postgres",
        )


def test_conflict_warn_mode(catalog):
    guard = SQLGuard(
        catalog,
        Policy(
            rls=[RLSRule(table="orders", column="customer_id", param="tenant")],
            on_conflicting_tenant_filter="warn",
            on_missing_stats="ignore",
        ),
        dialect="postgres",
    )
    result = guard.validate(
        "SELECT id FROM orders WHERE customer_id = 999", params={"tenant": 7}
    )
    # Warning, not error: the guard still injects its own filter.
    assert any(
        v.code == Code.TENANT_FILTER_CONFLICT and v.severity.value == "warning"
        for v in result.violations
    )


def test_default_param_name_is_column(catalog):
    guard = SQLGuard(
        catalog,
        Policy(rls=[RLSRule(table="orders", column="customer_id")], on_missing_stats="ignore"),
        dialect="postgres",
    )
    result = guard.validate("SELECT id FROM orders", params={"customer_id": 5})
    assert result.valid
    assert "customer_id = 5" in result.sql
