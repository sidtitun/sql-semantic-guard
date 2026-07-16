"""Pipeline latency benchmark with a CI regression gate.

Wall-clock on shared CI runners is noisy, so every scenario is reported as a
*ratio* to a fixed CPU-bound calibration loop measured in the same process.
Ratios are stable across machine speeds; the gate compares ratios only.

Usage:
    python benchmarks/bench_pipeline.py                    # human output
    python benchmarks/bench_pipeline.py --json             # machine output
    python benchmarks/bench_pipeline.py --update-baseline  # rewrite baseline.json
    python benchmarks/bench_pipeline.py --check [--tolerance 0.30]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from sqlguard import Catalog, ColumnRule, Policy, RLSRule, SQLGuard

BASELINE_PATH = Path(__file__).parent / "baseline.json"

CATALOG = Catalog.from_dict(
    {
        "orders": {
            "columns": {
                "id": "bigint", "customer_id": "bigint", "amount": "decimal(10,2)",
                "status": "varchar(32)", "ssn": {"type": "text", "tags": ["pii"]},
                "created_at": "timestamp", "notes": "text",
            },
            "row_count": 50_000_000, "total_bytes": 8 << 30,
        },
        "customers": {
            "columns": {
                "id": "bigint", "customer_id": "bigint", "name": "varchar(120)",
                "email": {"type": "varchar(255)", "tags": ["pii"]},
                "tier": "varchar(16)", "region": "varchar(8)",
            },
            "row_count": 2_000_000, "total_bytes": 512 << 20,
        },
        "line_items": {
            "columns": {
                "id": "bigint", "order_id": "bigint", "sku": "varchar(64)",
                "quantity": "integer", "unit_price": "decimal(10,2)",
            },
            "row_count": 200_000_000, "total_bytes": 20 << 30,
        },
    }
)

GUARD = SQLGuard(
    CATALOG,
    Policy(
        rls=[RLSRule(table="orders", column="customer_id", param="customer_id")],
        column_rules=[ColumnRule(tags=frozenset({"pii"}), action="deny")],
        max_bytes_scanned=64 << 30,
        on_missing_stats="ignore",
    ),
    dialect="postgres",
)
PARAMS = {"customer_id": 42}

SCENARIOS = {
    "simple": "SELECT id, amount FROM orders WHERE status = 'shipped'",
    "star_pii_rls": "SELECT * FROM orders",
    "medium_cte_joins": """
        WITH recent AS (
            SELECT id, customer_id, amount FROM orders WHERE created_at > '2026-01-01'
        )
        SELECT r.id, c.name, li.sku
        FROM recent r
        JOIN customers c ON r.customer_id = c.id
        JOIN line_items li ON li.order_id = r.id
        WHERE c.tier = 'gold'
        ORDER BY r.amount DESC
    """,
    "invalid_three_errors": "SELECT bogus1, bogus2 FROM orderz WHERE customer_id = 999",
}


def _calibrate() -> float:
    """Median seconds for a fixed CPU-bound workload (the ratio denominator)."""
    def workload() -> int:
        acc = 0
        for i in range(200_000):
            acc += i * i % 7
        return acc

    workload()  # warmup
    times = []
    for _ in range(15):
        t0 = time.perf_counter()
        workload()
        times.append(time.perf_counter() - t0)
    return statistics.median(times)


def run(iterations: int = 150) -> dict:
    unit = _calibrate()
    results: dict[str, dict] = {}
    for name, sql in SCENARIOS.items():
        for _ in range(15):  # warmup
            GUARD.validate(sql, params=PARAMS)
        times = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            GUARD.validate(sql, params=PARAMS)
            times.append(time.perf_counter() - t0)
        median = statistics.median(times)
        results[name] = {
            "median_us": round(median * 1e6, 1),
            "ratio": round(median / unit, 4),
        }
    return {"calibration_unit_us": round(unit * 1e6, 1), "scenarios": results}


def check(current: dict, baseline: dict, tolerance: float) -> int:
    failures = []
    for name, entry in baseline["scenarios"].items():
        cur = current["scenarios"].get(name)
        if cur is None:
            failures.append(f"{name}: missing from current run")
            continue
        allowed = entry["ratio"] * (1 + tolerance)
        if cur["ratio"] > allowed:
            failures.append(
                f"{name}: ratio {cur['ratio']:.3f} exceeds baseline "
                f"{entry['ratio']:.3f} by more than {tolerance:.0%}"
            )
    if failures:
        print("BENCHMARK REGRESSION:", file=sys.stderr)
        for f in failures:
            print("  -", f, file=sys.stderr)
        print(
            "If intentional, refresh with: "
            "python benchmarks/bench_pipeline.py --update-baseline",
            file=sys.stderr,
        )
        return 1
    print(f"benchmark gate OK (tolerance {tolerance:.0%})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--tolerance", type=float, default=0.30)
    parser.add_argument("--iterations", type=int, default=150)
    args = parser.parse_args()

    current = run(iterations=args.iterations)
    if args.update_baseline:
        BASELINE_PATH.write_text(json.dumps(current, indent=2) + "\n")
        print(f"baseline written to {BASELINE_PATH}")
        return 0
    if args.check:
        baseline = json.loads(BASELINE_PATH.read_text())
        return check(current, baseline, args.tolerance)
    if args.json:
        print(json.dumps(current, indent=2))
    else:
        print(f"calibration unit: {current['calibration_unit_us']} µs")
        for name, entry in current["scenarios"].items():
            print(f"  {name:24} {entry['median_us']:>9} µs   ratio {entry['ratio']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
