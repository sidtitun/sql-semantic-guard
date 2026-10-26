# Text-to-SQL research review — October 2026

This review compares recent research and production reports with SQL Semantic
Guard at commit `0eb8eab` (after driver parameter support). It is a design
input, not a claim that every reported benchmark result generalizes to this
project. Several 2026 sources are preprints; their architectural ideas are
useful, but their headline scores require independent reproduction.

## What the repository already covers well

The current guard already supplies a strong *SQL safety and structural
correctness* boundary: read-only statement enforcement, schema binding,
function/type/aggregation checks, FK-aware joins, enum validation, RLS,
masking, role policies, complexity and cost limits, shadow mode, audit records,
driver bindings, fuzzing, and a frozen SQL behavior corpus.

That boundary is necessary but incomplete. A query may pass every existing
check, execute successfully, and still answer the wrong business question.
Recent work consistently moves the remaining risk from SQL syntax toward
intent fidelity, governed semantics, ambiguity, and evaluation quality.

## Evidence-to-gap matrix

| Evidence | Production lesson | Current gap | Planned response |
|---|---|---|---|
| [Bounded Semantic Planning and Deterministic Compilation](https://doi.org/10.5281/zenodo.21966805) | Keep semantic choices typed and finite; resolve routes and grain in code; distinguish violations from undecidable findings | The guard accepts SQL only; it has no structured statement of the user's intended measures, dimensions, filters, route, or output grain | 6.1 typed `QueryIntent`; 6.2 governed semantic catalog and grain checks |
| [EntSQL](https://doi.org/10.48550/arXiv.2606.03363) | Enterprise questions depend on private metric definitions, reporting rules, mappings, and source documents beyond DDL | Catalog metadata describes physical columns, values, and FKs, but not governed metrics, fiscal rules, semantic evidence, or versions | 6.2 semantic objects with provenance and versioning |
| [PV-SQL](https://doi.org/10.48550/arXiv.2604.17653) | High-precision rules can check question constraints such as distinctness, top-k, counting, percentages, aggregation, comparison, and temporal order; bounded probes improve grounding | Existing checks validate SQL internally but cannot tell whether a requested constraint is absent | 6.1 deterministic coverage checks; 6.4 constrained probe interface |
| [AmbiSQL](https://doi.org/10.1145/3788853.3801581), [Query Carefully](https://doi.org/10.48550/arXiv.2512.21345), and ARCS (arXiv:2610.09396) | Ambiguous, underspecified, and unanswerable questions should produce targeted clarification or abstention rather than guessed SQL | No answerability/clarification result schema or abstention metric | 6.3 clarification and abstention protocol |
| [Memory Architectures for Multi-Turn Text-to-SQL](https://doi.org/10.48550/arXiv.2605.26394) | Recent working context helps; larger episodic/semantic memory can help or hurt, and stale context can silently change intent | Role is recorded, but there is no caller-supplied resolved-turn context or provenance check | 6.6 immutable resolved-context contract; conversation storage stays outside the library |
| [Text-to-SQL Benchmarks are Broken](https://www.vldb.org/cidrdb/papers/2026/p5-jin.pdf) | Gold SQL, questions, database contents, and domain assumptions may all be wrong or ambiguous | The frozen corpus tests guard behavior only and explicitly does not measure question-to-SQL correctness | 6.5 adjudicated question/intent corpus with label provenance |
| [Beyond Text-to-SQL: Enterprise ETL Debugging](https://doi.org/10.48550/arXiv.2601.18119) | Real enterprise SQL needs structural mutation cases, semantic bug classes, and execution-free diagnostics | Fuzzing targets crashes/idempotence, not realistic semantic faults | 6.5 semantic mutation generator and failure taxonomy |
| [BIRD-CRITIC](https://bird-critic.github.io/) | Repair should be measured by bug class and regression rate, not only final execution | No repair benchmark or before/after semantic regression accounting | 6.5 repair-regression evaluation; 5.1 consumes it |
| [dbt's 2026 semantic-layer benchmark](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026) | Governed metric definitions can matter more than changing the model | Planned dbt import covers schema tests but not MetricFlow-style measures, grains, dimensions, or entities | Expand 3.5 through 6.2 rather than adding an LLM dependency |
| [AWS parameterized template caching](https://aws.amazon.com/blogs/architecture/reducing-text2sql-latency-with-parameterized-query-templates/) | Reusing validated query shapes can reduce latency and model calls if cache identity includes semantics and policy | Driver binding exists, but no stable, policy-aware query fingerprint | 6.7 validated template fingerprint and cache contract |

## Decisions from the review

1. **Do not select an LLM in this library.** The guard remains deterministic
   and model-agnostic. Model comparison belongs in adapters and evaluations.
2. **Make intent an optional, typed input.** Existing `validate(sql)` remains
   byte-compatible. `validate(sql, intent=...)` adds checks; it never weakens
   existing policy.
3. **Prefer explicit abstention over guessed meaning.** Ambiguity findings are
   structured outputs. Only the caller can ask the user a question.
4. **Treat execution as evidence, not proof.** A single database instance can
   make different queries look equivalent. Counterfactual data and independent
   intent contracts are required for semantic evaluation.
5. **Keep database access outside the core validator.** Probing is opt-in,
   read-only, budgeted, adapter-owned, and returns facts with provenance.
6. **Import semantic layers rather than recreate their authoring experience.**
   SQL Semantic Guard consumes governed metrics and relationships and checks
   SQL against them; it does not become a BI modeling tool.

## Recommended order

Start with 6.5's corpus schema and 6.1's `QueryIntent` together. The corpus
prevents us from designing intent checks around hand-picked examples. Then add
metric/grain semantics (6.2), clarification contracts (6.3), fingerprints
(6.7), bounded probes (6.4), and finally multi-turn integration (6.6).

## Post-6.1 evaluation addendum

The review after typed intent validation added three requirements to 6.5:

- **Version the oracle, not only the corpus.** Recent oracle-comparison work
  shows that shipped databases, generated multi-instance suites, and expert
  judgments can disagree in both directions. Reports must name the database
  suite, comparator, accepted references, and adjudication source, and sample
  both accepted and rejected outcomes for review.
- **Preserve multiplicity.** Set-based execution scoring can hide missing
  `DISTINCT`, duplicate inflation, and fan-out. The evaluator records ordered,
  set, and multiset equivalence separately and never treats a set match as proof
  of bag-equivalent SQL.
- **Separate groundedness from execution.** Ambiguity benchmarks distinguish
  correct-but-ungrounded answers from grounded success and silent failure.
  Corpus cases therefore track ambiguity lifecycle and coverage independently
  from final SQL correctness, preparing the contract for 6.3 and 6.6.

Design signals: *Certified Against Which Oracle?* (arXiv:2609.25938),
*ModularSQL* (arXiv:2609.29573), *Beyond Correctness: Resolving
Underspecification in Agentic Text-to-SQL* (arXiv:2610.02739), and *TIDE-Bench*
(arXiv:2608.29543). These are recent preprints; their concrete thresholds are
not adopted as universal production gates.

