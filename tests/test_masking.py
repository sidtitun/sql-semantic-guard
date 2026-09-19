"""Column masking policy and rewrite behavior."""

from __future__ import annotations

import pytest

from sqlguard import Code, ColumnRule, Policy, RewriteKind, SQLGuard
from sqlguard.errors import PolicyError


def _guard(catalog, rule: ColumnRule, *, dialect: str = "postgres") -> SQLGuard:
    return SQLGuard(
        catalog,
        Policy(
            column_rules=[rule],
            default_limit=None,
            on_missing_stats="ignore",
            require_partition_filter=False,
        ),
        dialect=dialect,
    )


def test_redact_masks_explicit_projection(catalog):
    guard = _guard(
        catalog,
        ColumnRule(table="customers", column="email", action="mask", mask_with="redact"),
    )

    result = guard.validate("SELECT email FROM customers")

    assert result.valid
    assert result.sql == "SELECT '***' AS email FROM customers AS customers"
    assert any(rewrite.kind == RewriteKind.COLUMN_MASKED for rewrite in result.rewrites)


def test_redact_masks_star_projection(catalog):
    guard = _guard(
        catalog,
        ColumnRule(tags={"pii"}, action="mask", mask_with="redact"),
    )

    result = guard.validate("SELECT * FROM customers")

    assert result.valid
    assert "'***' AS email" in result.sql
    assert "customers.email AS email" not in result.sql


def test_null_mask_preserves_catalog_type(catalog):
    guard = _guard(
        catalog,
        ColumnRule(table="customers", column="id", action="mask", mask_with="null"),
    )

    result = guard.validate("SELECT id FROM customers")

    assert result.valid
    assert "CAST(NULL AS BIGINT) AS id" in result.sql


def test_hash_mask_is_dialect_specific(catalog, athena_catalog):
    postgres = _guard(
        catalog,
        ColumnRule(table="customers", column="email", action="mask", mask_with="hash"),
    ).validate("SELECT email FROM customers")
    athena = _guard(
        athena_catalog,
        ColumnRule(table="analytics.events", column="event_id", action="mask", mask_with="hash"),
        dialect="athena",
    ).validate("SELECT event_id FROM analytics.events")

    assert postgres.valid and "MD5(CAST(customers.email AS TEXT)) AS email" in postgres.sql
    assert athena.valid
    assert "TO_HEX(MD5(TO_UTF8(CAST(events.event_id AS VARCHAR)))) AS event_id" in athena.sql


def test_custom_mask_template(catalog):
    guard = _guard(
        catalog,
        ColumnRule(
            table="customers",
            column="email",
            action="mask",
            mask_with="LEFT({col}, 4) || '****'",
        ),
    )

    result = guard.validate("SELECT email FROM customers")

    assert result.valid
    assert "LEFT(customers.email, 4) || '****' AS email" in result.sql


@pytest.mark.parametrize(
    "mask_with",
    [None, "", "redact; DROP TABLE customers", "LOWER(other_column)"],
)
def test_invalid_mask_templates_fail_at_policy_construction(mask_with):
    with pytest.raises(PolicyError):
        ColumnRule(action="mask", mask_with=mask_with)


def test_masked_column_is_blocked_in_predicate_by_default(catalog):
    guard = _guard(
        catalog,
        ColumnRule(table="customers", column="email", action="mask", mask_with="hash"),
    )

    result = guard.validate("SELECT email FROM customers WHERE email = 'a@example.com'")

    assert not result.valid
    assert any(error.code == Code.COLUMN_DENIED for error in result.errors)


def test_masked_column_can_be_used_in_predicate_when_enabled(catalog):
    guard = _guard(
        catalog,
        ColumnRule(
            table="customers",
            column="email",
            action="mask",
            mask_with="hash",
            allow_predicates=True,
        ),
    )

    result = guard.validate("SELECT email FROM customers WHERE email = 'a@example.com'")

    assert result.valid
    assert "MD5(CAST(customers.email AS TEXT)) AS email" in result.sql
    assert "WHERE customers.email = 'a@example.com'" in result.sql


def test_masked_column_is_blocked_in_order_by(catalog):
    guard = _guard(
        catalog,
        ColumnRule(table="customers", column="email", action="mask", mask_with="redact"),
    )

    result = guard.validate("SELECT name FROM customers ORDER BY email")

    assert not result.valid
    assert any(error.code == Code.COLUMN_DENIED for error in result.errors)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM customers GROUP BY email",
        "SELECT c.name FROM customers c JOIN orders o ON c.email = o.notes",
    ],
)
def test_masked_column_is_blocked_in_grouping_and_joins(catalog, sql):
    guard = _guard(
        catalog,
        ColumnRule(table="customers", column="email", action="mask", mask_with="redact"),
    )

    result = guard.validate(sql)

    assert not result.valid
    assert any(error.code == Code.COLUMN_DENIED for error in result.errors)


def test_rule_precedence_deny_over_mask(catalog):
    guard = SQLGuard(
        catalog,
        Policy(
            column_rules=[
                ColumnRule(table="customers", column="email", action="mask", mask_with="redact"),
                ColumnRule(tags={"pii"}, action="deny"),
            ],
            default_limit=None,
            on_missing_stats="ignore",
        ),
    )

    result = guard.validate("SELECT email FROM customers")

    assert not result.valid
    assert not any(rewrite.kind == RewriteKind.COLUMN_MASKED for rewrite in result.rewrites)


def test_rule_precedence_mask_over_exclude(catalog):
    guard = SQLGuard(
        catalog,
        Policy(
            column_rules=[
                ColumnRule(table="customers", column="email", action="exclude_from_star"),
                ColumnRule(table="customers", column="email", action="mask", mask_with="redact"),
            ],
            default_limit=None,
            on_missing_stats="ignore",
        ),
    )

    result = guard.validate("SELECT * FROM customers")

    assert result.valid
    assert "'***' AS email" in result.sql


def test_explicit_projection_is_not_mistaken_for_star_output(catalog):
    guard = _guard(
        catalog,
        ColumnRule(table="orders", column="notes", action="exclude_from_star"),
    )

    result = guard.validate("SELECT *, notes AS explicit_notes FROM orders")

    assert result.valid
    assert "orders.notes AS notes" not in result.sql
    assert "orders.notes AS explicit_notes" in result.sql


def test_mask_applies_through_cte_lineage(catalog):
    guard = _guard(
        catalog,
        ColumnRule(table="customers", column="email", action="mask", mask_with="hash"),
    )

    result = guard.validate(
        "WITH contact AS (SELECT email AS address FROM customers) SELECT address FROM contact"
    )

    assert result.valid
    assert result.sql.count("MD5(") == 1
    assert sum(r.kind == RewriteKind.COLUMN_MASKED for r in result.rewrites) == 1


def test_cte_passthrough_preserves_allowed_predicate_semantics(catalog):
    guard = _guard(
        catalog,
        ColumnRule(
            table="customers",
            column="email",
            action="mask",
            mask_with="hash",
            allow_predicates=True,
        ),
    )

    result = guard.validate(
        "WITH contact AS (SELECT email AS address FROM customers) "
        "SELECT address FROM contact WHERE address = 'a@example.com'"
    )

    assert result.valid
    assert result.sql.count("MD5(") == 1
    assert "SELECT customers.email AS address" in result.sql
    assert "WHERE contact.address = 'a@example.com'" in result.sql


def test_policy_prompt_describes_masked_columns(catalog):
    guard = _guard(
        catalog,
        ColumnRule(tags={"pii"}, action="mask", mask_with="redact"),
    )

    prompt = guard.policy_prompt()

    assert "tagged pii" in prompt
    assert "masked automatically" in prompt
