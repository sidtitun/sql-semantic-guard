# Phase 6 — Intent fidelity and reliable evaluation

> Research basis: [October 2026 review](../research/2026-10-text-to-sql-review.md)
> · Target: **v0.6–v0.8** · Core effort: ~25 engineering days
>
> Goal: detect valid-but-wrong SQL by checking a typed statement of business
> intent, governed metric/grain semantics, and evidence quality. Every runtime
> feature is optional and off by default; the existing SQL-only API remains
> compatible.

## Sequencing

```text
6.5 evaluation schema ──▶ 6.1 intent contract ──▶ 6.2 semantic catalog
                                  │                        │
                                  ├──▶ 6.3 clarification  ├──▶ 6.4 probes
                                  └──▶ 6.7 fingerprints   └──▶ 6.6 turn context
```

6.5 starts first but lands with the first 6.1 fixtures. It is the acceptance
framework for the rest of the phase, not a final test pass added afterward.

## 6.1 Typed query intent and constraint coverage (P0, 5 d)

**Problem.** The current guard can prove that `SUM(amount)` is legal while
missing that the user asked for an average, monthly grain, a top-five result,
or a specific filter.

**API.** Add immutable, JSON-serializable semantic objects:

```python
QueryIntent(
    measures=(MeasureIntent(id="net_revenue", aggregation="sum"),),
    dimensions=(DimensionIntent(id="region"),),
    filters=(FilterIntent(id="status", operator="eq", values=("paid",)),),
    time_grain=TimeGrainIntent(dimension="paid_at", grain="month"),
    ordering=(OrderIntent(id="net_revenue", direction="desc"),),
    limit=5,
    distinct=False,
    expected_grain=("month", "region"),
)

guard.validate(sql, intent=intent)
```

The intent references semantic IDs when a semantic catalog is present and may
use physical column IDs for an initial catalog-only version. The guard extracts
SQL facets from the final AST and compares them deterministically.

**Initial checks.** Projection/measure presence; aggregation operator;
required filters and literal/parameter identity; group-by dimensions; time
grain; distinctness; top-k as `ORDER BY` + compatible `LIMIT`; comparison
direction; requested sort; and output grain. Each finding contains expected
and observed facets plus a repair hint.

**Result model.** Add `IntentCheckResult` to `QueryStats` and two codes:
`intent_constraint_missing` and `intent_constraint_conflict`. Unknown or
undecidable constructs are recorded separately and do not silently pass as
confirmed. Policy selects `intent_enforcement="off|warn|block"`, default off.

**Non-goals.** No natural-language parsing, model call, prompt orchestration,
or claim of full semantic equivalence. The application/LLM constructs the
intent; the guard checks SQL against it.

**Acceptance.** On the 6.5 mutation set, block 100% of deterministic missing or
conflicting constraint mutations and false-block ≤2% of adjudicated matches.
Every finding is stable across formatting and alias changes.

## 6.2 Governed semantic catalog, route roles, and grain safety (P0, 7 d)

**Problem.** Physical catalogs cannot express business metrics such as net
revenue, fiscal month, claim count, relationship roles, or the grain at which a
measure is additive. This leaves fan-out double counting and plausible wrong
joins undetected.

**Model.** Extend `Catalog` with separately versioned semantic objects:

- `Entity`: stable ID, physical table, primary grain, aliases, documentation;
- `Measure`: expression AST, aggregation, source grain, mandatory filters,
  additive dimensions, return type;
- `Dimension`: expression/column, value mappings, time semantics;
- `RelationshipRole`: FK path plus business role, cardinality, allowed
  traversal direction and fan-out behavior;
- `SemanticEvidence`: source URI/document ID, source version, owner, updated-at;
- `SemanticCatalogVersion`: content digest used by audit and cache identity.

Expressions are parsed and bound eagerly. Callers select route IDs; they never
submit editable join predicates under a trusted route name.

**Checks.** Recognize declared measures in the AST; verify mandatory filters;
verify route edges and role predicates; compare measure/output grain; detect
fan-out before aggregation; require safe pre-aggregation or refuse when a
lowering is undecidable. Results distinguish `violation`, `confirmed`, and
`undecidable`.

**Import.** Expand roadmap 3.5 to consume dbt manifests plus available semantic
manifest/MetricFlow entities, measures, dimensions, grains, and time dimensions.
Keep the internal model independent from dbt.

**New codes.** `unknown_semantic_object`, `metric_definition_mismatch`,
`relationship_role_mismatch`, `aggregation_grain_mismatch`, and
`fanout_aggregation_risk`.

**Acceptance.** Detect wrong relationship role and duplicated measures in a
claims/insurance fixture; accept safe pre-aggregation controls; reject invented
route IDs at construction; round-trip semantic catalogs deterministically.

## 6.3 Clarification, answerability, and abstention protocol (P1, 3 d)

**Problem.** The safest response to missing meaning is often a question or
refusal, but the library exposes only SQL violations.

**Design.** Add a model-independent schema for upstream analyzers:

```python
ClarificationIssue(
    kind="ambiguous_metric",             # or missing_scope, out_of_domain, ...
    question="Should revenue mean gross or net revenue?",
    choices=("gross_revenue", "net_revenue"),
    evidence=(...),
    required=True,
)
```

`QueryIntent` cannot be marked ready while required issues remain unresolved.
Each issue has an explicit lifecycle: planned, asked, answered, or dropped with
a reason. This prevents a planner from identifying ambiguity and silently
forgetting it. The library validates the lifecycle; the caller owns dialogue.

**Deterministic discovery.** Report multiple semantic aliases, multiple valid
relationship roles, absent time scope where policy requires one, unknown
entities/metrics, and filters whose values cannot be grounded. Pluggable
question analyzers may add issues but cannot mark core issues resolved.

**Metrics.** Clarification precision, ambiguity coverage, turns-to-resolution,
unnecessary-question rate, unresolved-required count, refusal rate, and
wrong-but-executed rate.

**Acceptance.** No SQL is released under block policy with an unresolved
required issue. A resolved choice produces exactly one stable intent. Existing
`validate(sql)` behavior is unchanged.

## 6.4 Safe, bounded database probing interface (P1, 4 d)

**Problem.** Schema names alone cannot ground opaque IDs, valid category
values, or mappings. Unrestricted agent-generated probes create a new query
execution and data-leak surface.

**Design.** Define `ProbeRequest`, `ProbePolicy`, `ProbeResult`, and a
`ProbeExecutor` protocol. Core constructs/validates probe ASTs; adapters execute.
Allowed templates in the first release: distinct sample values for one approved
column, exact lookup by approved key, and min/max date. Enforce read-only SQL,
approved tables/columns, row limit, timeout, scan budget, role/RLS, maximum
probe count, and result redaction. Audit probe purpose, shape hash, catalog
version, and row count; never audit returned values by default.

Probe results carry source, query hash, retrieved-at, and expiry. They may
ground an intent literal but cannot modify policy or semantic definitions.

**Acceptance.** No free-form SQL executor exists in the public API. All probe
templates pass through the existing guard. Attempts to probe denied columns or
bypass RLS fail closed. Integration tests prove row/time/count budgets.

## 6.5 Semantic evaluation and benchmark governance (P0, 5 d)

**Problem.** `evals/` measures SQL safety behavior, not whether SQL answers a
question. Single gold SQL and single database execution can reward wrong logic;
public benchmark labels can themselves be ambiguous or incorrect.

**Corpus schema.** Versioned cases contain:

- question, structured `QueryIntent`, catalog/semantic-catalog digests;
- one or more accepted SQL variants and explicit equivalence conditions;
- adjudication status, reviewer count, rationale, source provenance;
- ambiguity/unanswerable label and expected clarification outcome;
- database fixtures including discriminating counterfactual instances;
- semantic mutations: dropped/swapped filter, wrong aggregation or time grain,
  missing DISTINCT, wrong top-k ordering, wrong relationship role, fan-out,
  stale context, and correct-on-current-data-only SQL.

**Metrics.** Constraint precision/recall by class; false-block rate; execution
agreement on each database instance; wrong-but-executed rate; abstention
precision/coverage; repair success and repair regression; repeated-run solid
success and at-least-once success for integrations. Never collapse parse,
runtime, policy, intent, refusal, and wrong-result failures into one score.

**Governance.** New/changed labels require rationale and review. Track excluded
or disputed cases instead of deleting them. Report results under both accepted
SQL and multi-instance execution oracles. Keep private production cases outside
the repository but run the same schema and evaluator.

**Acceptance.** At least 100 public, license-compatible cases covering every
intent facet and mutation class; ≥25 ambiguous/unanswerable cases; ≥20
multi-instance counterfactual cases; deterministic evaluator and CI smoke set.
Do not publish a general Text-to-SQL accuracy claim from this corpus.

## 6.6 Caller-owned resolved-turn context (P2, 2 d)

**Problem.** Multi-turn systems need recent context, but storing conversation
memory in the guard would couple it to orchestration and can reintroduce stale
or cross-session state.

**Design.** Accept an optional immutable `ResolvedContext` containing current
entity/metric/filter bindings, provenance turn IDs, and explicit supersession
links. The guard validates that intent references only current bindings and
records a context digest in audit output. It stores no sessions and performs no
retrieval. Adapters provide a two-turn working-context example and must pass a
fresh snapshot on every validation.

**New code.** `stale_context_binding`.

**Acceptance.** A changed metric cannot inherit the prior metric accidentally;
retained dates/entities work when explicitly carried forward; session IDs and
values remain absent from default logs.

## 6.7 Validated template fingerprint and cache contract (P1, 2 d)

**Problem.** Parameter binding makes reusable query shapes possible, but a SQL
hash alone cannot safely identify a cached validation across roles, policies,
catalog versions, or semantic intent.

**Design.** Add `ValidationResult.template_fingerprint` derived from a
canonical placeholder-normalized AST plus dialect, role policy digest, physical
and semantic catalog digests, and intent digest. Provide a cache-key helper and
serialization format; provide no cache store and cache no result rows.

Only valid results receive a fingerprint. Commutative normalization is limited
to transformations proven safe; aliases/formatting normalize, predicate order
does not initially. Audit records may carry the fingerprint.

**Acceptance.** Formatting and bind values preserve the key; role, RLS,
policy, catalog, semantic intent, and SQL-structure changes invalidate it.

## Release slices

| Release | Contents | Exit condition |
|---|---|---|
| v0.6 | 6.5 corpus schema + 6.1 intent checks | Deterministic mutation targets met; legacy API unchanged |
| v0.7 | 6.2 semantic catalog + dbt semantic import | Claims fixture catches route/grain/fan-out errors |
| v0.8 | 6.3, 6.4, 6.6, 6.7 | Clarification/probe privacy gates and cache invalidation matrix pass |

## Phase definition of done

- [ ] SQL can be checked against an independently produced typed intent
- [ ] Governed measures, relationship roles, and grains are versioned and enforced
- [ ] Ambiguity/unanswerable outcomes have a structured abstention path
- [ ] Optional probes reuse policy/RLS and have hard execution budgets
- [ ] Evaluation separates safety, execution, intent, abstention, and repair regression
- [ ] Multi-turn context is caller-owned, explicit, current, and auditable
- [ ] Cached validation identities include every security/semantic dependency

