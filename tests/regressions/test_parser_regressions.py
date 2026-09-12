"""Minimized inputs that previously stressed parser or guard boundaries."""

import pytest
import sqlglot

from sqlguard import Catalog, Policy, SQLGuard
from sqlguard.analyzer import statement_gate

GUARD = SQLGuard(
    Catalog.from_dict({"orders": {"id": "bigint"}}),
    Policy(on_missing_stats="ignore"),
    dialect="postgres",
)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        ";;;",
        "(((((SELECT 1)))))",
        "SELECT",
        "SELECT * FROM",
        "WITH x AS (SELECT 1)",
        "SELECT * FROM orders LIMIT 'ten'",
        "'unterminated",
        'SELECT "unterminated',
        "\x00SELECT 1",
        # Minimized from nightly fuzz run 34453248872.
        'SELECT!0FROM"oRDERS"JOIN"CUSTOMERS"ON?.\':\'()LIMIT!0',
    ],
    ids=[
        "empty",
        "semicolons",
        "nested-parentheses",
        "bare-select",
        "missing-source",
        "cte-without-query",
        "invalid-limit",
        "token-error-string",
        "token-error-identifier",
        "nul-prefix",
        "nightly-34453248872",
    ],
)
def test_minimized_input_never_crashes_or_fails_open(sql: str) -> None:
    result = GUARD.validate(sql)

    if not result.valid:
        assert result.sql is None
        return

    assert result.sql is not None
    tree = sqlglot.parse_one(result.sql, read="postgres")
    assert statement_gate(tree, "postgres") == []
    second = GUARD.validate(result.sql)
    assert second.valid
    assert second.sql == result.sql
