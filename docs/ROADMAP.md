# Enhancement Roadmap — beyond v0.1.0

**Status:** proposed · **Baseline:** v0.1.0 on `claude/sweet-gauss-5abdlh` (164 tests, pipeline scorecard in [PLAN_AND_REVIEW.md](PLAN_AND_REVIEW.md))

v1 proved the pipeline: statement gate → semantic binding → column policy → RLS
→ rewrites → cost. This plan turns it from a strong library into the *default*
guardrail layer for text-to-SQL. Items are grouped into five phases, ordered by
(risk reduction × user demand) ÷ effort. Each item has a priority, a rough
effort estimate, and an acceptance criterion so "done" is testable.

Priorities: **P0** = blocks trust/adoption · **P1** = major differentiator ·
**P2** = valuable, not urgent · **P3** = exploratory.

Every phase has a detailed implementation plan (designs, APIs, files touched,
test plans, risks) under [plans/](plans/README.md).

---

## Phase 0 — Hardening the v1 core (close known gaps)

*Goal: nothing in the package is untested, unpublished, or unmeasured.*
*Detailed plan: [plans/phase-0-hardening.md](plans/phase-0-hardening.md)*

| # | Item | Prio | Effort | Acceptance criterion |
|---|---|:---:|:---:|---|
| 0.1 | **Adapter unit tests** — fake Glue client for `athena.py`, fake engine/DBAPI for `PostgresExplainEstimator`, SQLite-backed test for `reflect.py` | P0 | 2 d | Adapters reach ≥90% line coverage; CI green without any live cloud/DB |
| 0.2 | **Live integration job** — dockerized Postgres service in CI exercising `from_database()`, pg_class stats, and EXPLAIN gating end-to-end | P0 | 2 d | A real query is blocked by planner cost in CI |
| 0.3 | **Fuzz / property tests** — Hypothesis strategies + a corpus of generated SQL; invariant: *never crash, never return `sql` when invalid, idempotent re-validation* | P0 | 3 d | 10k fuzz cases pass in CI nightly; any crash becomes a regression test |
| 0.4 | **Single-pass scope index** — build the scope map once, update incrementally through mutations instead of 5 re-traversals | P1 | 4 d | Benchmark suite shows ≥40% E2E latency cut (6 ms → ≤3.5 ms on the medium query) with zero test changes |
| 0.5 | **Benchmark regression gate** — commit the bench script, run in CI, fail on >25% latency regression | P1 | 1 d | PR that doubles validate() latency fails CI |
| 0.6 | **mypy `--strict` as a hard gate** | P1 | 2 d | CI fails on type errors; `Any` count tracked |
| 0.7 | **PyPI release automation** — trusted publishing via GitHub Actions on tag, CHANGELOG, SemVer policy, stability guarantee for `Code` enum values | P0 | 1 d | `pip install sql-semantic-guard` works from PyPI; releases are one `git tag` away |

## Phase 1 — Deeper semantic validation (the differentiator)

*Goal: catch the remaining "wrong answer" failure classes, not just "broken query" ones.*
*Detailed plan: [plans/phase-1-semantics.md](plans/phase-1-semantics.md)*

| # | Item | Prio | Effort | Acceptance criterion |
|---|---|:---:|:---:|---|
| 1.1 | **FK-aware join validation** — catalog gains `relationships` (FK graph, importable or declared); flag joins on non-related columns and *suggest the declared join path* (`orders.customer_id → customers.id`) | P0 | 5 d | The classic wrong-join (`ON o.id = c.id`) yields `invalid_join_path` with the correct key pair in the hint |
| 1.2 | **Aggregation correctness** — non-aggregated column outside GROUP BY, aggregates in WHERE, HAVING without grouping context | P1 | 3 d | `SELECT status, sum(amount) FROM orders` (no GROUP BY) is caught pre-execution with a repair hint |
| 1.3 | **Nested-type modeling** — feed struct/array member types into `MappingSchema` so `payload.referrer` members are *validated*, not just tolerated; catch `payload.refferer` typos | P1 | 4 d | Unknown struct field → `unknown_column` with did-you-mean on Athena |
| 1.4 | **Function signature checks** — per-dialect arg-count/type table for the ~100 most-used builtins (`date_trunc`, `split_part`, `coalesce`, …) | P2 | 4 d | `date_trunc(created_at)` (missing unit) caught with the correct signature in the hint |
| 1.5 | **Enum/domain validation** — columns may declare `allowed_values` in the catalog; literals compared against them | P1 | 2 d | `WHERE status = 'shiped'` → `unknown_value`, "did you mean 'shipped'?" |
| 1.6 | **Derived-star resolution** — expand `SELECT *` inside derived tables/CTEs during binding so those sources stop being opaque (removes v1's main fail-open path) | P2 | 3 d | `SELECT sub.bogus FROM (SELECT * FROM orders) sub` is caught |

## Phase 2 — Security depth (enterprise asks)

*Goal: cover the full 7-layer enterprise guardrail matrix, not just tenancy.*
*Detailed plan: [plans/phase-2-security.md](plans/phase-2-security.md)*

| # | Item | Prio | Effort | Acceptance criterion |
|---|---|:---:|:---:|---|
| 2.1 | **Column masking** — `ColumnRule(action="mask", mask_with="'***'"/hash/partial)`, type-preserving, applied in star expansion *and* explicit selects | P0 | 4 d | `SELECT email FROM customers` returns `mask(email)` per policy instead of erroring, when policy says mask |
| 2.2 | **Expression RLS rules** — predicate templates beyond equality: `RLSRule(predicate="region IN :regions AND deleted_at IS NULL")`, parsed+validated at construction | P1 | 4 d | Multi-condition tenancy expressed in one rule; injection still per-scope and join-aware |
| 2.3 | **Role-based policy sets** — named roles mapping to (column rules, RLS, budgets); `guard.validate(sql, role="analyst", params=…)`; policy composition/inheritance | P1 | 4 d | Same guard object serves `analyst` and `support` with different visibility, tested |
| 2.4 | **Complexity budgets** — max joins, max subquery/CTE depth, max UNION branches, expression-node ceiling (planner-abuse and prompt-injection blast-radius control) | P1 | 2 d | A 40-join generated monster is rejected with `complexity_exceeded` |
| 2.5 | **Audit & observability hooks** — structured audit record per validation (who/what/verdict/violations), pluggable sink, OpenTelemetry spans + counters by violation code | P1 | 3 d | Compliance can reconstruct every allow/deny decision from the audit stream |
| 2.6 | **Driver paramstyle matrix** — placeholder rendering for `qmark`/`numeric`/`named`/`pyformat` chosen per policy, documented per driver (psycopg, pyathena, etc.) | P2 | 2 d | Parameterized mode works out-of-the-box with the 4 major paramstyles |
| 2.7 | **Shadow mode** — `enforcement="log_only"` policy switch for safe production rollout: everything evaluated and audited, nothing blocked | P1 | 1 d | A deployment can measure would-be-block rate before enforcing |

## Phase 3 — Cost intelligence & engine coverage

*Goal: replace config-knob heuristics with data, and widen the engine matrix.*
*Detailed plan: [plans/phase-3-cost-and-engines.md](plans/phase-3-cost-and-engines.md)*

| # | Item | Prio | Effort | Acceptance criterion |
|---|---|:---:|:---:|---|
| 3.1 | **Partition-stats selectivity** — pull per-partition sizes/values from Glue `get_partitions`; compute real pruning from date-range predicates instead of the fixed `partition_selectivity` knob | P0 | 4 d | `WHERE dt = '2026-07-01'` estimate uses that partition's actual bytes; dollar estimate (`$/TB`) surfaced in stats |
| 3.2 | **Postgres statistics selectivity** — `pg_stats` histograms/ndistinct for predicate selectivity; flag guaranteed seq-scans on large tables (no usable index for the predicate) | P2 | 5 d | Estimate error ≤10× on the TPC-H-lite test set (vs unbounded today) |
| 3.3 | **Trino/Athena EXPLAIN gate** — parse Athena `EXPLAIN (FORMAT JSON)` for a second opinion when a live connection is available | P2 | 3 d | Optional estimator mirrors `PostgresExplainEstimator` for Athena |
| 3.4 | **Dialect packs: Snowflake, BigQuery, Redshift, Databricks** — per-dialect defaults (function denylist, columnar flag, partition semantics incl. BigQuery `_PARTITIONTIME`/clustering), each with its own test file like `test_athena.py` | P1 | 4 d each | Full suite parity per dialect; README support matrix updated |
| 3.5 | **dbt manifest importer** — `Catalog.from_dbt_manifest("manifest.json")`: schemas, docs, tags (PII!), relationships from dbt tests (`relationships`, `accepted_values` → FK graph + enums) | P0 | 3 d | A dbt project's guardrails configure themselves from artifacts it already has |
| 3.6 | **Catalog freshness contract** — snapshot age metadata, staleness warnings, delta-refresh helpers | P2 | 2 d | Guard warns when validating against a catalog older than a configured TTL |

## Phase 4 — Ecosystem & developer experience

*Goal: be one import away in every stack where text-to-SQL is built.*
*Detailed plan: [plans/phase-4-ecosystem.md](plans/phase-4-ecosystem.md)*

| # | Item | Prio | Effort | Acceptance criterion |
|---|---|:---:|:---:|---|
| 4.1 | **MCP server** — expose `validate` / `policy_prompt` / `catalog` as MCP tools so any agent (Claude Code, desktop, custom) gets guarded SQL natively | P0 | 3 d | An MCP-enabled agent generates → validates → repairs without custom glue |
| 4.2 | **Framework adapters** — LangChain `Tool`/output-validator, LlamaIndex query-pipeline component, Guardrails-AI validator shim (be the engine inside their `valid_sql` slot) | P1 | 4 d | Each adapter is ≤50 lines for users, with a runnable example |
| 4.3 | **CLI** — `sqlguard validate q.sql --catalog cat.json --policy policy.yaml --dialect athena --json`; exit codes for CI use | P1 | 2 d | SQL review runs in a pre-merge pipeline with no Python written |
| 4.4 | **Policy-as-config** — YAML/JSON policy files with a published JSON Schema, env-var interpolation, `Policy.from_file()` | P1 | 2 d | Security team edits policy without touching application code |
| 4.5 | **FastAPI reference service** — containerized validate-as-a-service with auth, audit sink, and the self-repair loop built in | P2 | 3 d | `docker run` → POST /validate works; used as the deployment blueprint |
| 4.6 | **Docs site** — mkdocs-material: quickstart, failure-mode cookbook (one page per violation code with repair examples), architecture, policy reference | P1 | 4 d | Every `Code` value has a documented page the feedback string can link to |
| 4.7 | **`validate_many` batch API** + free-threading benchmarks | P3 | 1 d | Batch of 100 queries validates with one catalog lock-in, documented throughput |

## Phase 5 — Exploratory (validate demand before building)

*Detailed plan: [plans/phase-5-exploratory.md](plans/phase-5-exploratory.md)*

| # | Item | Prio | Effort | Notes |
|---|---|:---:|:---:|---|
| 5.1 | **`repair()` convenience loop** — user supplies an LLM callable; guard drives bounded validate→feedback→regenerate rounds | P2 | 2 d | Keeps the library LLM-agnostic: callable in, no SDK dependency |
| 5.2 | **Intent-consistency checking** — compare the user's NL question against the SQL's semantics (tables/filters/aggregations) via embeddings/NLI; flag "answers a different question" | P3 | 2–3 w | Ship as an optional extra (`[intent]`); high research risk, high payoff |
| 5.3 | **Policy tuning from telemetry** — aggregate violation stats → suggested policy diffs ("13% of blocks are `notes`; consider exclude_from_star") | P3 | 1 w | Needs 2.5 audit stream first |
| 5.4 | **Go/Rust hot path** — only if a per-query gateway use case materializes; keep the control plane in Python (per the v1 decision) | P3 | — | Trigger: a user needs <100 µs p99 in a proxy |

---

## Suggested release train

| Release | Contents | Theme |
|---|---|---|
| **v0.2** | Phase 0 (all) + 3.5 dbt importer + 2.7 shadow mode | *Trustworthy & installable*: tested adapters, PyPI, fuzzing, safe rollout |
| **v0.3** | 1.1 FK joins, 1.2 aggregation, 1.5 enums, 2.1 masking, 3.1 partition stats | *Catches what others can't* |
| **v0.4** | 2.2–2.5 security depth, 4.1 MCP, 4.3 CLI, 4.4 policy-as-config | *Enterprise-shaped* |
| **v0.5** | 3.4 dialect packs (Snowflake, BigQuery first), 4.2 adapters, 4.6 docs site | *Everywhere* |
| **v1.0** | API freeze, stability guarantees, deprecation policy, security audit pass | *Boring by design* |

## Cross-cutting rules (apply to every item)

1. **Fail closed, always.** New checks may only add violations; an internal error is never a pass.
2. **`Code` enum values are frozen** once released; new failure classes get new codes.
3. **Every feature lands with tests, a violation-code docs page, and a feedback-string that an LLM can act on** — the repair loop is the product.
4. **No new required dependencies.** Everything beyond `sqlglot` stays behind extras.
5. **Benchmark before merge.** The perf gate (0.5) applies to every phase.
