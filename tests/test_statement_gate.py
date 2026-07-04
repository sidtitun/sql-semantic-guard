"""The statement gate must reject everything that isn't a read-only SELECT."""

from __future__ import annotations

import pytest

from sqlguard import Code


@pytest.mark.parametrize(
    "sql,code",
    [
        ("INSERT INTO orders (id) VALUES (1)", Code.DISALLOWED_STATEMENT),
        ("UPDATE orders SET amount = 0", Code.DISALLOWED_STATEMENT),
        ("DELETE FROM orders", Code.DISALLOWED_STATEMENT),
        ("DROP TABLE orders", Code.DISALLOWED_STATEMENT),
        ("TRUNCATE TABLE orders", Code.DISALLOWED_STATEMENT),
        ("ALTER TABLE orders ADD COLUMN x int", Code.DISALLOWED_STATEMENT),
        ("CREATE TABLE t AS SELECT * FROM orders", Code.DISALLOWED_STATEMENT),
        ("GRANT SELECT ON orders TO bob", Code.DISALLOWED_STATEMENT),
        ("MERGE INTO orders o USING customers c ON o.id=c.id WHEN MATCHED THEN UPDATE SET amount=1", Code.DISALLOWED_STATEMENT),
        ("BEGIN", Code.DISALLOWED_STATEMENT),
        ("SET search_path TO evil", Code.DISALLOWED_STATEMENT),
    ],
)
def test_non_select_statements_blocked(plain_guard, sql, code):
    result = plain_guard.validate(sql)
    assert not result.valid
    assert result.sql is None
    assert any(v.code == code for v in result.errors)


@pytest.mark.parametrize(
    "sql",
    [
        "VACUUM orders",
        "CALL do_something()",
        "COPY orders FROM '/etc/passwd'",
        "EXPLAIN ANALYZE SELECT * FROM orders",
    ],
)
def test_opaque_commands_blocked(plain_guard, sql):
    """Anything sqlglot can only represent as an opaque Command is unverifiable."""
    result = plain_guard.validate(sql)
    assert not result.valid
    assert any(
        v.code in (Code.DISALLOWED_COMMAND, Code.DISALLOWED_STATEMENT)
        for v in result.errors
    )


def test_writable_cte_blocked(plain_guard):
    sql = "WITH d AS (DELETE FROM orders RETURNING id) SELECT * FROM d"
    result = plain_guard.validate(sql)
    assert not result.valid
    assert any(v.code == Code.NESTED_WRITE for v in result.errors)


def test_data_modifying_cte_insert_blocked(plain_guard):
    sql = "WITH x AS (INSERT INTO orders (id) VALUES (1) RETURNING id) SELECT * FROM x"
    result = plain_guard.validate(sql)
    assert not result.valid
    assert any(v.code == Code.NESTED_WRITE for v in result.errors)


def test_select_into_blocked(plain_guard):
    result = plain_guard.validate("SELECT * INTO evil_copy FROM orders")
    assert not result.valid
    assert any(v.code == Code.SELECT_INTO for v in result.errors)


def test_locking_clause_blocked(plain_guard):
    result = plain_guard.validate("SELECT * FROM orders FOR UPDATE")
    assert not result.valid
    assert any(v.code == Code.LOCKING_CLAUSE for v in result.errors)


def test_multiple_statements_blocked(plain_guard):
    result = plain_guard.validate("SELECT 1; DROP TABLE orders")
    assert not result.valid
    assert any(v.code == Code.MULTIPLE_STATEMENTS for v in result.errors)


def test_stacked_select_blocked(plain_guard):
    """The classic injection: a benign SELECT hiding a second statement."""
    result = plain_guard.validate(
        "SELECT id FROM orders; UPDATE customers SET tier='vip'"
    )
    assert not result.valid
    assert any(v.code == Code.MULTIPLE_STATEMENTS for v in result.errors)


def test_trailing_semicolon_is_fine(plain_guard):
    result = plain_guard.validate("SELECT id FROM orders;")
    assert result.valid


def test_empty_input_blocked(plain_guard):
    for sql in ("", "   ", "\n\t"):
        result = plain_guard.validate(sql)
        assert not result.valid
        assert any(v.code == Code.PARSE_ERROR for v in result.errors)


def test_garbage_input_blocked(plain_guard):
    result = plain_guard.validate("this is not sql at all !!!")
    assert not result.valid


def test_parenthesized_select_allowed(plain_guard):
    result = plain_guard.validate("(SELECT id FROM orders)")
    assert result.valid


def test_union_of_selects_allowed(plain_guard):
    result = plain_guard.validate(
        "SELECT id FROM orders UNION ALL SELECT id FROM customers"
    )
    assert result.valid


def test_all_blocked_results_have_no_sql(plain_guard):
    """Fail-closed: an invalid result must never carry executable SQL."""
    for sql in ["DELETE FROM orders", "DROP TABLE orders", "SELECT 1; SELECT 2"]:
        result = plain_guard.validate(sql)
        assert result.sql is None
        assert bool(result) is False
