"""Shared fixtures: a realistic multi-tenant e-commerce catalog."""

from __future__ import annotations

import pytest

from sqlguard import Catalog, ColumnRule, Policy, RLSRule, SQLGuard


@pytest.fixture
def catalog() -> Catalog:
    return Catalog.from_dict(
        {
            "orders": {
                "columns": {
                    "id": "bigint",
                    "customer_id": "bigint",
                    "amount": "decimal(10,2)",
                    "status": "varchar(32)",
                    "ssn": {"type": "text", "tags": ["pii"]},
                    "created_at": "timestamp",
                    "notes": "text",
                },
                "row_count": 50_000_000,
                "total_bytes": 8 * (1 << 30),
            },
            "customers": {
                "columns": {
                    "id": "bigint",
                    "customer_id": "bigint",
                    "name": "varchar(120)",
                    "email": {"type": "varchar(255)", "tags": ["pii"]},
                    "tier": "varchar(16)",
                    "region": "varchar(8)",
                },
                "row_count": 2_000_000,
                "total_bytes": 512 * (1 << 20),
            },
            "line_items": {
                "columns": {
                    "id": "bigint",
                    "order_id": "bigint",
                    "sku": "varchar(64)",
                    "quantity": "integer",
                    "unit_price": "decimal(10,2)",
                },
                "row_count": 200_000_000,
                "total_bytes": 20 * (1 << 30),
            },
            "audit_log": {
                "columns": {
                    "id": "bigint",
                    "actor": "text",
                    "action": "text",
                    "at": "timestamp",
                }
            },
        }
    )


@pytest.fixture
def guard(catalog: Catalog) -> SQLGuard:
    policy = Policy(
        rls=[RLSRule(table="orders", column="customer_id", param="customer_id")],
        column_rules=[ColumnRule(tags=frozenset({"pii"}), action="deny", reason="PII")],
        default_limit=1000,
        max_limit=10_000,
        on_missing_stats="ignore",
    )
    return SQLGuard(catalog, policy, dialect="postgres")


@pytest.fixture
def plain_guard(catalog: Catalog) -> SQLGuard:
    """A guard with no RLS/column rules, for isolating individual checks."""
    return SQLGuard(catalog, Policy(on_missing_stats="ignore"), dialect="postgres")


@pytest.fixture
def athena_catalog() -> Catalog:
    return Catalog.from_dict(
        {
            "analytics": {
                "events": {
                    "columns": {
                        "event_id": "string",
                        "user_id": "bigint",
                        "tenant_id": "bigint",
                        "event_type": "string",
                        "payload": "struct<referrer:string,ua:string>",
                        "dt": "string",
                    },
                    "partition_columns": ["dt"],
                    "row_count": 10_000_000_000,
                    "total_bytes": 4 * (1 << 40),
                    "columnar": True,
                },
                "dim_user": {
                    "columns": {
                        "user_id": "bigint",
                        "tenant_id": "bigint",
                        "name": "string",
                        "country": "string",
                    },
                    "row_count": 5_000_000,
                    "total_bytes": 200 * (1 << 20),
                    "columnar": True,
                },
            }
        },
        nested=True,
    )
