"""Cost controls: join sanity, partition pruning, scan budgets."""

from __future__ import annotations

from sqlguard import Code, Policy, SQLGuard

# -- join sanity ----------------------------------------------------------


def test_cross_join_warned(plain_guard):
    result = plain_guard.validate("SELECT o.id FROM orders o CROSS JOIN customers c")
    assert any(v.code == Code.CARTESIAN_JOIN for v in result.violations)


def test_comma_join_without_predicate_warned(plain_guard):
    result = plain_guard.validate("SELECT o.id FROM orders o, customers c")
    assert any(v.code == Code.CARTESIAN_JOIN for v in result.violations)


def test_comma_join_with_where_link_ok(plain_guard):
    result = plain_guard.validate(
        "SELECT o.id FROM orders o, customers c WHERE o.customer_id = c.id"
    )
    assert not any(v.code == Code.CARTESIAN_JOIN for v in result.violations)


def test_proper_join_no_warning(plain_guard):
    result = plain_guard.validate(
        "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id"
    )
    assert not any(
        v.code in (Code.CARTESIAN_JOIN, Code.SUSPICIOUS_JOIN) for v in result.violations
    )


def test_strict_joins_escalates_to_error(catalog):
    guard = SQLGuard(
        catalog, Policy(strict_joins=True, on_missing_stats="ignore")
    )
    result = guard.validate("SELECT o.id FROM orders o CROSS JOIN customers c")
    assert not result.valid
    assert any(
        v.code == Code.CARTESIAN_JOIN and v.severity.value == "error"
        for v in result.errors
    )


def test_one_sided_join_condition_warned(plain_guard):
    result = plain_guard.validate(
        "SELECT o.id FROM orders o JOIN customers c ON o.id = o.customer_id"
    )
    assert any(v.code == Code.SUSPICIOUS_JOIN for v in result.violations)


# -- scan budgets ---------------------------------------------------------


def test_scan_budget_blocks_large_table(catalog):
    # line_items is 20 GiB; cap at 1 GiB
    guard = SQLGuard(
        catalog, Policy(max_bytes_scanned=1 << 30, on_missing_stats="ignore")
    )
    result = guard.validate("SELECT sku FROM line_items")
    assert not result.valid
    assert any(v.code == Code.SCAN_BUDGET_EXCEEDED for v in result.errors)


def test_scan_budget_allows_small_table(catalog):
    guard = SQLGuard(
        catalog, Policy(max_bytes_scanned=1 << 30, on_missing_stats="ignore")
    )
    result = guard.validate("SELECT name FROM customers")  # 512 MiB < 1 GiB
    assert result.valid


def test_row_budget(catalog):
    guard = SQLGuard(
        catalog, Policy(max_rows_scanned=1_000_000, on_missing_stats="ignore")
    )
    result = guard.validate("SELECT id FROM orders")  # 50M rows
    assert not result.valid
    assert any(v.code == Code.SCAN_BUDGET_EXCEEDED for v in result.errors)


def test_cost_estimate_populated(catalog):
    guard = SQLGuard(catalog, Policy(on_missing_stats="ignore"))
    result = guard.validate("SELECT id FROM orders")
    assert result.stats.cost is not None
    assert result.stats.cost.bytes_scanned is not None
    assert result.stats.cost.rows_scanned == 50_000_000


def test_missing_stats_error_mode(catalog):
    guard = SQLGuard(catalog, Policy(on_missing_stats="error"))
    # audit_log has no stats
    result = guard.validate("SELECT id FROM audit_log")
    assert not result.valid
    assert any(v.code == Code.MISSING_STATISTICS for v in result.errors)


def test_missing_stats_ignore_mode(catalog):
    guard = SQLGuard(catalog, Policy(on_missing_stats="ignore"))
    result = guard.validate("SELECT id FROM audit_log")
    assert result.valid


def test_self_join_doubles_scan_estimate(catalog):
    guard = SQLGuard(catalog, Policy(on_missing_stats="ignore"))
    single = guard.validate("SELECT id FROM orders")
    double = guard.validate(
        "SELECT a.id FROM orders a JOIN orders b ON a.id = b.id"
    )
    assert double.stats.cost.bytes_scanned > single.stats.cost.bytes_scanned


# -- partition filters (Athena) ------------------------------------------


def test_athena_missing_partition_filter_blocked(athena_catalog):
    guard = SQLGuard(athena_catalog, Policy(on_missing_stats="ignore"), dialect="athena")
    result = guard.validate("SELECT event_id FROM analytics.events")
    assert not result.valid
    assert any(v.code == Code.MISSING_PARTITION_FILTER for v in result.errors)


def test_athena_with_partition_filter_ok(athena_catalog):
    guard = SQLGuard(athena_catalog, Policy(on_missing_stats="ignore"), dialect="athena")
    result = guard.validate(
        "SELECT event_id FROM analytics.events WHERE dt >= '2026-01-01'"
    )
    assert result.valid, [str(v) for v in result.errors]


def test_partition_filter_reduces_estimate(athena_catalog):
    guard = SQLGuard(
        athena_catalog,
        Policy(partition_selectivity=0.01, on_missing_stats="ignore"),
        dialect="athena",
    )
    filtered = guard.validate(
        "SELECT event_id FROM analytics.events WHERE dt = '2026-01-01'"
    )
    assert filtered.valid
    # With a partition filter and columnar column pruning, the estimate should
    # be far below the 4 TiB raw table size.
    assert filtered.stats.cost.bytes_scanned < (4 << 40) * 0.5


def test_columnar_column_pruning(athena_catalog):
    """Selecting one narrow column of a wide columnar table scans far less."""
    guard = SQLGuard(athena_catalog, Policy(on_missing_stats="ignore"), dialect="athena")
    narrow = guard.validate(
        "SELECT user_id FROM analytics.events WHERE dt = '2026-01-01'"
    )
    est = narrow.stats.cost.per_table[0]
    assert est.column_ratio is not None and est.column_ratio < 1.0


def test_partition_filter_not_required_on_postgres(catalog):
    """Partition enforcement defaults on for Athena, off for Postgres."""
    guard = SQLGuard(catalog, Policy(on_missing_stats="ignore"), dialect="postgres")
    # orders has no partition columns anyway; just assert no partition error
    result = guard.validate("SELECT id FROM orders")
    assert not any(v.code == Code.MISSING_PARTITION_FILTER for v in result.violations)


def test_require_partition_filter_override(athena_catalog):
    guard = SQLGuard(
        athena_catalog,
        Policy(require_partition_filter=False, on_missing_stats="ignore"),
        dialect="athena",
    )
    result = guard.validate("SELECT event_id FROM analytics.events")
    assert not any(v.code == Code.MISSING_PARTITION_FILTER for v in result.violations)
