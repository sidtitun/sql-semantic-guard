"""Shadow mode: measure would-block rates before enforcing (roadmap 2.7)."""

from __future__ import annotations

import pytest

from sqlguard import Code, ColumnRule, Policy, RLSRule, SQLGuard
from sqlguard.errors import PolicyError


@pytest.fixture
def shadow_guard(catalog):
    policy = Policy(
        enforcement="log_only",
        rls=[RLSRule(table="orders", column="customer_id", param="customer_id")],
        column_rules=[ColumnRule(tags=frozenset({"pii"}), action="deny")],
        max_bytes_scanned=1 << 30,  # tight budget: orders (8 GiB) exceeds it
        on_missing_stats="ignore",
    )
    return SQLGuard(catalog, policy, dialect="postgres")


def test_semantic_error_passes_through_flagged(shadow_guard):
    """Unknown column: recorded as an ERROR, but the query is not runnable —
    wait, it IS runnable? No: the column doesn't exist, but shadow mode's job
    is measurement, and the DB will produce its own error. The guard lets it
    through flagged."""
    result = shadow_guard.validate("SELECT bogus FROM orders", params={"customer_id": 1})
    assert result.valid
    assert result.would_block
    assert result.sql is not None
    assert any(v.code == Code.UNKNOWN_COLUMN for v in result.errors)


def test_budget_violation_passes_through_flagged(shadow_guard):
    result = shadow_guard.validate("SELECT id FROM orders", params={"customer_id": 1})
    assert result.valid and result.would_block
    assert any(v.code == Code.SCAN_BUDGET_EXCEEDED for v in result.errors)


def test_rls_still_applied_in_shadow(shadow_guard):
    """Shadow mode is log-only, not unprotected: safe rewrites still apply."""
    result = shadow_guard.validate("SELECT id FROM orders", params={"customer_id": 7})
    assert "customer_id = 7" in result.sql


def test_pii_still_dropped_from_star_in_shadow(shadow_guard):
    result = shadow_guard.validate("SELECT * FROM orders", params={"customer_id": 7})
    assert result.sql is not None
    assert "ssn" not in result.sql


def test_writes_still_blocked_in_shadow(shadow_guard):
    for sql in [
        "DELETE FROM orders",
        "DROP TABLE orders",
        "SELECT 1; SELECT 2",
        "WITH d AS (DELETE FROM orders RETURNING id) SELECT * FROM d",
        "SELECT * FROM orders FOR UPDATE",
        "not sql at all (",
    ]:
        result = shadow_guard.validate(sql, params={"customer_id": 1})
        assert not result.valid, f"hard-stop must block in shadow mode: {sql}"
        assert result.sql is None
        assert not result.would_block  # blocked outright, not "would"


def test_clean_query_has_no_would_block(shadow_guard):
    result = shadow_guard.validate(
        "SELECT name FROM customers", params={"customer_id": 1}
    )
    assert result.valid and not result.would_block


def test_missing_rls_param_flagged_but_shadowed(shadow_guard):
    """rls_param_missing is a policy error, not a hard stop: in shadow mode it
    flows through flagged (and unfiltered — which is exactly the signal the
    would-block metric exists to surface before enforcement)."""
    result = shadow_guard.validate("SELECT id FROM orders", params={})
    assert result.valid and result.would_block
    assert any(v.code == Code.RLS_PARAM_MISSING for v in result.errors)


def test_feedback_mentions_shadow(shadow_guard):
    result = shadow_guard.validate("SELECT bogus FROM orders", params={"customer_id": 1})
    assert "shadow mode" in result.feedback()
    assert "BLOCKED" in result.feedback()


def test_would_block_in_to_dict(shadow_guard):
    result = shadow_guard.validate("SELECT bogus FROM orders", params={"customer_id": 1})
    assert result.to_dict()["would_block"] is True


def test_enforcing_mode_unchanged(catalog):
    guard = SQLGuard(catalog, Policy(on_missing_stats="ignore"))
    result = guard.validate("SELECT bogus FROM orders")
    assert not result.valid and not result.would_block and result.sql is None


def test_bad_enforcement_value_rejected():
    with pytest.raises(PolicyError):
        Policy(enforcement="maybe")
