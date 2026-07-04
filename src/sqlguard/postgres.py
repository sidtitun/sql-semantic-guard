"""Postgres-specific cost gating via the planner itself.

``EXPLAIN`` (without ANALYZE) plans the query but reads no table data, so it
is safe to run on untrusted-but-validated SQL. Add this estimator *after* the
heuristic one — SQLGuard only invokes secondary estimators when the query has
no blocking violations, so EXPLAIN never sees SQL that failed validation.

Example::

    guard = SQLGuard(
        catalog, policy, dialect="postgres",
        estimators=[
            HeuristicCostEstimator(),
            PostgresExplainEstimator(engine, max_total_cost=1_000_000),
        ],
    )
"""

from __future__ import annotations

import json
from typing import Any, Callable

from sqlguard.cost import EstimateInputs
from sqlguard.violations import Code, CostEstimate, Severity, Violation


class PostgresExplainEstimator:
    """Gate queries on the Postgres planner's own cost estimate.

    Args:
        engine: a SQLAlchemy engine (mutually exclusive with
            ``connection_factory``).
        connection_factory: zero-arg callable returning a DBAPI connection
            (it will be closed after use).
        max_total_cost: reject when the plan's total cost (planner units)
            exceeds this.
        max_rows: reject when the planner expects more than this many rows
            out of the top plan node.
    """

    def __init__(
        self,
        engine: Any = None,
        connection_factory: Callable[[], Any] | None = None,
        max_total_cost: float | None = None,
        max_rows: int | None = None,
    ) -> None:
        if (engine is None) == (connection_factory is None):
            raise ValueError("Provide exactly one of engine= or connection_factory=")
        self.engine = engine
        self.connection_factory = connection_factory
        self.max_total_cost = max_total_cost
        self.max_rows = max_rows

    # -- plumbing ----------------------------------------------------------

    def _explain(self, sql: str) -> Any:
        statement = f"EXPLAIN (FORMAT JSON) {sql}"
        if self.engine is not None:
            with self.engine.connect() as conn:
                row = conn.exec_driver_sql(statement).fetchone()
                payload = row[0]
        else:
            conn = self.connection_factory()  # type: ignore[misc]
            try:
                cursor = conn.cursor()
                try:
                    cursor.execute(statement)
                    payload = cursor.fetchone()[0]
                finally:
                    cursor.close()
            finally:
                conn.close()
        if isinstance(payload, str):
            payload = json.loads(payload)
        return payload

    # -- estimator protocol --------------------------------------------------

    def estimate(
        self, sql: str, inputs: EstimateInputs
    ) -> tuple[CostEstimate | None, list[Violation]]:
        try:
            payload = self._explain(sql)
            plan = payload[0]["Plan"]
            total_cost = float(plan["Total Cost"])
            plan_rows = int(plan.get("Plan Rows", 0))
        except Exception as e:
            return None, [
                Violation(
                    Code.COST_ESTIMATION_FAILED,
                    Severity.WARNING,
                    f"EXPLAIN-based cost estimation failed: {type(e).__name__}: {e}",
                    hint="The query was not cost-gated by the planner.",
                )
            ]

        violations: list[Violation] = []
        if self.max_total_cost is not None and total_cost > self.max_total_cost:
            violations.append(
                Violation(
                    Code.SCAN_BUDGET_EXCEEDED,
                    Severity.ERROR,
                    f"Postgres planner cost {total_cost:,.0f} exceeds the budget "
                    f"of {self.max_total_cost:,.0f}",
                    hint="Narrow the query with more selective filters.",
                    extra={"total_cost": total_cost, "max_total_cost": self.max_total_cost},
                )
            )
        if self.max_rows is not None and plan_rows > self.max_rows:
            violations.append(
                Violation(
                    Code.SCAN_BUDGET_EXCEEDED,
                    Severity.ERROR,
                    f"Postgres planner expects {plan_rows:,} rows, over the budget "
                    f"of {self.max_rows:,}",
                    extra={"plan_rows": plan_rows, "max_rows": self.max_rows},
                )
            )
        return (
            CostEstimate(source="postgres_explain", total_cost=total_cost, rows_scanned=plan_rows),
            violations,
        )
