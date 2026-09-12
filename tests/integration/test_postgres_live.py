"""Live Postgres integration tests (roadmap 0.2).

Skipped unless SQLGUARD_PG_URL points at a disposable database, e.g.::

    SQLGUARD_PG_URL=postgresql+psycopg2://postgres:postgres@localhost/postgres

CI provides one via a `services: postgres` container. These tests seed real
tables, reflect them, and assert the guard's promises against actual rows —
including THE security assertion: an RLS-filtered query returns only the
caller's tenant rows.
"""

from __future__ import annotations

import os

import pytest

from sqlguard import Policy, RLSRule, SQLGuard
from sqlguard.cost import HeuristicCostEstimator
from sqlguard.postgres import PostgresExplainEstimator
from sqlguard.reflect import catalog_from_sqlalchemy

pytestmark = pytest.mark.integration

PG_URL = os.environ.get("SQLGUARD_PG_URL")
if not PG_URL:
    pytest.skip("SQLGUARD_PG_URL not set", allow_module_level=True)

sqlalchemy = pytest.importorskip("sqlalchemy")


@pytest.fixture(scope="module")
def engine():
    eng = sqlalchemy.create_engine(PG_URL)
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS sg_orders, sg_customers CASCADE")
        conn.exec_driver_sql(
            "CREATE TABLE sg_customers (id BIGINT PRIMARY KEY, name TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE sg_orders (id BIGSERIAL PRIMARY KEY, customer_id BIGINT, "
            "amount NUMERIC(10,2), status VARCHAR(32))"
        )
        conn.exec_driver_sql(
            "INSERT INTO sg_customers SELECT g, 'customer ' || g FROM generate_series(1, 100) g"
        )
        conn.exec_driver_sql(
            "INSERT INTO sg_orders (customer_id, amount, status) "
            "SELECT (g %% 100) + 1, (g %% 500)::numeric, "
            "CASE WHEN g %% 2 = 0 THEN 'shipped' ELSE 'pending' END "
            "FROM generate_series(1, 100000) g"
        )
        conn.exec_driver_sql("ANALYZE sg_orders; ANALYZE sg_customers")
    yield eng
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS sg_orders, sg_customers CASCADE")


@pytest.fixture(scope="module")
def guard(engine):
    catalog = catalog_from_sqlalchemy(engine, schemas=["public"])
    return SQLGuard(
        catalog,
        Policy(
            rls=[RLSRule(table="sg_orders", column="customer_id", param="tenant")],
            default_limit=500,
            on_missing_stats="ignore",
        ),
        dialect="postgres",
        estimators=[
            HeuristicCostEstimator(),
            PostgresExplainEstimator(engine=engine, max_total_cost=5_000_000),
        ],
    )


def test_reflection_includes_pg_stats(engine):
    catalog = catalog_from_sqlalchemy(engine, schemas=["public"])
    orders = next(t for t in catalog.tables if t.name == "sg_orders")
    assert orders.row_count is not None
    assert 50_000 <= orders.row_count <= 200_000  # within 2x of the 100k truth
    assert orders.total_bytes and orders.total_bytes > 0


def test_rls_returns_only_tenant_rows(engine, guard):
    """THE end-to-end security assertion, against real rows."""
    result = guard.validate(
        "SELECT customer_id FROM sg_orders", params={"tenant": 7}
    )
    assert result.valid, [str(v) for v in result.errors]
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(result.sql).fetchall()
    assert rows, "tenant 7 must have rows"
    assert {r[0] for r in rows} == {7}
    assert len(rows) <= 500  # LIMIT applied


def test_explain_gate_blocks_query_on_planner_cost(engine):
    """Prove the live planner estimator runs and causes the rejection."""
    catalog = catalog_from_sqlalchemy(engine, schemas=["public"])
    explain_guard = SQLGuard(
        catalog,
        Policy(
            rls=[RLSRule(table="sg_orders", column="customer_id", param="tenant")],
            default_limit=500,
            check_joins=False,
            on_missing_stats="ignore",
        ),
        dialect="postgres",
        estimators=[PostgresExplainEstimator(engine=engine, max_total_cost=0)],
    )

    result = explain_guard.validate(
        "SELECT id FROM sg_orders",
        params={"tenant": 7},
    )

    assert not result.valid
    assert result.stats.cost is not None
    assert result.stats.cost.source == "postgres_explain"
    assert result.stats.cost.total_cost is not None
    assert result.stats.cost.total_cost > 0
    assert any(v.code.value == "scan_budget_exceeded" for v in result.errors)


def test_rewritten_sql_executes(engine, guard):
    result = guard.validate(
        "SELECT * FROM sg_orders WHERE status = 'shipped'", params={"tenant": 3}
    )
    assert result.valid, [str(v) for v in result.errors]
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(result.sql).fetchall()
    assert 0 < len(rows) <= 500
