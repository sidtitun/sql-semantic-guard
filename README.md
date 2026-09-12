# sql-semantic-guard

**A semantic firewall for LLM-generated SQL.** Companies want text-to-SQL but are
terrified of it — and rightly so. `sql-semantic-guard` takes a generated query
plus your live schema catalog and, *before anything touches the database*:

1. **Blocks writes & DDL** — `INSERT`/`UPDATE`/`DELETE`/`MERGE`/`CREATE`/`DROP`/
   `ALTER`/`TRUNCATE`/`GRANT`, writable CTEs, `SELECT INTO`, `FOR UPDATE`
   locking, and any statement the parser can only treat as an opaque command.
2. **Validates semantics against your real catalog** — hallucinated tables and
   columns, ambiguous references, misused aliases, and type-mismatched
   comparisons, with *scope-aware name binding* (CTEs, subqueries, correlation)
   — not just "is this syntactically valid SQL?"
3. **Injects row-level security** — auto-adds `WHERE tenant_id = :caller`
   predicates to **every** table reference in **every** scope, with join-aware
   placement, and rejects queries that try to read another tenant's rows.
4. **Bounds scan cost** — statically estimates bytes/rows scanned from catalog
   statistics, enforces partition filters (Athena), and blocks queries over
   budget *before* you pay for them.
5. **Rewrites in the safe direction** — expands `SELECT *`, drops restricted
   columns, and adds/clamps `LIMIT`.
6. **Returns a structured, LLM-ready violation report** so the model can
   self-repair.

Everything is composable, dependency-light (just [`sqlglot`](https://github.com/tobymao/sqlglot)),
and fails **closed**: an invalid result never carries executable SQL.

```text
generated SQL ──▶  ┌─────────────────────────────────────────────┐  ──▶ safe SQL
                   │ parse → statement gate → function gate →      │      (rewritten,
   live catalog ──▶│ name binding → qualification → type checks → │       tenant-scoped,
                   │ column policy → RLS injection → limits →      │       LIMITed)
     policy ──────▶│ join sanity → partition filters → cost        │  ──▶ or violations[]
                   └─────────────────────────────────────────────┘      (for self-repair)
```

## Why this exists

The dangerous failures of text-to-SQL are **semantic, not syntactic**:
hallucinated columns, wrong joins, a missing tenant filter, misuse of a
sensitive field. Catching them needs name binding, scope resolution, type
checks, and catalog awareness — which a plain AST parser doesn't do. Today's
options stop at "does it parse / does it execute," so every serious NL2SQL
deployment re-builds the same guardrails by hand. This is that layer, as a
library.

## Install

```bash
pip install sql-semantic-guard                 # core (sqlglot only)
pip install 'sql-semantic-guard[postgres]'     # + SQLAlchemy live reflection
pip install 'sql-semantic-guard[athena]'       # + boto3 / AWS Glue reflection
```

Requires Python 3.9+.

## Quickstart

```python
from sqlguard import SQLGuard, Catalog, Policy, RLSRule, ColumnRule

catalog = Catalog.from_dict({
    "orders": {
        "columns": {
            "id": "bigint",
            "customer_id": "bigint",
            "amount": "decimal(10,2)",
            "ssn": {"type": "text", "tags": ["pii"]},
            "created_at": "timestamp",
        },
        "row_count": 50_000_000,
        "total_bytes": 8 << 30,
    },
})

guard = SQLGuard(
    catalog,
    Policy(
        rls=[RLSRule(table="orders", column="customer_id", param="customer_id")],
        column_rules=[ColumnRule(tags={"pii"}, action="deny", reason="PII")],
        default_limit=1000,
        max_bytes_scanned=16 << 30,
    ),
    dialect="postgres",
)

# The LLM generated this:
result = guard.validate("SELECT * FROM orders", params={"customer_id": 42})

if result.valid:
    run(result.sql)
    # SELECT orders.id, orders.customer_id, orders.amount, orders.created_at
    # FROM orders WHERE orders.customer_id = 42 LIMIT 1000
    #  → SELECT * expanded, ssn (PII) dropped, tenant filter injected, LIMIT added
else:
    print(result.feedback())   # hand back to the LLM to fix
```

A blocked query returns actionable, deterministic feedback:

```python
r = guard.validate("SELECT amont FROM orderz WHERE customer_id = 999",
                   params={"customer_id": 42})
print(r.feedback())
```
```text
The SQL failed validation with 3 error(s).
Errors (must fix):
  1. [unknown_table] Table 'orderz' does not exist in the catalog Did you mean: orders?
  2. [unknown_column] Column 'amont' does not exist in any table in scope Did you mean: amount?
  3. [tenant_filter_conflict] Query filters 'orders'.'customer_id' to 999 but the caller
     context requires 42. Remove that filter; row-level security is applied automatically.
Regenerate the complete SQL statement fixing every error above. Only a single read-only
SELECT statement is allowed.
```

## The self-repair loop

The whole point of a *structured* report is that the model can consume it:

```python
messages = [{"role": "system", "content": guard.policy_prompt()}, ...]

for attempt in range(4):
    sql = llm(messages)
    result = guard.validate(sql, params={"customer_id": user_id})
    if result.valid:
        break
    messages.append({"role": "assistant", "content": sql})
    messages.append({"role": "user", "content": result.feedback()})

rows = run(result.sql) if result.valid else refuse()
```

`guard.policy_prompt()` also emits a schema + rules block to *prevent* many
violations up front — prevention beats repair.

## What each layer catches

| Layer | Example it blocks / fixes | Violation code |
|---|---|---|
| Statement gate | `DELETE FROM orders`; `WITH d AS (DELETE … RETURNING id) SELECT …`; `SELECT … FOR UPDATE`; `SELECT 1; DROP TABLE …` | `disallowed_statement`, `nested_write`, `locking_clause`, `multiple_statements` |
| Function gate | `pg_sleep(10)`, `pg_read_file(…)`, `lo_export(…)` | `forbidden_function` |
| Semantic binding | `SELECT amont FROM orderz`; `SELECT customer_id FROM a JOIN b …` (ambiguous); `WHERE alias_from_select > 1` | `unknown_table`, `unknown_column`, `ambiguous_column`, `alias_misuse` |
| Type checks | `WHERE amount = 'expensive'`; `WHERE created_at > 'last tuesday'` | `type_mismatch` |
| Aggregation checks | `SELECT status, SUM(amount) FROM orders`; `WHERE SUM(amount) > 10` | `group_by_violation`, `aggregate_in_where` |
| Domain checks | `WHERE status = 'shiped'` when the catalog allows `shipped` | `unknown_value` |
| Relationship checks | `orders.id = customers.id` when the declared FK is `orders.customer_id = customers.id` | `invalid_join_path` |
| Column policy | `SELECT ssn …`; `SELECT * …` (drops `ssn`) | `column_denied` |
| Row-level security | missing tenant scope; `WHERE customer_id = <other tenant>` | `missing_tenant_filter`, `tenant_filter_conflict` |
| Cost & partitions | 20 GiB scan over a 1 GiB budget; Athena query with no partition filter | `scan_budget_exceeded`, `missing_partition_filter` |
| Complexity budgets | generated query with 40 joins, excessive nesting, CTEs, UNION branches, or AST nodes | `complexity_exceeded` |

## Row-level security done correctly

RLS is the highest-stakes feature, so placement is join-aware and applied to
every scope:

- **FROM / INNER / comma join** → conjunct added to that scope's `WHERE`.
- **LEFT join** → conjunct added to the join's `ON` (a `WHERE` predicate on the
  nullable side would silently turn a `LEFT JOIN` into an `INNER JOIN`).
- **RIGHT / FULL / `USING` join** → the table reference is wrapped in a filtered
  subquery, the only always-correct placement.
- **Every CTE and subquery** that references a governed table is filtered too —
  a self-join filters *both* instances.

```python
guard.validate(
    "SELECT c.id, o.id FROM customers c LEFT JOIN orders o ON o.customer_id = c.id",
    params={"customer_id": 7},
).sql
# ... LEFT JOIN orders AS o ON o.customer_id = c.id AND o.customer_id = 7
```

Strategies: `predicate` (default, inject inline), `subquery` (always wrap), or
`require` (inject nothing — *verify* the caller already wrote the filter).
Set `rls_parameterize=True` to emit driver placeholders (`%(customer_id)s`)
instead of literals so your existing parameter-binding path stays intact.

## Live schema reflection

Skip hand-writing the catalog:

```python
# Postgres / any SQLAlchemy dialect (pulls pg_class row/byte estimates)
guard = SQLGuard.from_database(
    "postgresql://user:pass@host/db", schemas=["public"],
    rls=[RLSRule(table="orders", column="customer_id")],
    max_bytes_scanned=16 << 30,
)

# AWS Glue Data Catalog (partition keys + crawler statistics)
from sqlguard.athena import catalog_from_glue
guard = SQLGuard(catalog_from_glue("analytics_db"), policy, dialect="athena")
```

For Postgres you can additionally gate on the planner's own estimate with
`EXPLAIN` (no rows read) — see `sqlguard.postgres.PostgresExplainEstimator` and
`examples/postgres_example.py`.

## Design guarantees

- **Fail closed.** If `result.valid` is `False`, `result.sql` is `None`. Even an
  internal error becomes a blocking violation, never a pass.
- **Stateless & thread-safe.** Build one `SQLGuard` per `(catalog, policy,
  dialect)` and share it across threads/requests.
- **Deterministic.** Same input → same violations and same feedback string.
- **Dialect-aware.** `postgres` and `athena` (Trino) are covered by the test
  suite; other sqlglot dialects parse and validate too.
- **JSON-serializable output.** `result.to_dict()` crosses service boundaries.

## Scope & limitations

v1 is intentionally **read-only** (`SELECT` only). The heuristic cost estimator
is for *budgeting*, not billing — it approximates from catalog statistics; pair
it with `PostgresExplainEstimator` when you want the real planner in the loop.
Semantic checks are as good as the catalog you give them: reflect it live, or
keep it in sync. It reduces risk dramatically; it is not a substitute for
least-privilege database credentials — keep those too.

## Development

```bash
pip install -e '.[dev,all]'
pytest          # 220+ tests, with a 90% coverage gate in CI
ruff check .
mypy            # strict typing is blocking in CI
```

Where this is headed: [docs/ROADMAP.md](docs/ROADMAP.md) (phased enhancement
plan). How v1 was built and scored: [docs/PLAN_AND_REVIEW.md](docs/PLAN_AND_REVIEW.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
