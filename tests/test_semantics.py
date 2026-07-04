"""Catalog-aware semantic validation: the core differentiator."""

from __future__ import annotations

from sqlguard import Code


def test_hallucinated_table(plain_guard):
    result = plain_guard.validate("SELECT * FROM orderz")
    assert not result.valid
    errs = [v for v in result.errors if v.code == Code.UNKNOWN_TABLE]
    assert errs
    assert "orderz" in errs[0].message
    assert errs[0].hint and "orders" in errs[0].hint  # did-you-mean


def test_hallucinated_column(plain_guard):
    result = plain_guard.validate("SELECT customer_name FROM customers")
    assert not result.valid
    assert any(v.code == Code.UNKNOWN_COLUMN for v in result.errors)


def test_hallucinated_column_suggests_real_one(plain_guard):
    result = plain_guard.validate("SELECT amont FROM orders")
    errs = [v for v in result.errors if v.code == Code.UNKNOWN_COLUMN]
    assert errs
    assert errs[0].hint and "amount" in errs[0].hint


def test_qualified_unknown_column(plain_guard):
    result = plain_guard.validate("SELECT o.nonexistent FROM orders o")
    errs = [v for v in result.errors if v.code == Code.UNKNOWN_COLUMN]
    assert errs
    assert errs[0].table == "orders"
    assert errs[0].column == "nonexistent"


def test_unknown_alias(plain_guard):
    result = plain_guard.validate("SELECT x.id FROM orders o")
    assert not result.valid
    assert any(v.code == Code.UNKNOWN_TABLE_ALIAS for v in result.errors)


def test_ambiguous_column(plain_guard):
    # customer_id exists on both orders and customers
    result = plain_guard.validate(
        "SELECT customer_id FROM orders o JOIN customers c ON o.customer_id = c.id"
    )
    assert not result.valid
    errs = [v for v in result.errors if v.code == Code.AMBIGUOUS_COLUMN]
    assert errs
    assert errs[0].hint and "." in errs[0].hint


def test_qualified_resolves_ambiguity(plain_guard):
    result = plain_guard.validate(
        "SELECT o.customer_id FROM orders o JOIN customers c ON o.customer_id = c.id"
    )
    assert result.valid


def test_valid_multi_table_join(plain_guard):
    sql = """
        SELECT o.id, c.name, li.sku
        FROM orders o
        JOIN customers c ON o.customer_id = c.id
        JOIN line_items li ON li.order_id = o.id
        WHERE o.status = 'shipped'
    """
    result = plain_guard.validate(sql)
    assert result.valid, [str(v) for v in result.errors]


def test_all_errors_reported_at_once(plain_guard):
    """An LLM repair loop needs every problem in one pass, not just the first."""
    result = plain_guard.validate("SELECT bogus1, bogus2, bogus3 FROM orders")
    unknown = [v for v in result.errors if v.code == Code.UNKNOWN_COLUMN]
    assert len(unknown) == 3


def test_cte_column_binding(plain_guard):
    sql = """
        WITH recent AS (
            SELECT id, customer_id, amount FROM orders WHERE created_at > '2026-01-01'
        )
        SELECT r.id, r.amount FROM recent r
    """
    result = plain_guard.validate(sql)
    assert result.valid, [str(v) for v in result.errors]


def test_cte_unknown_output_column(plain_guard):
    sql = """
        WITH recent AS (SELECT id, amount FROM orders)
        SELECT r.customer_id FROM recent r
    """
    result = plain_guard.validate(sql)
    assert not result.valid
    errs = [v for v in result.errors if v.code == Code.UNKNOWN_COLUMN]
    assert errs
    assert errs[0].column == "customer_id"
    assert errs[0].hint and "id" in errs[0].hint  # lists the CTE's real outputs


def test_subquery_scope_isolation(plain_guard):
    """A column valid only inside a subquery must not leak to the outer scope."""
    sql = """
        SELECT o.id FROM orders o
        WHERE o.customer_id IN (SELECT c.id FROM customers c WHERE c.tier = 'gold')
    """
    result = plain_guard.validate(sql)
    assert result.valid, [str(v) for v in result.errors]


def test_correlated_subquery(plain_guard):
    sql = """
        SELECT c.id, c.name FROM customers c
        WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)
    """
    result = plain_guard.validate(sql)
    assert result.valid, [str(v) for v in result.errors]


def test_select_alias_in_where_is_error(plain_guard):
    """Engines don't resolve SELECT aliases in WHERE; catch it before they do."""
    result = plain_guard.validate("SELECT amount AS amt FROM orders WHERE amt > 100")
    assert not result.valid
    assert any(v.code in (Code.ALIAS_MISUSE, Code.UNKNOWN_COLUMN) for v in result.errors)


def test_select_alias_in_order_by_is_ok(plain_guard):
    result = plain_guard.validate("SELECT amount AS amt FROM orders ORDER BY amt")
    assert result.valid, [str(v) for v in result.errors]


def test_group_by_alias_ok(plain_guard):
    result = plain_guard.validate(
        "SELECT status AS s, count(*) AS n FROM orders GROUP BY s ORDER BY n DESC"
    )
    assert result.valid, [str(v) for v in result.errors]


def test_system_tables_not_in_catalog(plain_guard):
    """Fail closed: information_schema isn't in the catalog, so it's off-limits."""
    result = plain_guard.validate("SELECT table_name FROM information_schema.tables")
    assert not result.valid
    assert any(v.code == Code.UNKNOWN_TABLE for v in result.errors)


def test_self_join_with_aliases(plain_guard):
    sql = """
        SELECT a.id, b.id FROM orders a
        JOIN orders b ON a.customer_id = b.customer_id AND a.id <> b.id
    """
    result = plain_guard.validate(sql)
    assert result.valid, [str(v) for v in result.errors]


def test_case_insensitive_identifiers(plain_guard):
    result = plain_guard.validate("SELECT ID, AMOUNT FROM ORDERS")
    assert result.valid, [str(v) for v in result.errors]


def test_aggregate_and_expressions(plain_guard):
    sql = """
        SELECT customer_id, count(*) AS orders, sum(amount) AS total
        FROM orders GROUP BY customer_id HAVING sum(amount) > 1000
    """
    result = plain_guard.validate(sql)
    assert result.valid, [str(v) for v in result.errors]


def test_scalar_subquery_in_select(plain_guard):
    sql = """
        SELECT c.id,
               (SELECT count(*) FROM orders o WHERE o.customer_id = c.id) AS n
        FROM customers c
    """
    result = plain_guard.validate(sql)
    assert result.valid, [str(v) for v in result.errors]


def test_derived_table_star_is_opaque_but_ok(plain_guard):
    sql = "SELECT sub.id FROM (SELECT * FROM orders) sub"
    result = plain_guard.validate(sql)
    # sub.* is opaque; we can't disprove sub.id, so no false positive
    assert result.valid, [str(v) for v in result.errors]


def test_star_expansion_lists_columns(plain_guard):
    result = plain_guard.validate("SELECT * FROM customers")
    assert result.valid
    assert "customers.id" in result.sql or "id" in result.sql
    assert "*" not in result.sql  # expanded


def test_stats_referenced_columns(plain_guard):
    result = plain_guard.validate(
        "SELECT o.id, o.amount FROM orders o WHERE o.status = 'x'"
    )
    cols = result.stats.referenced_columns.get("orders", [])
    assert "id" in cols and "amount" in cols and "status" in cols


def test_no_false_positive_on_functions(plain_guard):
    sql = "SELECT date_trunc('month', created_at) AS m, count(*) FROM orders GROUP BY m"
    result = plain_guard.validate(sql)
    assert result.valid, [str(v) for v in result.errors]
