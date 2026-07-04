"""Athena/Trino: partition-filter enforcement and columnar scan budgeting.

On Athena, cost *is* bytes scanned, so the two controls that matter most are
(1) forcing a partition filter and (2) capping scanned bytes. This example
uses a hand-written catalog; swap in ``catalog_from_glue("your_db")`` to
reflect a live AWS Glue Data Catalog (partition keys + crawler statistics).

    python examples/athena_example.py
"""

from __future__ import annotations

from sqlguard import Catalog, Policy, RLSRule, SQLGuard


def build_guard() -> SQLGuard:
    # For live reflection instead:
    #     from sqlguard.athena import catalog_from_glue
    #     catalog = catalog_from_glue("analytics_db")
    catalog = Catalog.from_dict(
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
                    "total_bytes": 4 << 40,  # 4 TiB
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
                    "total_bytes": 200 << 20,
                    "columnar": True,
                },
            }
        },
        nested=True,
    )
    return SQLGuard(
        catalog,
        Policy(
            rls=[RLSRule(table="events", column="tenant_id", param="tenant")],
            max_bytes_scanned=100 << 30,   # 100 GiB budget
            partition_selectivity=0.02,    # one day out of ~50
            default_limit=1000,
        ),
        dialect="athena",
    )


def main() -> None:
    guard = build_guard()
    tenant = {"tenant": 7}

    queries = [
        # blocked: no partition filter → full 4 TiB scan
        "SELECT event_id FROM analytics.events",
        # ok: partition-pruned + column-pruned + tenant-scoped
        "SELECT event_id, event_type FROM analytics.events WHERE dt = '2026-07-01'",
        # ok: struct field access is not a hallucinated table
        "SELECT payload.referrer FROM analytics.events WHERE dt = '2026-07-01'",
        # ok: join to a dimension, tenant filter injected on events
        "SELECT e.event_id, u.name FROM analytics.events e "
        "JOIN analytics.dim_user u ON e.user_id = u.user_id WHERE e.dt = '2026-07-01'",
        # blocked: CTAS is a write
        "CREATE TABLE tmp AS SELECT * FROM analytics.events",
    ]

    for sql in queries:
        result = guard.validate(sql, params=tenant)
        print("=" * 78)
        print("INPUT :", sql)
        if result.valid:
            print("STATUS: ✅ valid")
            print("OUTPUT:", result.sql)
            if result.stats.cost and result.stats.cost.bytes_scanned:
                gib = result.stats.cost.bytes_scanned / (1 << 30)
                print(f"   scan estimate: {gib:.2f} GiB")
        else:
            print("STATUS: ⛔ blocked")
            print(result.feedback())
    print("=" * 78)


if __name__ == "__main__":
    main()
