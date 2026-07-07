# Phase 3 — Cost intelligence & engine coverage

> Roadmap: [Phase 3](../ROADMAP.md#phase-3--cost-intelligence--engine-coverage) · Target: **v0.4–v0.5** · Total effort: ~25 days (dialect packs dominate)
>
> Goal: replace config-knob heuristics with real statistics, put a dollar sign
> on Athena estimates, and widen the engine matrix without forking the core.

## Sequencing

```
3.5 dbt importer (3 d, ships in v0.2 per release train — zero deps on others)
3.1 partition stats ──▶ (Athena estimates become data-driven)
3.4 dialect packs: snowflake ▶ bigquery ▶ redshift ▶ databricks (4 d each)
3.2 pg_stats selectivity (experimental flag) · 3.3 Trino EXPLAIN · 3.6 freshness
```

---

## 3.1 Partition-statistics selectivity (P0, 4 d)

**Problem.** v1 applies a flat `partition_selectivity=0.1` whenever *any*
partition filter exists. `dt = '2026-07-01'` over 3 years of daily partitions
is ~0.001, not 0.1 — the knob can be off by 100×.

**Design.**
- Catalog: `Table.partitions: PartitionStats | None` where
  `PartitionStats(values: dict[tuple[str, ...], PartitionEntry], keys: tuple[str, ...])`
  and `PartitionEntry(bytes: int | None, rows: int | None)` — one entry per
  partition value-tuple, e.g. `("2026-07-01",) -> (42 GiB, 9e8 rows)`.
  Serialized in `to_dict`/`from_dict` (size-capped: reflectors keep ≤10k
  partitions, else aggregate to per-key counts + totals).
- `athena.catalog_from_glue(..., include_partitions=True)` paginates
  `get_partitions` (Values + Parameters totalSize/numRows). One extra API
  call per table; opt-in flag because large tables have huge partition lists.
- Estimator upgrade (`cost.HeuristicCostEstimator`): for a partitioned table
  occurrence, extract the partition-column constraints from the scope's
  predicate pool (the pool logic exists in `check_partition_filters`; factor
  it to return the *constraint expressions*, not just a boolean):
  - `EQ` literal → exact entry lookup;
  - `IN` literals → sum of entries;
  - range ops (`>=/<=/BETWEEN`) on ISO-date-shaped strings → lexicographic
    range over sorted partition values (correct for zero-padded date/hour
    partition schemes — the overwhelmingly common case; documented);
  - anything unresolvable (function-wrapped column, OR across keys, params)
    → fall back to the existing `partition_selectivity` knob.
  Result: `bytes = Σ matched entry bytes × column_ratio` — no knob involved.
- Dollars: `Policy.usd_per_tb_scanned: float | None` (Athena list price 5.0
  documented, not hard-coded); `CostEstimate.estimated_usd` populated when
  set; the `scan_budget_exceeded` message includes it ("~$183 for this
  query").

**New:** `PartitionStats`, `Table.partitions`,
`Policy.usd_per_tb_scanned`, `CostEstimate.estimated_usd`.

**Tests:** EQ/IN/range each hit exact partition sums; unresolvable predicate
falls back to knob; range over non-date strings falls back; USD in message;
10k-partition cap aggregates instead of exploding memory.

## 3.2 Postgres statistics selectivity (P2, 5 d, experimental)

**Design.** Opt-in via `catalog_from_sqlalchemy(..., include_column_stats=True)`:
one query over `pg_stats` per schema pulling `n_distinct`, `null_frac`, and
`histogram_bounds` (as text) into new `Column.stats: ColumnStats | None`.
Estimator (behind `Policy.experimental_selectivity=False`):
- `col = literal` → `rows × max(1/ndistinct, null-adjusted floor)`
  (negative `n_distinct` = ratio semantics handled);
- range predicate → fraction of histogram buckets covered;
- conjuncts multiply (independence assumption, floor at 1 row); OR → sum
  capped at 1.0; anything else → selectivity 1 (conservative).
- Feeds `rows_scanned`/`bytes_scanned` for row-store tables and, later, join
  fan-out (with 1.1's FK uniqueness: FK→PK join ≤ left cardinality).
Accuracy target (acceptance): ≤10× error on a seeded TPC-H-lite dataset in
the 0.2 integration job — planner-grade is explicitly *not* the goal; the
EXPLAIN estimator remains the precise option.

**New:** `Column.stats`, `Policy.experimental_selectivity`.

## 3.3 Trino/Athena EXPLAIN gate (P2, 3 d)

**Design.** `sqlguard.athena.AthenaExplainEstimator(client=None,
workgroup=None, output_location=None, max_estimated_rows=None,
timeout_s=15)` implementing the `CostEstimator` protocol like the Postgres
one: runs `EXPLAIN (TYPE IO, FORMAT JSON) <sql>` via
`start_query_execution` + result polling (EXPLAIN itself scans no table
data), parses the IO plan's table handles + estimate blocks when present.
Degrades to `cost_estimation_failed` WARNING on timeout/permission issues —
same contract as `PostgresExplainEstimator`. Registered after the heuristic;
never sees invalid SQL (guard already guarantees this for secondary
estimators). Tested with a fake Athena client (canned EXPLAIN JSON captured
from a real run and committed as a fixture).

## 3.4 Dialect packs: Snowflake, BigQuery, Redshift, Databricks (P1, 4 d each)

**Design.** One mechanism, four data files. New `src/sqlguard/dialects/`:
```python
@dataclass(frozen=True)
class DialectPack:
    name: str
    function_denylist: frozenset[str]
    columnar_default: bool
    require_partition_filter_default: bool
    pseudo_columns: dict[str, str]        # e.g. {"_partitiontime": "timestamp"}
    quirks: frozenset[str]                # feature flags checks may consult
REGISTRY: dict[str, DialectPack]
```
- `Policy.effective_function_denylist` / `effective_require_partition_filter`
  and the estimator's columnar default consult `REGISTRY` (postgres/athena
  entries recreate today's behavior exactly — pure refactor first, packs
  after).
- **BigQuery** specifics: `_PARTITIONTIME`/`_PARTITIONDATE` pseudo-columns
  injected as virtual catalog columns on tables flagged
  `partition_columns=("_partitiontime",)`; `require_partition_filter` default
  on; bytes-based billing → `usd_per_tb_scanned` docs (on-demand pricing).
- **Snowflake**: no user-visible partitions (micro-partitions) →
  partition enforcement default off; cost budgeting leans on row budgets +
  EXPLAIN-style estimator deferred; function denylist seeds
  (`system$…` admin functions, `get_ddl`).
- **Redshift**: postgres-derived denylist minus absent functions plus
  `unload`-adjacent blocking (already a Command); columnar default on;
  dist/sort keys captured as plain metadata for later.
- **Databricks/Spark**: hive-style partitions reuse the Athena path wholesale.
- Per dialect: `tests/test_<dialect>.py` cloned from `test_athena.py`'s
  structure (same scenario matrix: binding, RLS placement, partitions where
  applicable, writes blocked, struct/pseudo-column handling), plus a
  README support-matrix row. **A dialect isn't "supported" until its test
  file exists** — that's the definition, enforced by the matrix doc.

## 3.5 dbt manifest importer (P0, 3 d — ships early, in v0.2)

**Why P0.** dbt projects already maintain everything the guard needs —
schemas, docs, PII tags, relationships, accepted values, freshness. This is
the zero-config adoption path.

**Design.** `sqlguard/dbt.py` → `Catalog.from_dbt(manifest_path,
catalog_path=None, select_schemas=None)`:
- `manifest.json` nodes with `resource_type == "model"` (+ `source`
  definitions) → `Table(name, schema=node.schema)`;
- columns: types from `catalog.json` when provided (compiled truth), else
  manifest column docs (types optional → "unknown");
- tags: column `meta.tags` ∪ `tags`, model tags → `Table.tags` (so
  `ColumnRule(tags={"pii"})` lights up from dbt metadata unchanged);
- tests → metadata: `relationships` test ⇒ `ForeignKey` (1.1),
  `accepted_values` ⇒ `Column.allowed_values` (1.5), `unique`+`not_null`
  pairs on one column ⇒ `Table.primary_key` heuristic;
- stats: `catalog.json` `stats.num_rows`/`stats.bytes` when the adapter
  provides them (Snowflake/BigQuery do) → `row_count`/`total_bytes`.
- Pure-stdlib JSON parsing, no dbt dependency; tested against a committed
  fixture manifest (small 3-model project, generated once with dbt-duckdb
  and checked in).

**Acceptance.** A dbt project gets semantic validation + PII rules + FK-aware
joins from artifacts it already ships, in one constructor call.

## 3.6 Catalog freshness contract (P2, 2 d)

**Design.** `Catalog.snapshot_at: datetime | None` (reflectors stamp it,
serialized in `to_dict`); `Policy.max_catalog_age: timedelta | None`; when
exceeded, each `validate()` emits `Code.CATALOG_STALE` — WARNING by default,
`Policy.on_stale_catalog="error"` for fail-closed shops. `Catalog.age()`
helper; docs pattern for a background-refresh loop (build a new guard, swap
atomically — guards are immutable, so swap is a pointer assignment).

**New:** `Code.CATALOG_STALE`, `Catalog.snapshot_at`,
`Policy.max_catalog_age`, `Policy.on_stale_catalog`.

---

## Definition of done (phase)

- [ ] Athena estimates use real partition bytes when available; knob is the
      fallback, not the default
- [ ] `Catalog.from_dbt` round-trips the fixture project into working
      PII/FK/enum enforcement
- [ ] Support matrix: postgres, athena, snowflake, bigquery, redshift,
      databricks — each row backed by its own test file
- [ ] Dollar estimates appear in budget-violation messages when priced
- [ ] Bench gate green (packs are data lookups, not new passes)
