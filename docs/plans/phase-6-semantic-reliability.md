# Phase 6 — Intent fidelity and reliable evaluation

> Research basis: [October 2026 review](../research/2026-10-text-to-sql-review.md),
> [Post-SQLFix](https://arxiv.org/abs/2610.41472), and
> [business-semantic revalidation](https://arxiv.org/abs/2610.41090)
> · Target: **v0.6–v0.8** · Core effort: ~32 engineering days
>
> Goal: detect valid-but-wrong SQL by checking a typed statement of business
> intent, governed metric/grain semantics, and evidence quality. Every runtime
> feature is optional and off by default; the existing SQL-only API remains
> compatible.

## Sequencing

```text
6.5 evaluation schema ──▶ 6.1 intent contract ──▶ 6.2 semantic catalog ──▶ 6.9 revisions
                                  │                        │                   │
                                  ├──▶ 6.3 clarification  ├──▶ 6.4 probes     └──▶ 6.7 fingerprints
                                  └──▶ 6.8 checked repair └──▶ 6.6 turn context
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

Keep business-facing semantic objects separate from their physical mappings.
An LLM-facing/public view contains semantic IDs, descriptions, aliases,
cardinality, and supported grains but excludes table names, column names, join
predicates, and governed predicate values. The full mapping remains local and
is included in the catalog digest. Tests inspect the complete outbound payload,
not only the semantic-object block, before making any identifier-hiding claim.

**Delivery slices.** (A) immutable catalog, evidence, canonical digest, eager
physical binding, semantic measure IDs, mandatory filters, and direct fan-out
checks; (B) selectable version-bound route handles, role predicates, safe
pre-aggregation recognition, and broader grain compatibility; (C) dbt
semantic-manifest/OSI import. Each slice updates the 6.5 known-gap baselines.

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
Changing the catalog or physical mapping changes its digest, and the public
semantic view contains no physical identifiers.

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

Report result equivalence under explicit comparator semantics: ordered rows,
sets, and multiplicity-preserving multisets. A set match must never imply a
multiset match; duplicate inflation, missing `DISTINCT`, and fan-out are tracked
as their own failure slice.

**Governance.** New/changed labels require rationale and review. Track excluded
or disputed cases instead of deleting them. Report results under both accepted
SQL and multi-instance execution oracles. Keep private production cases outside
the repository but run the same schema and evaluator.

Every report names and versions its oracle: corpus revision, database-instance
suite, comparator, accepted SQL variants, and adjudication source. Automated
execution disagreements are review triggers, not automatic semantic truth.
Audit samples from both oracle-accepted and oracle-rejected predictions so false
acceptance and false rejection remain measurable. Keep reference defects,
underspecified questions, unrealistic instances, comparator errors, and model
errors as separate outcomes.

For ambiguity cases, record planned, asked, answered, and explicitly dropped
issues. Report execution accuracy, ambiguity recall, grounded success,
correct-but-ungrounded success, silent failure, and clarification cost
separately. Later multi-turn cases pair additive elaboration with explicit
supersession/intent-drift cases over the same anchor question.

**Acceptance.** Land a versioned schema and deterministic CI smoke set first.
Before the phase exits: at least 100 public, license-compatible cases covering
every intent facet and mutation class; ≥25 ambiguous/unanswerable cases; ≥20
multi-instance counterfactual cases; both set and multiset comparisons; and an
adjudicated audit sample drawn from both accepted and rejected oracle outcomes.
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

## 6.8 Monotonic repair acceptance and regression interception (P0, 4 d)

**Problem.** Execution-feedback loops let the same probabilistic model propose a
repair and implicitly decide that the repair is better. A candidate can execute
while introducing a new semantic, policy, or structural regression. With a
single retry budget, accepting the wrong transition is worse than returning the
original deterministic failure.

**Contract.** Add an LLM-agnostic repair coordinator. The caller supplies the
proposal callable; the guard supplies structured feedback and remains the only
authority that may accept the transition:

```python
RepairRequest(
    error_type="aggregation_grain_mismatch",
    affected_clause="GROUP BY",
    evidence=(...),
    permitted_changes=("group_by", "projection"),
    max_attempts=1,
)

outcome = guard.repair_once(sql, proposer, intent=intent)
```

The original and candidate each produce an immutable validation snapshot with
AST identity, violations, confirmed checks, undecidable checks, policy/catalog/
semantic revisions, and execution evidence. The candidate is reparsed and every
applicable validator runs again. Execution success is evidence only and cannot
override a deterministic denial.

**Acceptance rule.** Apply a candidate only when all conditions hold:

1. the blocking-violation set strictly decreases;
2. every previously confirmed invariant remains confirmed—none becomes a
   violation or undecidable;
3. no violation outside the targeted rule family is introduced;
4. the AST diff stays within the declared edit boundary;
5. policy, role, physical catalog, semantic revision, and intent are unchanged;
6. final validation is stable and idempotent.

A failed proposal is rejected and the original result remains authoritative.
Audit both accepted and rejected transitions, including the changed clauses,
before/after violation sets, execution evidence, and rejection reason. Default
to one proposal; higher budgets require explicit policy and retain the same
monotonic checks on every transition.

**Metrics.** Repair eligibility, proposal rate, accepted-repair rate, true repair
rate, regression-interception rate, unresolved rate, token/latency cost, and
violations before/after. Report execution repair separately from intent repair.

**Acceptance.** Mutation tests cover fixes that trade one error for another,
broaden filters, remove mandatory predicates, change aggregation grain, bypass
RLS, or alter unrelated clauses. CI proves that no accepted candidate regresses
a previously confirmed invariant, every rejected candidate leaves the original
result intact, the one-attempt budget always halts, and revalidating an accepted
result is idempotent.

## 6.9 Semantic revision lifecycle and targeted revalidation (P0, 3 d)

**Problem.** A schema can remain unchanged while a business definition changes.
Cached validated SQL may therefore keep executing successfully with stale
meaning—for example, after changing a claim-status mapping, eligible population,
date basis, or metric formula. A content digest alone does not express which
definition was requested or when it applies.

**Model.** Keep these identities separate:

- semantic document/schema-format version;
- physical catalog version;
- policy version;
- business-definition revision;
- event/report interval and knowledge cutoff.

Add immutable revision metadata to governed semantic objects: stable object ID,
revision ID, effective interval, optional knowledge cutoff, superseded revision,
change reason, owner/evidence, and content digest. A resolved `QueryIntent`
binds each referenced object to an explicit revision. The latest applicable
revision may be resolved by policy, while an explicitly requested historical
revision remains valid.

**Revalidation.** Produce a semantic-revision diff listing changed objects and
their dependents. Use it to invalidate affected template fingerprints and replay
the relevant 6.5 cases. Unrelated definitions retain their cache identities.
The audit record states the requested and resolved revisions; it never silently
substitutes a newer definition.

For high-risk definition changes, support obligation fixtures containing legal
counterexample data that distinguishes old and new meanings. Agreement on the
current database is recorded as finite evidence, not proof of conformance.

**Acceptance.** In the claims fixture, changing the meaning of “under review,”
a mandatory population filter, or the processed-date basis invalidates every
affected cached validation and selects the new expected SQL. An explicit
historical request continues to validate against its old revision. An unrelated
metric change preserves unaffected fingerprints. Replay output identifies
conforming, nonconforming, and unresolved cases without treating snapshot
agreement as semantic proof.

## Release slices

| Release | Contents | Exit condition |
|---|---|---|
| v0.6 | 6.5 corpus schema + 6.1 intent checks + 6.8 checked repair | Deterministic mutation targets met; accepted repairs preserve every prior invariant; legacy API unchanged |
| v0.7 | 6.2 semantic catalog + 6.9 revision lifecycle + dbt semantic import | Claims fixture catches route/grain/fan-out errors; definition changes trigger targeted revalidation |
| v0.8 | 6.3, 6.4, 6.6, 6.7 | Clarification/probe privacy gates and cache invalidation matrix pass |

## Phase definition of done

- [ ] SQL can be checked against an independently produced typed intent
- [ ] Governed measures, relationship roles, and grains are versioned and enforced
- [ ] Ambiguity/unanswerable outcomes have a structured abstention path
- [ ] Optional probes reuse policy/RLS and have hard execution budgets
- [ ] Evaluation separates safety, execution, intent, abstention, and repair regression
- [ ] Multi-turn context is caller-owned, explicit, current, and auditable
- [ ] Cached validation identities include every security/semantic dependency
- [ ] Every accepted repair makes deterministic progress and preserves all previously confirmed invariants
- [ ] Business-definition revisions are distinct from schema versions and trigger targeted cache invalidation and replay

