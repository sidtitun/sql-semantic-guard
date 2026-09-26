# SQL Semantic Guard

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
5. **Rewrites in the safe direction** — expands `SELECT *`, drops or masks
   restricted columns, and adds/clamps `LIMIT`.
6. **Returns a structured, LLM-ready violation report** so the model can
   self-repair.

Everything is composable, dependency-light (just [`sqlglot`](https://github.com/tobymao/sqlglot)),
and fails **closed**: an invalid result never carries executable SQL.

## What this repository is

SQL Semantic Guard is a Python library that sits **between a SQL-generating
LLM and your database**. It parses the proposed SQL, resolves it against a
trusted catalog, applies security and resource policies, and returns either:

- rewritten SQL that is safe to send to a read-only database connection; or
- structured violations that your application can reject or send back to the
  LLM for correction.

It is not a text-to-SQL model, database driver, query executor, or database
proxy. Bring your own LLM and execution layer; use this repository as the
deterministic enforcement boundary between them.

```mermaid
flowchart TD
    Q["User question"] --> L["Text-to-SQL model"]
    L --> G["SQL Semantic Guard"]
    C["Catalog + policy + trusted caller context"] --> G
    G -->|"invalid: structured feedback"| L
    G -->|"valid: rewritten SQL only"| D["Read-only database role"]
    D --> R["Application result"]
```

## The problem it solves

The dangerous failures of text-to-SQL are **semantic, not syntactic**:
hallucinated columns, wrong joins, a missing tenant filter, misuse of a
sensitive field. Catching them needs name binding, scope resolution, type
checks, and catalog awareness — which a plain AST parser doesn't do. Today's
options stop at "does it parse / does it execute," so every serious NL2SQL
deployment re-builds the same guardrails by hand. This is that layer, as a
library.

| Failure in generated SQL | Production risk | Guard response |
|---|---|---|
| Valid syntax, nonexistent table or column | Runtime failure or incorrect answer | Catalog-aware binding rejects it with suggestions |
| `UPDATE`, DDL, writable CTE, locking, or dangerous function | Data loss, state changes, denial of service | Statement and function gates block it |
| Missing or spoofed tenant filter | Cross-tenant data exposure | RLS is verified or injected at every table reference |
| `SELECT *` includes PII | Sensitive-data leakage | Stars expand and restricted columns are denied, removed, or masked |
| Wrong join path or ambiguous reference | Plausible but incorrect answer | Relationship and scope checks report the error |
| Unbounded or partition-free query | High latency and warehouse cost | Limits, complexity budgets, partition rules, and scan budgets stop it |
| Invalid query returned as a plain exception | Weak LLM repair loop | Stable codes and `feedback()` provide actionable correction context |

The guard never executes SQL. Your application must execute only `result.sql`
when `result.valid` is `True`; never fall back to the original model output.

## Install

```bash
pip install sql-semantic-guard                 # core (sqlglot only)
pip install 'sql-semantic-guard[postgres]'     # + SQLAlchemy live reflection
pip install 'sql-semantic-guard[athena]'       # + boto3 / AWS Glue reflection
```

Requires Python 3.9+.

## Implement it

### 1. Build a trusted catalog

The catalog is the ground truth for table, column, type, relationship, domain,
cost, and policy checks. Define it in code or reflect it from the live schema.
Do not construct it from user prompts or model output.

```python
from sqlguard import Catalog

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
```

For production systems, prefer live reflection so schema changes do not make
the guard stale. See [Live schema reflection](#live-schema-reflection).

### 2. Define the enforcement policy

Policies are application-owned configuration. RLS parameter values must come
from authenticated server-side context—not from the prompt or the LLM.

```python
from sqlguard import ColumnRule, Policy, RLSRule

policy = Policy(
    rls=[RLSRule(table="orders", column="customer_id", param="customer_id")],
    column_rules=[ColumnRule(tags={"pii"}, action="deny", reason="PII")],
    default_limit=1000,
    max_limit=10_000,
    max_bytes_scanned=16 << 30,
    max_joins=8,
    max_subquery_depth=4,
    max_ctes=8,
    max_union_branches=6,
    max_expression_nodes=5000,
)
```

### 3. Construct one reusable guard

`SQLGuard` is stateless per validation call and can be shared across requests.

```python
from sqlguard import SQLGuard

guard = SQLGuard(catalog, policy, dialect="postgres")
```

### 4. Validate before every execution

Treat model-generated SQL as untrusted input. Execute only the rewritten SQL
returned when `result.valid` is `True`.

```python
result = guard.validate(
    sql_from_llm,
    params={"customer_id": authenticated_tenant_id},
)

if not result.valid:
    return {
        "message": result.feedback(),
        "validation": result.to_dict(),
    }

rows = execute_with_read_only_role(result.sql)
```

For example, validating `SELECT * FROM orders` can produce:

```sql
SELECT orders.id, orders.customer_id, orders.amount, orders.created_at
FROM orders
WHERE orders.customer_id = 42
LIMIT 1000
```

The rewrite expands `*`, removes `ssn`, injects the tenant filter, and adds a
row limit.

### 5. Add bounded LLM self-repair

Use the structured feedback for one or two correction attempts, then fail
closed. Never execute the original SQL after a guard failure.

```python
messages = [{"role": "system", "content": guard.policy_prompt()}, ...]

for _ in range(2):
    sql_from_llm = llm(messages)
    result = guard.validate(
        sql_from_llm,
        params={"customer_id": authenticated_tenant_id},
    )
    if result.valid:
        break
    messages.extend([
        {"role": "assistant", "content": sql_from_llm},
        {"role": "user", "content": result.feedback()},
    ])

if not result.valid:
    refuse_or_escalate(result)
else:
    rows = execute_with_read_only_role(result.sql)
```

`guard.policy_prompt()` supplies the known schema and active rules to the
model, reducing avoidable failures before the repair loop starts.

### Minimal API-service pattern

Keep validation in the trusted backend, immediately before execution:

```python
def answer_database_question(question: str, authenticated_tenant_id: int):
    generated_sql = text_to_sql_model(question, guard.policy_prompt())
    result = guard.validate(
        generated_sql,
        params={"customer_id": authenticated_tenant_id},
    )
    if not result.valid:
        return {"ok": False, "validation": result.to_dict()}

    rows = execute_with_read_only_role(result.sql)
    return {
        "ok": True,
        "rows": rows,
        "rewrites": [rewrite.to_dict() for rewrite in result.rewrites],
    }
```

A blocked query returns actionable, deterministic feedback:

```python
r = guard.validate(
    "SELECT amont FROM orderz WHERE customer_id = 999",
    params={"customer_id": 42},
)
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

## What each layer catches

| Layer | Example it blocks / fixes | Violation code |
|---|---|---|
| Statement gate | `DELETE FROM orders`; `WITH d AS (DELETE … RETURNING id) SELECT …`; `SELECT … FOR UPDATE`; `SELECT 1; DROP TABLE …` | `disallowed_statement`, `nested_write`, `locking_clause`, `multiple_statements` |
| Function gate | `pg_sleep(10)`, `pg_read_file(…)`, `lo_export(…)` | `forbidden_function` |
| Function signatures | `date_trunc(created_at)`, `split_part(name, '.')` | `function_misuse` |
| Semantic binding | `SELECT amont FROM orderz`; `SELECT customer_id FROM a JOIN b …` (ambiguous); `WHERE alias_from_select > 1` | `unknown_table`, `unknown_column`, `ambiguous_column`, `alias_misuse` |
| Type checks | `WHERE amount = 'expensive'`; `WHERE created_at > 'last tuesday'` | `type_mismatch` |
| Aggregation checks | `SELECT status, SUM(amount) FROM orders`; `WHERE SUM(amount) > 10` | `group_by_violation`, `aggregate_in_where` |
| Domain checks | `WHERE status = 'shiped'` when the catalog allows `shipped` | `unknown_value` |
| Nested/derived binding | `payload.refferer`; `SELECT sub.bogus FROM (SELECT * FROM orders) sub` | `unknown_column` |
| Relationship checks | `orders.id = customers.id` when the declared FK is `orders.customer_id = customers.id` | `invalid_join_path` |
| Column policy | `SELECT ssn …`; `SELECT * …` (drops or masks `ssn`) | `column_denied` |
| Row-level security | missing tenant scope; `WHERE customer_id = <other tenant>` | `missing_tenant_filter`, `tenant_filter_conflict` |
| Cost & partitions | 20 GiB scan over a 1 GiB budget; Athena query with no partition filter | `scan_budget_exceeded`, `missing_partition_filter` |
| Complexity budgets | generated query with 40 joins, excessive nesting, CTEs, UNION branches, or AST nodes | `complexity_exceeded` |

## Validation result contract

`guard.validate()` always returns a `ValidationResult`; SQL problems do not
raise exceptions. The important fields are:

| Field | Meaning |
|---|---|
| `valid` | `True` only when the returned query may be executed |
| `sql` | Rewritten SQL when valid; `None` when blocked |
| `violations` / `errors` | Structured codes, messages, hints, table/column context, and metadata |
| `rewrites` | Applied protections such as RLS, masking, star expansion, and limits |
| `stats` | Referenced tables/columns, complexity counters, checks run, and cost estimate |
| `would_block` | In shadow mode, whether enforcing mode would have rejected the query |

Use `result.to_dict()` for API responses or audit events and
`result.feedback()` for an LLM correction prompt. Use `validate_or_raise()`
only when exception-based application flow is preferred.

## Column masking

Mask sensitive output while retaining its shape with a built-in or a validated
SQL expression:

```python
Policy(column_rules=[
    ColumnRule(tags={"pii"}, action="mask", mask_with="hash"),
    ColumnRule(
        table="customers",
        column="phone",
        action="mask",
        mask_with="LEFT({col}, 4) || '****'",
    ),
])
```

The built-ins are `"redact"` (`'***'`), `"null"` (a typed `NULL`), and
`"hash"` (rendered for PostgreSQL or Athena/Trino). They apply to explicit
projections and expanded stars while preserving output aliases. Custom
expressions must contain `{col}` and are parsed when the policy is created.

Masked columns are blocked in `WHERE`, `JOIN`, `GROUP BY`, and `ORDER BY` by
default because rewriting those references would change query semantics. Set
`allow_predicates=True` on the mask rule when callers may filter on the raw
value while the returned value remains masked. When rules overlap, precedence
is deterministic: `deny` beats `mask`, which beats `exclude_from_star`.

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

For compound tenancy, soft-delete, region, or effective-date rules, use a
validated predicate template instead of a single equality:

```python
Policy(rls=[
    RLSRule(
        table="orders",
        predicate="region IN :regions AND deleted_at IS NULL",
    )
])

guard.validate(sql, params={"regions": ["APAC", "EMEA"]})
```

Predicate columns are unqualified in the template and are bound to each table
alias when injected. Named parameters are converted to SQL literals; list and
tuple values expand inside `IN`. Templates, dialect syntax, and referenced
catalog columns are validated when the rule/guard is constructed. Missing or
empty parameters fail closed. Expression rules support the `predicate` and
`subquery` strategies; `require` and conflict diagnostics remain available for
the simpler equality rules.

## Role-specific policies

Use one guard when roles share a catalog but need different restrictions. A
role overlay adds rules and can only lower resource limits or enable stricter
checks. It cannot remove a base rule or raise a base limit.

```python
from sqlguard import ColumnRule, Policy, PolicyOverlay, PolicySet, SQLGuard

policies = PolicySet(
    base=Policy(default_limit=1_000),
    roles={
        "analyst": PolicyOverlay(
            add_column_rules=[
                ColumnRule(tags={"pii"}, action="mask", mask_with="hash"),
            ]
        ),
        "support": PolicyOverlay(
            add_column_rules=[ColumnRule(tags={"pii"}, action="deny")],
            max_bytes_scanned=1 << 30,
        ),
    },
)
guard = SQLGuard(catalog, policies, dialect="postgres")

result = guard.validate(sql_from_llm, params=trusted_context, role="support")
```

Roles are checked when the guard starts. An unknown role raises `PolicyError`;
the selected role is included in `result.stats` and `result.to_dict()`.

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

## Production checklist

- Run queries with a **read-only, least-privilege database role**. The guard is
  defense in depth, not a replacement for database permissions.
- Build RLS parameters from authenticated server context. Never let the model
  choose tenant IDs, regions, roles, or other authorization values.
- Reflect or refresh the catalog as part of deployment so validation matches
  the database schema.
- Validate every generated and regenerated query. Execute only `result.sql`.
- Configure default/max row limits, complexity ceilings, partition rules, and
  scan budgets appropriate for the application.
- Keep LLM repair attempts bounded; one or two retries is usually enough.
- Add database statement timeouts and warehouse-native quotas as a second
  resource-control layer.
- Log `result.to_dict()` for observability, but apply your normal controls to
  SQL text and literals because they may contain sensitive data.
- Use `Policy(enforcement="log_only")` to measure would-block behavior before
  enforcing a new policy, then switch to the default `"block"` mode.
- Test representative joins, CTEs, tenant rules, masks, and failure cases
  against a non-production database before rollout.

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
pytest          # 360+ tests, with a 90% coverage gate in CI
ruff check .
mypy            # strict typing is blocking in CI
python -m evals.run  # frozen safety/rewriting corpus and release thresholds
```

Where this is headed: [docs/ROADMAP.md](docs/ROADMAP.md) (phased enhancement
plan). How v1 was built and scored: [docs/PLAN_AND_REVIEW.md](docs/PLAN_AND_REVIEW.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
