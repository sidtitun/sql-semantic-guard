"""Safe rewrites: star expansion, LIMIT enforcement, sensitive-column handling."""

from __future__ import annotations

from sqlglot import exp, parse_one

from sqlguard import Code, ColumnRule, Policy, RewriteKind, SQLGuard


def _limit_of(sql: str) -> int | None:
    tree = parse_one(sql, read="postgres")
    lim = tree.args.get("limit")
    if isinstance(lim, exp.Limit) and isinstance(lim.expression, exp.Literal):
        return int(lim.expression.name)
    return None


def test_star_expanded(plain_guard):
    result = plain_guard.validate("SELECT * FROM customers")
    assert result.valid
    assert "*" not in result.sql
    assert any(r.kind == RewriteKind.STAR_EXPANDED for r in result.rewrites)


def test_default_limit_added(plain_guard):
    result = plain_guard.validate("SELECT id FROM orders")
    assert _limit_of(result.sql) == 1000
    assert any(r.kind == RewriteKind.LIMIT_ADDED for r in result.rewrites)


def test_existing_small_limit_untouched(plain_guard):
    result = plain_guard.validate("SELECT id FROM orders LIMIT 10")
    assert _limit_of(result.sql) == 10
    assert not any(
        r.kind in (RewriteKind.LIMIT_ADDED, RewriteKind.LIMIT_CLAMPED)
        for r in result.rewrites
    )


def test_oversized_limit_clamped(plain_guard):
    result = plain_guard.validate("SELECT id FROM orders LIMIT 999999")
    assert _limit_of(result.sql) == 10_000
    assert any(r.kind == RewriteKind.LIMIT_CLAMPED for r in result.rewrites)


def test_no_limit_when_disabled(catalog):
    guard = SQLGuard(catalog, Policy(default_limit=None, on_missing_stats="ignore"))
    result = guard.validate("SELECT id FROM orders")
    assert result.valid
    assert _limit_of(result.sql) is None


def test_limit_on_union_applies_to_whole(plain_guard):
    result = plain_guard.validate(
        "SELECT id FROM orders UNION ALL SELECT id FROM customers"
    )
    assert result.valid
    assert _limit_of(result.sql) == 1000


def test_fetch_first_recognized_as_limit(plain_guard):
    result = plain_guard.validate("SELECT id FROM orders FETCH FIRST 5 ROWS ONLY")
    assert result.valid
    # An explicit small FETCH shouldn't trigger add/clamp.
    assert not any(
        r.kind in (RewriteKind.LIMIT_ADDED, RewriteKind.LIMIT_CLAMPED)
        for r in result.rewrites
    )


def test_star_expansion_disabled(catalog):
    guard = SQLGuard(catalog, Policy(expand_star=False, on_missing_stats="ignore"))
    result = guard.validate("SELECT * FROM customers")
    assert result.valid
    assert "*" in result.sql


# -- column policy --------------------------------------------------------


def test_denied_column_explicit_reference_blocked(guard):
    # ssn is tagged pii and denied
    result = guard.validate("SELECT ssn FROM orders", params={"customer_id": 1})
    assert not result.valid
    assert any(v.code == Code.COLUMN_DENIED for v in result.errors)


def test_denied_column_dropped_from_star(guard):
    result = guard.validate("SELECT * FROM orders", params={"customer_id": 1})
    assert result.valid
    assert "ssn" not in result.sql
    assert any(
        r.kind == RewriteKind.SENSITIVE_COLUMN_EXCLUDED for r in result.rewrites
    )


def test_denied_column_in_where_blocked(guard):
    result = guard.validate(
        "SELECT id FROM orders WHERE ssn = '123'", params={"customer_id": 1}
    )
    assert not result.valid
    assert any(v.code == Code.COLUMN_DENIED for v in result.errors)


def test_denied_pii_across_tables(guard):
    # customers.email is also tagged pii
    result = guard.validate("SELECT email FROM customers", params={"customer_id": 1})
    assert not result.valid
    assert any(v.code == Code.COLUMN_DENIED for v in result.errors)


def test_table_specific_column_rule(catalog):
    guard = SQLGuard(
        catalog,
        Policy(
            column_rules=[ColumnRule(table="orders", column="notes", action="deny")],
            on_missing_stats="ignore",
        ),
    )
    # notes is denied on orders...
    assert not guard.validate("SELECT notes FROM orders").valid
    # ...but there's no notes on customers, so a customers query is unaffected
    assert guard.validate("SELECT name FROM customers").valid


def test_exclude_from_star_allows_explicit(catalog):
    guard = SQLGuard(
        catalog,
        Policy(
            column_rules=[
                ColumnRule(table="orders", column="notes", action="exclude_from_star")
            ],
            on_missing_stats="ignore",
        ),
    )
    # dropped from *
    star = guard.validate("SELECT * FROM orders")
    assert star.valid and "notes" not in star.sql
    # but explicit reference is allowed
    explicit = guard.validate("SELECT notes FROM orders")
    assert explicit.valid


def test_star_expanding_to_nothing_is_error(catalog):
    guard = SQLGuard(
        catalog,
        Policy(
            column_rules=[ColumnRule(table="audit_log", column="*", action="deny")],
            on_missing_stats="ignore",
        ),
    )
    result = guard.validate("SELECT * FROM audit_log")
    assert not result.valid
    assert any(v.code == Code.EMPTY_SELECT for v in result.errors)


def test_denied_column_through_cte(guard):
    """A restricted column must stay restricted when laundered through a CTE."""
    sql = """
        WITH t AS (SELECT ssn AS s FROM orders)
        SELECT t.s FROM t
    """
    result = guard.validate(sql, params={"customer_id": 1})
    assert not result.valid
    assert any(v.code == Code.COLUMN_DENIED for v in result.errors)
