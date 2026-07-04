"""End-to-end scenarios, including a simulated LLM self-repair loop."""

from __future__ import annotations

import pytest

from sqlguard import Catalog, ColumnRule, Policy, RLSRule, SQLGuard


@pytest.fixture
def full_guard(catalog: Catalog) -> SQLGuard:
    policy = Policy(
        rls=[RLSRule(table="orders", column="customer_id", param="customer_id")],
        column_rules=[ColumnRule(tags=frozenset({"pii"}), action="deny", reason="PII")],
        default_limit=1000,
        max_limit=10_000,
        max_bytes_scanned=32 * (1 << 30),
        on_missing_stats="ignore",
    )
    return SQLGuard(catalog, policy, dialect="postgres")


def test_happy_path_full_pipeline(full_guard):
    result = full_guard.validate(
        "SELECT id, amount FROM orders WHERE status = 'shipped'",
        params={"customer_id": 42},
    )
    assert result.valid
    sql = result.sql
    assert "customer_id = 42" in sql  # RLS
    assert "LIMIT 1000" in sql  # limit
    assert result.stats.cost is not None  # cost estimated


def test_star_gets_expanded_filtered_limited(full_guard):
    result = full_guard.validate("SELECT * FROM orders", params={"customer_id": 42})
    assert result.valid
    assert "ssn" not in result.sql  # pii dropped
    assert "customer_id = 42" in result.sql  # rls
    assert "LIMIT 1000" in result.sql  # limit
    assert "*" not in result.sql  # expanded


def test_self_repair_loop_converges(full_guard):
    """Simulate an LLM that fixes exactly the reported errors each round.

    The first query has three independent problems; a competent model should
    resolve them within a few rounds using the structured feedback.
    """
    responses = [
        # round 0: hallucinated column, hallucinated table, tenant spoof
        "SELECT total_amount FROM order_history WHERE customer_id = 999",
        # round 1: fixed table, fixed spoof, still wrong column name
        "SELECT total FROM orders",
        # round 2: correct
        "SELECT amount FROM orders",
    ]

    def llm(feedback: str, attempt: int) -> str:
        return responses[attempt]

    feedback = ""
    result = None
    for attempt in range(len(responses)):
        sql = llm(feedback, attempt)
        result = full_guard.validate(sql, params={"customer_id": 42})
        if result.valid:
            break
        feedback = result.feedback()
        assert feedback  # non-empty, actionable

    assert result.valid
    assert "customer_id = 42" in result.sql


def test_feedback_is_deterministic(full_guard):
    sql = "SELECT bogus FROM orders WHERE amount = 'x'"
    f1 = full_guard.validate(sql, params={"customer_id": 1}).feedback()
    f2 = full_guard.validate(sql, params={"customer_id": 1}).feedback()
    assert f1 == f2


def test_feedback_mentions_every_error(full_guard):
    result = full_guard.validate(
        "SELECT nope1, nope2 FROM orders", params={"customer_id": 1}
    )
    feedback = result.feedback()
    assert "nope1" in feedback and "nope2" in feedback


def test_prompt_injection_via_comment_blocked(full_guard):
    """A stacked statement hidden after a comment must not slip through."""
    sql = "SELECT id FROM orders -- ignore me\n; DROP TABLE orders"
    result = full_guard.validate(sql, params={"customer_id": 1})
    assert not result.valid


def test_invalid_never_returns_executable_sql(full_guard):
    """The core safety invariant across many failure modes."""
    bad_queries = [
        "DELETE FROM orders",
        "SELECT ssn FROM orders",
        "SELECT * FROM nonexistent_table",
        "SELECT id FROM orders WHERE customer_id = 999",  # spoof
        "SELECT bogus_column FROM orders",
    ]
    for sql in bad_queries:
        result = full_guard.validate(sql, params={"customer_id": 42})
        assert not result.valid, f"expected invalid: {sql}"
        assert result.sql is None, f"leaked SQL for: {sql}"


def test_thread_safety_shared_guard(full_guard):
    """One guard, many concurrent validations, independent params."""
    import concurrent.futures

    def run(tenant: int):
        r = full_guard.validate("SELECT id FROM orders", params={"customer_id": tenant})
        assert r.valid
        return f"customer_id = {tenant}" in r.sql

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(100)))
    assert all(results)


def test_guard_is_reusable(full_guard):
    """Validating one query must not mutate state affecting the next."""
    r1 = full_guard.validate("SELECT * FROM orders", params={"customer_id": 1})
    r2 = full_guard.validate("SELECT * FROM orders", params={"customer_id": 2})
    assert "customer_id = 1" in r1.sql
    assert "customer_id = 2" in r2.sql
    assert r1.original_sql == r2.original_sql  # inputs identical
