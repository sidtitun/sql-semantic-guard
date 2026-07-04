"""Postgres: catalog-aware validation, RLS, and (optional) EXPLAIN cost gating.

Run against a hand-written catalog (no DB needed):

    python examples/postgres_example.py

Or reflect a live database and gate on the planner:

    DATABASE_URL=postgresql://user:pass@localhost/mydb \
        python examples/postgres_example.py
"""

from __future__ import annotations

import os

from sqlguard import Catalog, ColumnRule, Policy, RLSRule, SQLGuard


def build_guard() -> SQLGuard:
    db_url = os.environ.get("DATABASE_URL")
    policy_kwargs = dict(
        rls=[RLSRule(table="orders", column="customer_id", param="customer_id")],
        column_rules=[ColumnRule(tags={"pii"}, action="deny", reason="PII")],
        default_limit=1000,
        max_limit=10_000,
        max_bytes_scanned=8 << 30,
    )

    if db_url:
        # Live reflection: pulls columns, types, and pg_class size estimates,
        # then (optionally) gates on the real planner via EXPLAIN.
        from sqlalchemy import create_engine

        from sqlguard.postgres import PostgresExplainEstimator
        from sqlguard.reflect import catalog_from_sqlalchemy
        from sqlguard.cost import HeuristicCostEstimator

        engine = create_engine(db_url)
        catalog = catalog_from_sqlalchemy(engine, schemas=["public"])
        return SQLGuard(
            catalog,
            Policy(**policy_kwargs),
            dialect="postgres",
            estimators=[
                HeuristicCostEstimator(),
                PostgresExplainEstimator(engine, max_total_cost=5_000_000),
            ],
        )

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
                "row_count": 50_000_000,
                "total_bytes": 8 << 30,
            },
            "customers": {
                "columns": {
                    "id": "bigint",
                    "name": "varchar(120)",
                    "email": {"type": "varchar(255)", "tags": ["pii"]},
                    "tier": "varchar(16)",
                },
                "row_count": 2_000_000,
                "total_bytes": 512 << 20,
            },
        }
    )
    return SQLGuard(catalog, Policy(**policy_kwargs), dialect="postgres")


def main() -> None:
    guard = build_guard()
    tenant = {"customer_id": 42}

    queries = [
        "SELECT * FROM orders WHERE status = 'shipped'",         # star + rls + limit
        "SELECT ssn FROM orders",                                # blocked: PII
        "SELECT amont FROM orders",                              # blocked: typo
        "DELETE FROM orders",                                    # blocked: write
        "SELECT id FROM orders WHERE customer_id = 999",         # blocked: tenant spoof
        "SELECT o.id, c.name FROM orders o "
        "JOIN customers c ON o.customer_id = c.id",              # multi-table, rls
    ]

    for sql in queries:
        result = guard.validate(sql, params=tenant)
        print("=" * 78)
        print("INPUT :", sql)
        if result.valid:
            print("STATUS: ✅ valid")
            print("OUTPUT:", result.sql)
            for rw in result.rewrites:
                print("   ~", rw.message)
            if result.stats.cost and result.stats.cost.bytes_scanned:
                gib = result.stats.cost.bytes_scanned / (1 << 30)
                print(f"   scan estimate: {gib:.2f} GiB")
        else:
            print("STATUS: ⛔ blocked")
            print(result.feedback())
    print("=" * 78)


if __name__ == "__main__":
    main()
