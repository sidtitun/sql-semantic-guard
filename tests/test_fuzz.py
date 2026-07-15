"""Property-based fuzzing: the guard must never crash and never fail open.

Three input distributions:
  A. grammatical queries assembled from the catalog (mostly valid),
  B. mutated queries (A with injected typos/corruption — mostly invalid),
  C. adversarial text (random unicode + classic injection strings).

Five invariants, each its own property. CI runs a modest example count; a
nightly job can raise it via HYPOTHESIS_PROFILE=nightly.
"""

from __future__ import annotations

import os

import sqlglot
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlguard import Catalog, ColumnRule, Policy, RLSRule, SQLGuard
from sqlguard.analyzer import statement_gate
from sqlguard.catalog import CatalogIndex
from sqlguard.rls import _scope_predicate_pool
from sqlguard.semantics import SourceKind, build_scope_maps

settings.register_profile(
    "default", max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile(
    "nightly", max_examples=10_000, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

CATALOG = Catalog.from_dict(
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
            "row_count": 1_000_000,
            "total_bytes": 1 << 30,
        },
        "customers": {
            "columns": {
                "id": "bigint",
                "customer_id": "bigint",
                "name": "varchar(120)",
                "tier": "varchar(16)",
            },
            "row_count": 10_000,
            "total_bytes": 1 << 20,
        },
    }
)
GUARD = SQLGuard(
    CATALOG,
    Policy(
        rls=[RLSRule(table="orders", column="customer_id", param="tenant")],
        column_rules=[ColumnRule(tags=frozenset({"pii"}), action="deny")],
        max_bytes_scanned=64 << 30,
        on_missing_stats="ignore",
    ),
    dialect="postgres",
)
PARAMS = {"tenant": 7}
INDEX = CatalogIndex(CATALOG, "postgres")

# -- strategy A: grammatical ------------------------------------------------

_COLS = ["id", "customer_id", "amount", "status", "created_at", "*"]
_PREDS = [
    "amount > 100",
    "status = 'shipped'",
    "created_at > '2026-01-01'",
    "customer_id = 7",
    "id IN (1, 2, 3)",
    "amount BETWEEN 10 AND 20",
]


@st.composite
def grammatical(draw) -> str:
    cols = draw(st.lists(st.sampled_from(_COLS), min_size=1, max_size=3, unique=True))
    projection = ", ".join(c if c == "*" else f"o.{c}" for c in cols)
    sql = f"SELECT {projection} FROM orders o"
    if draw(st.booleans()):
        kind = draw(st.sampled_from(["JOIN", "LEFT JOIN"]))
        sql += f" {kind} customers c ON o.customer_id = c.id"
    preds = draw(st.lists(st.sampled_from(_PREDS), max_size=2))
    if preds:
        sql += " WHERE " + " AND ".join(f"o.{p}" if not p.startswith("o.") else p for p in preds)
    if draw(st.booleans()):
        sql += " ORDER BY o.id"
    if draw(st.booleans()):
        sql += f" LIMIT {draw(st.integers(min_value=1, max_value=100000))}"
    if draw(st.booleans()):
        cte = draw(st.sampled_from(["orders", "customers"]))
        sql = f"WITH src AS (SELECT id FROM {cte}) {sql}"
    return sql


# -- strategy B: mutations ----------------------------------------------------


def _mutate(sql: str, choices: list[int]) -> str:
    out = sql
    for c in choices:
        if not out:
            break
        pos = c % max(len(out), 1)
        op = c % 5
        if op == 0:
            out = out[:pos] + out[pos + 1 :]  # delete a char
        elif op == 1:
            out = out[:pos] + "zz" + out[pos:]  # corrupt an identifier
        elif op == 2:
            out = out[:pos] + "'" + out[pos:]  # unbalanced quote
        elif op == 3:
            out = out + "; DROP TABLE orders"  # stack a statement
        else:
            out = out.replace("SELECT", "DELETE", 1)  # flip the verb
    return out


mutated = st.builds(
    _mutate,
    grammatical(),
    st.lists(st.integers(min_value=0, max_value=10_000), min_size=1, max_size=4),
)

# -- strategy C: adversarial ---------------------------------------------------

_INJECTION_CORPUS = [
    "' OR 1=1 --",
    "SELECT * FROM orders; DELETE FROM orders",
    "SELECT * FROM orders WHERE id = 1 UNION SELECT ssn, 1 FROM orders",
    "SELECT/**/ssn/**/FROM/**/orders",
    "WITH x AS (UPDATE orders SET amount=0 RETURNING id) SELECT * FROM x",
    "\x00SELECT 1",
    "sElEcT * fRoM orders WhErE customer_id = 999",
    "SELECT pg_sleep(9999)",
    "SELECT id INTO evil FROM orders",
]
adversarial = st.one_of(st.sampled_from(_INJECTION_CORPUS), st.text(max_size=200))

any_input = st.one_of(grammatical(), mutated, adversarial)


# -- invariant 1: never raises -------------------------------------------------


@given(any_input)
def test_never_crashes(sql):
    GUARD.validate(sql, params=PARAMS)  # any outcome but an exception


# -- invariant 2: fail closed ---------------------------------------------------


@given(any_input)
def test_invalid_has_no_sql(sql):
    result = GUARD.validate(sql, params=PARAMS)
    if not result.valid:
        assert result.sql is None


# -- invariant 3: output self-audit ----------------------------------------------


@given(any_input)
def test_valid_output_is_a_pure_select(sql):
    result = GUARD.validate(sql, params=PARAMS)
    if not result.valid:
        return
    tree = sqlglot.parse_one(result.sql, read="postgres")
    # Re-run the statement gate on our own OUTPUT: no write/DDL/command/lock
    # may ever survive to executable SQL.
    assert statement_gate(tree, "postgres") == []


# -- invariant 4: idempotency ------------------------------------------------------


@given(grammatical())
def test_revalidating_output_is_stable(sql):
    first = GUARD.validate(sql, params=PARAMS)
    if not first.valid:
        return
    second = GUARD.validate(first.sql, params=PARAMS)
    assert second.valid, [str(v) for v in second.errors]
    assert second.sql == first.sql


# -- invariant 5: RLS always present -------------------------------------------------


@given(any_input)
def test_rls_predicate_reaches_every_orders_scope(sql):
    result = GUARD.validate(sql, params=PARAMS)
    if not result.valid:
        return
    tree = sqlglot.parse_one(result.sql, read="postgres")
    infos, _, _ = build_scope_maps(tree, INDEX)
    for info in infos:
        for alias, src in info.sources.items():
            if src.kind is not SourceKind.TABLE or src.table is None:
                continue
            if src.table.name != "orders":
                continue
            select = info.expression
            pool = _scope_predicate_pool(select) if select.__class__.__name__ == "Select" else []
            texts = " ".join(p.sql() for p in pool)
            assert "customer_id = 7" in texts.replace(f"{alias}.", "").replace(
                "orders.", ""
            ) or f"{alias}.customer_id = 7" in texts, (
                f"unfiltered orders reference (alias {alias!r}) in: {result.sql}"
            )


# -- regression corpus: crashes found by fuzzing become permanent tests ------------


REGRESSION_CORPUS = [
    # seed entries; extend with minimized failures from nightly runs
    "",
    ";;;",
    "(((((SELECT 1)))))",
    "SELECT",
    "SELECT * FROM",
    "WITH x AS (SELECT 1)",
    "SELECT * FROM orders LIMIT 'ten'",
]


def test_regression_corpus_never_crashes():
    for sql in REGRESSION_CORPUS:
        result = GUARD.validate(sql, params=PARAMS)
        if not result.valid:
            assert result.sql is None
