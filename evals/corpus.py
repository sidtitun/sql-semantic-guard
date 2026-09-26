"""Versioned deterministic corpus for SQLGuard's pre-release quality gates.

This measures guard behavior on known-safe and known-unsafe SQL. It is not a
text-to-SQL model benchmark and makes no claim about answer correctness.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlguard import Catalog, ColumnRule, Policy, RLSRule, SQLGuard


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    sql: str
    expected_valid: bool
    params: dict[str, object]


def build_guard() -> SQLGuard:
    catalog = Catalog.from_dict(
        {
            "orders": {
                "columns": {
                    "id": "bigint",
                    "customer_id": "bigint",
                    "amount": "decimal(10,2)",
                    "status": "varchar(32)",
                    "ssn": {"type": "text", "tags": ["pii"]},
                    "created_at": "timestamp",
                },
                "foreign_keys": [{"columns": ["customer_id"], "ref_table": "customers", "ref_columns": ["id"]}],
                "row_count": 50_000_000,
                "total_bytes": 8 * (1 << 30),
            },
            "customers": {
                "columns": {
                    "id": "bigint",
                    "name": "varchar(120)",
                    "tier": "varchar(16)",
                }
            },
        }
    )
    policy = Policy(
        rls=[RLSRule(table="orders", column="customer_id", param="customer_id")],
        column_rules=[ColumnRule(tags=frozenset({"pii"}), action="deny")],
        default_limit=1000,
        max_limit=10_000,
        on_missing_stats="ignore",
    )
    return SQLGuard(catalog, policy, dialect="postgres")


def _safe_cases() -> tuple[EvalCase, ...]:
    """Curated safe patterns with small deterministic value variations."""
    sql: list[str] = [
        "SELECT id FROM orders",
        "SELECT id, amount FROM orders WHERE status = 'shipped'",
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status",
        "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id",
        "WITH recent AS (SELECT id, amount FROM orders WHERE amount > 10) SELECT id FROM recent",
        "SELECT id FROM orders WHERE created_at >= DATE '2025-01-01'",
        "SELECT status, SUM(amount) AS revenue FROM orders GROUP BY status HAVING SUM(amount) > 100",
        "SELECT id FROM orders ORDER BY created_at DESC LIMIT 25",
        "SELECT id FROM orders WHERE amount BETWEEN 10 AND 50",
        "SELECT id FROM orders WHERE status IN ('shipped', 'pending')",
    ]
    # These combinations intentionally exercise independent projections and
    # predicates while keeping each case a readable, inspectable SQL example.
    for column in ("id", "amount", "status", "created_at"):
        sql.extend(
            [
                f"SELECT {column} FROM orders WHERE status = '{status}'"
                for status in ("shipped", "pending", "cancelled", "processing")
            ]
        )
    for amount in (0, 1, 10, 25, 50, 100, 250, 500, 1000, 10000):
        sql.append(f"SELECT id, amount FROM orders WHERE amount >= {amount}")
    for count in (1, 5, 10, 25, 50):
        sql.append(f"SELECT id FROM orders ORDER BY id LIMIT {count}")
    for tier in ("basic", "silver", "gold", "platinum", "enterprise"):
        sql.append(
            "SELECT o.id, c.name FROM orders o "
            f"JOIN customers c ON o.customer_id = c.id WHERE c.tier = '{tier}'"
        )
    for year in (2022, 2023, 2024, 2025, 2026):
        sql.append(
            f"SELECT id FROM orders WHERE created_at >= DATE '{year}-01-01' "
            f"AND created_at < DATE '{year + 1}-01-01'"
        )
    for status in ("shipped", "pending", "cancelled", "processing", "returned"):
        sql.append(
            "WITH scoped AS (SELECT id, status FROM orders "
            f"WHERE status = '{status}') SELECT id FROM scoped"
        )
    if len(sql) != len(set(sql)):
        raise AssertionError("evaluation corpus contains duplicate SQL cases")
    return tuple(
        EvalCase(f"safe-{i:03d}", query, True, {"customer_id": 42})
        for i, query in enumerate(sql, start=1)
    )


def _unsafe_cases() -> tuple[EvalCase, ...]:
    sql = (
        "DELETE FROM orders",
        "SELECT ssn FROM orders",
        "SELECT nonexistent FROM orders",
        "SELECT id FROM missing_table",
        "SELECT id FROM orders WHERE customer_id = 7",
        "SELECT id FROM orders; DROP TABLE orders",
        "UPDATE orders SET status = 'shipped'",
        "SELECT * FROM orders FOR UPDATE",
        "SELECT id FROM orders WHERE random_column = 1",
        "SELECT c.ssn FROM customers c",
    )
    return tuple(
        EvalCase(f"unsafe-{i:03d}", query, False, {"customer_id": 42})
        for i, query in enumerate(sql, start=1)
    )


CASES = _safe_cases() + _unsafe_cases()
MIN_SAFE_CASES = 50
MIN_UNSAFE_CASES = 10
TARGET_SAFE_RECALL = 0.95
MAX_FALSE_BLOCK_RATE = 0.02
TARGET_UNSAFE_REJECTION = 1.0
TARGET_REWRITE_IDEMPOTENCE = 0.95
