"""Adapter coverage: Glue reflection, EXPLAIN estimator, SQLAlchemy reflection.

All fakes are hand-rolled — no network, no cloud credentials, no moto.
"""

from __future__ import annotations

import json

import pytest
from sqlglot import parse_one

from sqlguard import Catalog, CostEstimate, Policy, SQLGuard
from sqlguard.athena import catalog_from_glue
from sqlguard.catalog import CatalogIndex
from sqlguard.cost import EstimateInputs
from sqlguard.postgres import PostgresExplainEstimator
from sqlguard.reflect import catalog_from_sqlalchemy

# ---------------------------------------------------------------------------
# Glue
# ---------------------------------------------------------------------------


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.pages)


class FakeGlueClient:
    def __init__(self, pages):
        self.paginator = FakePaginator(pages)

    def get_paginator(self, name):
        assert name == "get_tables"
        return self.paginator


GLUE_PAGES = [
    {
        "TableList": [
            {
                "Name": "events",
                "Description": "clickstream",
                "StorageDescriptor": {
                    "Columns": [
                        {"Name": "event_id", "Type": "string"},
                        {"Name": "user_id", "Type": "bigint", "Comment": "fk"},
                    ],
                    "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                },
                "PartitionKeys": [{"Name": "dt", "Type": "string"}],
                "Parameters": {"numRows": "1000000", "totalSize": "5368709120"},
            }
        ]
    },
    {
        "TableList": [
            {
                "Name": "raw_logs",
                "StorageDescriptor": {
                    "Columns": [{"Name": "line", "Type": "string"}],
                    "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
                },
                "Parameters": {"recordCount": "42", "rawDataSize": "not-a-number"},
            }
        ]
    },
]


def test_glue_reflection_end_to_end():
    catalog = catalog_from_glue("analytics", client=FakeGlueClient(GLUE_PAGES))
    assert {t.name for t in catalog.tables} == {"events", "raw_logs"}
    events = next(t for t in catalog.tables if t.name == "events")

    # partition key is both a queryable column and a partition column
    assert events.partition_columns == ("dt",)
    assert events.column("dt") is not None
    assert events.column("user_id").comment == "fk"
    assert events.schema == "analytics"

    # stats parsed from Parameters
    assert events.row_count == 1_000_000
    assert events.total_bytes == 5 * (1 << 30)
    assert events.columnar is True
    assert events.comment == "clickstream"


def test_glue_stats_fallback_and_bad_values():
    catalog = catalog_from_glue("analytics", client=FakeGlueClient(GLUE_PAGES))
    raw = next(t for t in catalog.tables if t.name == "raw_logs")
    assert raw.row_count == 42  # recordCount fallback
    assert raw.total_bytes is None  # non-numeric rawDataSize ignored
    assert raw.columnar is None  # text format => unknown, not False-positive


def test_glue_include_stats_false():
    catalog = catalog_from_glue(
        "analytics", client=FakeGlueClient(GLUE_PAGES), include_stats=False
    )
    events = next(t for t in catalog.tables if t.name == "events")
    assert events.row_count is None and events.total_bytes is None


def test_glue_catalog_id_passthrough():
    client = FakeGlueClient(GLUE_PAGES)
    catalog_from_glue("analytics", client=client, catalog_id="123456789012")
    assert client.paginator.calls == [
        {"DatabaseName": "analytics", "CatalogId": "123456789012"}
    ]


def test_glue_catalog_drives_the_guard():
    """A reflected catalog must work in the real pipeline, not just parse."""
    catalog = catalog_from_glue("analytics", client=FakeGlueClient(GLUE_PAGES))
    guard = SQLGuard(catalog, Policy(on_missing_stats="ignore"), dialect="athena")
    blocked = guard.validate("SELECT event_id FROM analytics.events")
    assert not blocked.valid  # partition filter enforced from reflected keys
    ok = guard.validate(
        "SELECT event_id FROM analytics.events WHERE dt = '2026-07-01'"
    )
    assert ok.valid, [str(v) for v in ok.errors]


# ---------------------------------------------------------------------------
# PostgresExplainEstimator
# ---------------------------------------------------------------------------


class FakeResult:
    def __init__(self, payload):
        self.payload = payload

    def fetchone(self):
        return [self.payload]


class FakeSAConnection:
    def __init__(self, payload, log):
        self.payload = payload
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def exec_driver_sql(self, statement):
        self.log.append(statement)
        if isinstance(self.payload, Exception):
            raise self.payload
        return FakeResult(self.payload)


class FakeEngine:
    def __init__(self, payload):
        self.payload = payload
        self.statements = []

    def connect(self):
        return FakeSAConnection(self.payload, self.statements)


def _inputs() -> EstimateInputs:
    catalog = Catalog.from_dict({"t": {"a": "int"}})
    index = CatalogIndex(catalog, "postgres")
    return EstimateInputs(
        tree=parse_one("SELECT a FROM t", read="postgres"),
        index=index,
        policy=Policy(on_missing_stats="ignore"),
        dialect="postgres",
    )


def _plan(cost=1234.5, rows=99):
    return [{"Plan": {"Total Cost": cost, "Plan Rows": rows}}]


def test_explain_estimator_reads_plan():
    engine = FakeEngine(_plan())
    est = PostgresExplainEstimator(engine=engine)
    estimate, violations = est.estimate("SELECT a FROM t", _inputs())
    assert violations == []
    assert isinstance(estimate, CostEstimate)
    assert estimate.total_cost == 1234.5
    assert estimate.rows_scanned == 99
    assert engine.statements == ["EXPLAIN (FORMAT JSON) SELECT a FROM t"]


def test_explain_estimator_parses_string_payload():
    engine = FakeEngine(json.dumps(_plan(cost=10.0)))
    estimate, _ = PostgresExplainEstimator(engine=engine).estimate("SELECT 1", _inputs())
    assert estimate.total_cost == 10.0


def test_explain_estimator_cost_budget():
    est = PostgresExplainEstimator(engine=FakeEngine(_plan(cost=2e6)), max_total_cost=1e6)
    _, violations = est.estimate("SELECT a FROM t", _inputs())
    assert any(v.code.value == "scan_budget_exceeded" for v in violations)
    assert all(v.is_error for v in violations)


def test_explain_estimator_row_budget():
    est = PostgresExplainEstimator(engine=FakeEngine(_plan(rows=5000)), max_rows=100)
    _, violations = est.estimate("SELECT a FROM t", _inputs())
    assert any(v.code.value == "scan_budget_exceeded" for v in violations)


def test_explain_estimator_failure_degrades_to_warning():
    est = PostgresExplainEstimator(engine=FakeEngine(RuntimeError("boom")))
    estimate, violations = est.estimate("SELECT a FROM t", _inputs())
    assert estimate is None
    assert len(violations) == 1
    assert violations[0].code.value == "cost_estimation_failed"
    assert not violations[0].is_error  # warning: query not blocked, but visible


def test_explain_estimator_dbapi_path_closes_everything():
    events = []

    class FakeCursor:
        def execute(self, stmt):
            events.append(("execute", stmt))

        def fetchone(self):
            return [_plan(cost=7.0)]

        def close(self):
            events.append(("cursor_close", None))

    class FakeDBAPIConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            events.append(("conn_close", None))

    est = PostgresExplainEstimator(connection_factory=FakeDBAPIConn)
    estimate, violations = est.estimate("SELECT 1", _inputs())
    assert estimate.total_cost == 7.0 and violations == []
    assert ("cursor_close", None) in events and ("conn_close", None) in events


def test_explain_estimator_requires_exactly_one_source():
    with pytest.raises(ValueError):
        PostgresExplainEstimator()
    with pytest.raises(ValueError):
        PostgresExplainEstimator(engine=FakeEngine(_plan()), connection_factory=lambda: None)


def test_explain_estimator_as_secondary_in_guard():
    """Wired into SQLGuard: heuristic first, EXPLAIN gate second."""
    catalog = Catalog.from_dict({"t": {"a": "int"}})
    guard = SQLGuard(
        catalog,
        Policy(on_missing_stats="ignore"),
        dialect="postgres",
        estimators=[
            # heuristic omitted intentionally: estimator list is caller-owned
            PostgresExplainEstimator(engine=FakeEngine(_plan(cost=99.0))),
        ],
    )
    result = guard.validate("SELECT a FROM t")
    assert result.valid
    assert result.stats.cost.source == "postgres_explain"
    assert result.stats.cost.total_cost == 99.0

    # invalid SQL: secondary estimators must NOT run (index 0 always runs)
    from sqlguard import HeuristicCostEstimator

    engine = FakeEngine(_plan())
    guard2 = SQLGuard(
        catalog,
        Policy(on_missing_stats="ignore"),
        dialect="postgres",
        estimators=[
            HeuristicCostEstimator(),
            PostgresExplainEstimator(engine=engine),
        ],
    )
    bad = guard2.validate("SELECT bogus FROM t")
    assert not bad.valid
    assert engine.statements == []  # EXPLAIN never saw invalid SQL


# ---------------------------------------------------------------------------
# SQLAlchemy reflection (sqlite in-memory)
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_engine():
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id BIGINT NOT NULL, "
            "amount NUMERIC(10,2), status VARCHAR(32))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE VIEW big_orders AS SELECT id, amount FROM orders WHERE amount > 100"
        )
    return engine


def test_sqlalchemy_reflection(sqlite_engine):
    catalog = catalog_from_sqlalchemy(sqlite_engine)
    names = {t.name for t in catalog.tables}
    assert {"orders", "customers", "big_orders"} <= names

    orders = next(t for t in catalog.tables if t.name == "orders")
    assert orders.column("customer_id") is not None
    assert orders.column("customer_id").nullable is False
    assert orders.column("amount").type.upper().startswith("NUMERIC")
    assert orders.columnar is False


def test_sqlalchemy_reflection_excludes_views_when_asked(sqlite_engine):
    catalog = catalog_from_sqlalchemy(sqlite_engine, include_views=False)
    assert "big_orders" not in {t.name for t in catalog.tables}


def test_reflected_catalog_drives_the_guard(sqlite_engine):
    catalog = catalog_from_sqlalchemy(sqlite_engine)
    guard = SQLGuard(catalog, Policy(on_missing_stats="ignore"), dialect="postgres")
    ok = guard.validate("SELECT id, amount FROM orders WHERE status = 'shipped'")
    assert ok.valid, [str(v) for v in ok.errors]
    bad = guard.validate("SELECT nonexistent FROM orders")
    assert not bad.valid


def test_pg_stats_failure_degrades_gracefully():
    """A postgres-looking engine whose stats query fails still reflects."""

    class FailingStatsEngine:
        class dialect:  # noqa: N801 - mimic sqlalchemy attribute
            name = "postgresql"

        def connect(self):
            raise RuntimeError("no pg_class for you")

    # inspect() would also fail on this fake, so drive _pg_stats directly:
    from sqlguard.reflect import _pg_stats

    stats = _pg_stats(FailingStatsEngine(), ["public"])
    assert stats == {}
