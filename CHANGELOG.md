# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com);
versioning: [SemVer](https://semver.org). Violation `Code` values and
`to_dict()` keys are frozen once released — new failure classes get new codes.

## [Unreleased]

## [0.2.0] - 2026-09-12

### Added
- Adapter unit tests (Glue, EXPLAIN estimator, SQLAlchemy reflection) — 95% adapter coverage.
- Hypothesis property tests: never-crash, fail-closed, output self-audit, idempotency, RLS-everywhere invariants; nightly 10k-example fuzz workflow.
- Shared `ScopeIndex` across pipeline stages (`SQLGUARD_PARANOID=1` disables caching for debugging); invalid-path latency −37%, throughput +12.5%.
- Calibrated benchmark regression gate (`benchmarks/bench_pipeline.py`) enforced in CI.
- Shadow mode: `Policy(enforcement="log_only")` + `ValidationResult.would_block` for measured rollouts; hard-stop classes still block.
- Live Postgres integration suite (`SQLGUARD_PG_URL`-gated) run in CI against a service container, including the end-to-end RLS row-level assertion.
- Release automation: PyPI trusted publishing on `v*` tags.
- Query-complexity budgets for joins, nested subqueries, CTEs, UNION branches,
  and total expression nodes, enforced before semantic analysis.
- Foreign-key and primary-key catalog metadata, SQLAlchemy relationship
  reflection, and FK-aware validation for direct, composite, and CTE-lineage joins.
- Aggregation validation for GROUP BY correctness, aggregate placement,
  HAVING/ORDER BY, ordinals, aliases, window functions, and PostgreSQL
  primary-key functional dependency.
- Catalog enum/domain validation with suggestions, bounded allowed-value
  profiling, and explicit case-insensitive matching.

### Changed
- Strict mypy, 90% coverage, minimum/latest-compatible sqlglot, packaging,
  PostgreSQL integration, and calibrated benchmarks are blocking CI gates.
- Supported parser range is pinned to `sqlglot>=30.18,<31`; widening it is a
  security-sensitive compatibility change.

## [0.1.0] - 2026-07-04

### Added
- Initial release: statement gate (writes/DDL/commands/locking blocked, writable CTEs caught), catalog-aware semantic validation (name binding, scopes, did-you-mean, type checks), column policy (deny / exclude-from-star, tag-based), join-aware row-level security injection with tenant-spoof detection, `SELECT *` expansion and LIMIT enforcement, heuristic scan-cost budgets with Athena partition-filter enforcement, Postgres `EXPLAIN` cost gate, SQLAlchemy and AWS Glue catalog reflection, structured violations with LLM-ready `feedback()`, `policy_prompt()`, examples, and a 164-test suite.
