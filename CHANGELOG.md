# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com);
versioning: [SemVer](https://semver.org). Violation `Code` values and
`to_dict()` keys are frozen once released — new failure classes get new codes.

## [Unreleased]

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

## [0.1.0] - 2026-07-04

### Added
- Initial release: statement gate (writes/DDL/commands/locking blocked, writable CTEs caught), catalog-aware semantic validation (name binding, scopes, did-you-mean, type checks), column policy (deny / exclude-from-star, tag-based), join-aware row-level security injection with tenant-spoof detection, `SELECT *` expansion and LIMIT enforcement, heuristic scan-cost budgets with Athena partition-filter enforcement, Postgres `EXPLAIN` cost gate, SQLAlchemy and AWS Glue catalog reflection, structured violations with LLM-ready `feedback()`, `policy_prompt()`, examples, and a 164-test suite.
