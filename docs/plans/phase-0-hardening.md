# Phase 0 — Hardening the v1 core

> Roadmap: [Phase 0](../ROADMAP.md#phase-0--hardening-the-v1-core-close-known-gaps) · Target: **v0.2** · Total effort: ~15 days
>
> Goal: nothing in the package is untested, unpublished, or unmeasured. This
> phase has no new user-facing features by design — it converts the v1 review's
> ⚠️ items into ✅ before feature work widens the surface.

## Implementation status (2026-09-12)

The repository now enforces the automated Phase 0 gates: 221 tests, 90% total
coverage, adapter coverage above 90%, strict mypy, Ruff, live PostgreSQL,
minimum/latest-compatible sqlglot checks, calibrated latency checks, final-SQL
audit, idempotence properties, and a permanent minimized failure corpus.

The remaining exit criteria are operational rather than code changes:

- accumulate 14 consecutive green scheduled fuzz runs;
- configure PyPI trusted publishing and publish v0.2.0;
- protect `main` and make the CI/fuzz checks required in repository settings.

Parser upgrades are security changes. The supported range is intentionally
bounded to `sqlglot>=30.18,<31`; widening it requires both compatibility lanes
and the extended fuzz profile to pass.

## Sequencing

```
0.1 adapter tests ──┐
0.3 fuzzing ────────┼──▶ 0.4 scope index ──▶ 0.5 bench gate
0.2 integration ────┘                            │
0.6 mypy strict (parallel, any time)             ▼
0.7 release automation (last: ships v0.2)
```
0.4 must land *after* 0.1–0.3 so the refactor is caught by maximum coverage.

---

## 0.1 Adapter unit tests (P0, 2 d)

**Problem.** `reflect.py`, `athena.py`, `postgres.py` (348 LOC combined) have
zero coverage — the weakest link in the v1 scorecard (6.5/10).

**Design.** New `tests/test_adapters.py` with hand-rolled fakes (no moto, no
network, no new deps):

- `FakeGlueClient`: `get_paginator("get_tables")` returns a paginator whose
  `paginate(DatabaseName=…)` yields 2 pages of `TableList` entries. Cases:
  - partition keys are appended to `columns` *and* land in `partition_columns`;
  - `Parameters` stats parsing: `numRows`/`recordCount` fallback order,
    `totalSize`/`rawDataSize` fallback, non-numeric values ignored;
  - `InputFormat` containing `parquet`/`orc` ⇒ `columnar=True`, text ⇒ `None`;
  - `catalog_id` passed through; `include_stats=False` yields no stats.
- `FakeEngine` / `FakeConnection` for `PostgresExplainEstimator`:
  `connect()` context manager whose `exec_driver_sql()` records the statement
  and returns a row `[ [{"Plan": {"Total Cost": …, "Plan Rows": …}}] ]`. Cases:
  - budgets: over-cost ⇒ `scan_budget_exceeded` ERROR; under ⇒ estimate only;
  - string JSON payload (some drivers) is parsed;
  - thrown exception ⇒ `cost_estimation_failed` WARNING, estimate `None`;
  - constructor rejects both/neither of `engine`/`connection_factory`;
  - the statement sent is exactly `EXPLAIN (FORMAT JSON) <sql>`;
  - DBAPI path: fake `connection_factory` with cursor lifecycle asserted closed.
- `reflect.py` against **SQLite in-memory** (SQLAlchemy is already a dev dep):
  create tables + a view, reflect, assert names/types/nullability/view
  inclusion, `default_schema` behavior; `_pg_stats` failure path via a fake
  engine whose `exec_driver_sql` raises ⇒ warning logged, catalog still built.
- Wire the fakes into a guard end-to-end: `SQLGuard(catalog_from_glue(...))`
  validates an Athena query with partition enforcement — proving reflected
  catalogs drive the real pipeline.

**Acceptance.** Adapters ≥90% line coverage (`pytest --cov=sqlguard`); CI green
with no cloud credentials.

## 0.2 Live Postgres integration job (P0, 2 d)

**Design.**
- `tests/integration/test_postgres_live.py`, marked
  `@pytest.mark.integration`, auto-skipped unless `SQLGUARD_PG_URL` is set
  (`pyproject`: register the marker; default `addopts` unchanged).
- Fixture seeds a schema: `orders` (100k rows via `generate_series`),
  `customers` (10k), a view, a PII column — then `ANALYZE`.
- Tests: `SQLGuard.from_database()` reflects tables *and* pg_class stats
  (row_count within 2× of truth); EXPLAIN estimator blocks
  `SELECT * FROM orders o1 CROSS JOIN orders o2` on `max_total_cost`; the
  full pipeline result executes successfully via the same engine and returns
  ≤ LIMIT rows; RLS-filtered query returns only the seeded tenant's rows
  (**the** end-to-end security assertion).
- CI: new `integration` job with `services: postgres: image: postgres:16`,
  health-checked; runs only the marker.

**Acceptance.** A real query is blocked by planner cost in CI; RLS row-level
assertion passes against real data.

## 0.3 Fuzz / property tests (P0, 3 d)

**Design.** `tests/test_fuzz.py` using Hypothesis (add to `[dev]` extra):

- **Strategy A — grammatical queries** over the fixture catalog: composite
  strategy assembling SELECTs (random projections incl. `*`, 0–3 joins with
  random kinds, optional CTE, WHERE trees from a predicate strategy, GROUP
  BY/ORDER BY/LIMIT). Mostly valid by construction.
- **Strategy B — mutations**: take A's output text and apply typo operators
  (identifier corruption, keyword swap, paren/quote deletion, `;` injection,
  random unicode). Mostly invalid by construction.
- **Strategy C — adversarial text**: `st.text()` plus a seeded corpus of SQLi
  classics (`' OR 1=1 --`, stacked statements, comment tricks).

**Invariants (each is one property test):**
1. `guard.validate()` never raises, for any input, with RLS+column
   rules+budgets enabled.
2. `result.valid is False` ⇒ `result.sql is None`.
3. `result.valid is True` ⇒ `sqlglot.parse_one(result.sql, dialect)` succeeds
   and the tree contains no write/DDL/Command node (re-run `statement_gate`
   on the *output* — a self-audit).
4. Idempotency: re-validating `result.sql` is valid and byte-identical
   (`validate(result.sql).sql == result.sql`).
5. RLS presence: if a governed table survives to output, the tenant predicate
   appears in every scope that references it (reuse the scope walker to
   assert, not string matching).
- CI: 200 examples per property on PRs (fast); nightly workflow
  (`fuzz.yml`, cron) with `--hypothesis-seed=random`, 10k examples, failures
  minimized and committed under `tests/regressions/` as plain pytest cases.

**Acceptance.** 10k nightly cases pass; every found crash becomes a permanent
regression test.

## 0.4 Single-pass scope index (P1, 4 d)

**Problem.** The pipeline rebuilds scope maps five times (`bind_names`,
`apply_column_rules`, `apply_rls`, `check_joins`,
`check_partition_filters`/estimator/`collect_referenced_columns`) — measured
at ~55% of E2E latency on the medium benchmark query.

**Key insight.** After qualification, the *source topology* (alias →
table/derived mapping per scope) only changes on RLS **subquery wraps**.
Predicate appends (WHERE/ON), LIMIT changes, and select-item drops never
change `selected_sources`. So one index can serve every post-qualify stage.

**Design.** New `src/sqlguard/scopeindex.py`:
```python
class ScopeIndex:
    @classmethod
    def build(cls, tree, index: CatalogIndex) -> "ScopeIndex": ...
    @property
    def infos(self) -> list[ScopeInfo]: ...          # today's ScopeInfo, reused
    def by_expression(self) -> dict[int, ScopeInfo]: ...
    def invalidate(self) -> None: ...                # wholesale; rebuilt lazily
```
- `guard._validate` builds it once after qualification and passes it to
  `apply_column_rules`, `apply_rls`, `check_joins`, `check_partition_filters`,
  the estimator, and `collect_referenced_columns` (each gains an optional
  `scope_index=` parameter defaulting to self-build, keeping their public
  signatures usable standalone).
- `rls._wrap_in_subquery` calls `scope_index.invalidate()`; wraps are rare
  (RIGHT/FULL/USING joins or explicit subquery strategy), so the common path
  is exactly **2 traversals** (raw-tree bind + qualified-tree index) instead
  of 6.
- `bind_names` keeps its own raw-tree build (qualify copies the tree; node
  identities differ) — that build is cheap (244 µs) and stays.

**Risks.** Stale-index bugs. Mitigations: 0.3's invariants run against the
refactor; a `SQLGUARD_PARANOID=1` env flag makes `ScopeIndex` rebuild and
structurally compare on every access in CI (debug assertion mode).

**Acceptance.** Benchmark medium query ≤3.5 ms E2E (from 6.0 ms) with zero
test modifications; fuzz suite green.

**Outcome (implemented).** Shipped with all tests green in normal and
`SQLGUARD_PARANOID` modes. Measured: invalid-path −37% (1.04→0.66 ms),
throughput +12.5% (480→540/s), medium query −6% (6.03→5.69 ms). The ≤3.5 ms
target was not reached because profiling shows the residual cost is sqlglot's
`qualify()`/`annotate_types()` and parse — not scope traversal. Further E2E
gains need caching at those layers; tracked as follow-up, not blocking v0.2.

## 0.5 Benchmark regression gate (P1, 1 d)

**Design.**
- Move the session bench script into `benchmarks/bench_pipeline.py`; JSON
  output `{scenario: {median_us, p95_us}}`; calibrate against a fixed
  CPU-bound loop and report *ratios*, not wall time, to absorb runner
  variance on shared CI hardware.
- `benchmarks/baseline.json` committed; CI job fails if any scenario ratio
  regresses >25%; `python -m benchmarks.bench_pipeline --update-baseline`
  refreshes it deliberately (reviewed like any diff).

**Acceptance.** A PR that doubles `validate()` latency fails CI; the 0.4 PR
updates the baseline *downward*.

## 0.6 mypy `--strict` gate (P1, 2 d)

**Design.** Remove `|| true` from the CI mypy step. Known debt to fix:
`rls._convert_value`'s `list`-as-marker return (introduce a
`_InValues(list)` wrapper or a tagged union), `Any` in adapter boundaries
(type `FakeEngine`-shaped protocols: `_SupportsConnect`), `Code`/`Severity`
enum typing in `to_dict`, `cost.CostEstimator` protocol variance. Budget:
strict on `sqlguard.*`; tests stay non-strict.

**Acceptance.** `mypy --strict src` exits 0 in CI and is a required check.

## 0.7 PyPI release automation (P0, 1 d)

**Design.**
- `.github/workflows/release.yml`: on tag `v*` → build (sdist+wheel), `twine
  check`, publish via **trusted publishing** (OIDC,
  `pypa/gh-action-pypi-publish`, no long-lived token), then create a GitHub
  Release with the matching `CHANGELOG.md` section.
- `CHANGELOG.md` (Keep-a-Changelog format), seeded with v0.1.0.
- `docs/versioning.md`: SemVer; **`Code` enum values and `to_dict()` keys are
  frozen once released**; new failure classes get new codes; deprecations
  live one minor release behind a `DeprecationWarning`.

**Acceptance.** `pip install sql-semantic-guard` works from PyPI;
releasing is `git tag v0.2.0 && git push --tags`.

---

## Definition of done (phase)

- [x] Coverage: overall ≥90%, adapters ≥90%
- [ ] Nightly fuzz green for 7 consecutive days
- [ ] Medium-query E2E ≤3.5 ms; bench gate required on PRs
- [x] mypy strict required; ruff required
- [ ] v0.2.0 on PyPI with changelog and frozen-codes policy published
