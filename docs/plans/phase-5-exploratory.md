# Phase 5 — Exploratory tracks

> Roadmap: [Phase 5](../ROADMAP.md#phase-5--exploratory-validate-demand-before-building) · Target: unscheduled — each item sits behind an explicit **decision gate**
>
> These are plans for *experiments*, not commitments. Each defines the
> hypothesis, the cheapest honest test of it, and the go/no-go criterion, so
> we spend research time only where a gate opens. None of these may touch the
> core pipeline's guarantees (fail-closed, frozen codes, no new required
> deps) even in prototype form.

---

## 5.1 `repair()` convenience loop (P2, 2 d)

**Hypothesis.** Most integrators hand-write the same 15-line
validate→feedback→regenerate loop; owning it improves convergence and gives
us loop telemetry.

**Design.** `sqlguard/repair.py`:
```python
class GenerateFn(Protocol):
    def __call__(self, prompt: str, feedback: str | None, attempt: int) -> str: ...

def repair(
    guard: SQLGuard,
    generate: GenerateFn,          # user's LLM callable — any SDK, or none
    request: str,                  # the NL question (passed through to generate)
    params: Mapping | None = None,
    role: str | None = None,
    max_rounds: int = 3,
) -> RepairOutcome                 # (result, rounds, transcript)
```
- The library stays LLM-agnostic: a callable in, no SDK dependency, no
  network code. `policy_prompt()` is prepended to `prompt` on round 0.
- Termination: valid result, `max_rounds` exhausted, or **no-progress
  detection** (identical violation-code multiset two rounds running ⇒ stop
  early — the model is looping).
- `RepairOutcome.transcript` (list of (sql, feedback) pairs) feeds evaluation
  and 5.3 telemetry. The Phase 4 framework adapters re-wrap this function.

**Gate to build:** two independent integration requests for the loop, or the
LangChain adapter needing it (4.2 lists it as shared core — that gate is
effectively open; build alongside 4.2).

**Acceptance.** The scripted `examples/self_repair_loop.py` rewritten on
`repair()` behaves identically; no-progress case terminates in 2 rounds.

## 5.2 Intent-consistency checking (P3, 2–3 w, research-risk)

**Hypothesis.** A query can pass every structural check and still answer a
different question than the user asked ("revenue by month" → SQL grouping by
week). Facet comparison between the NL request and the validated SQL can
flag this *class* of error with useful precision.

**Cheapest honest test.** Before any model work:
1. Build the deterministic half: `extract_facets(result)` — tables, filter
   columns+operators, aggregation functions, group-by columns, time grain
   (from `date_trunc`/partition predicates), limit/ordering. This is pure
   AST work on machinery we already have, and is independently useful
   (audit records, 5.3).
2. Assemble an eval set: 300 (question, SQL, label) triples — 150 correct
   pairs from public text-to-SQL dev sets (Spider/BIRD subset re-based onto
   our fixture catalogs), 150 mismatch pairs made by systematic perturbation
   (swap aggregation, wrong grain, dropped filter, wrong table).
3. Baseline A: embedding similarity (question vs verbalized facets) with an
   off-the-shelf sentence encoder. Baseline B: small-LLM judge with a fixed
   rubric prompt over (question, facets). Measure **precision at the
   operating point where recall = 30%** — this check may only ever be a
   WARNING, so false positives are the thing that kills it.
- Prototype lives in `experiments/intent/` (not shipped in the wheel); any
  eventual product form is an optional extra (`[intent]`) emitting
  `Code.INTENT_MISMATCH` at WARNING severity, never ERROR.

**Go/no-go gate:** ≥80% precision at 30% recall on the held-out split for
either baseline. Below that: ship `extract_facets()` alone (it earns its
keep) and shelve the classifier.

## 5.3 Policy tuning from telemetry (P3, 1 w)

**Hypothesis.** A month of audit records contains the policy you *should*
have: which denies are noise, which budgets are mis-sized, which tables need
FK declarations.

**Design.** `sqlguard audit analyze records.jsonl` (CLI, consumes the 2.5
`AuditRecord` stream; pure offline, no runtime coupling):
- Violation-code histogram, per table/column; would-block rate over time
  (shadow-mode fleets get their flip-the-switch report from this);
- Rule-shaped suggestions, each with evidence counts:
  *"13% of blocks are `column_denied` on `orders.notes` — consider
  `exclude_from_star` instead of `deny` (312 occurrences, 0 explicit
  selects)"*; *"`scan_budget_exceeded` median overshoot is 1.2× — budget may
  be 20% too tight"*; *"418 `invalid_join_path` warnings on
  (events, dim_user) — declare the FK"*;
- Output: human report + `--emit-policy-diff` producing a commented YAML
  overlay (4.4 format) a human reviews — **suggestions never self-apply**.

**Gate to build:** 2.5 audit sink shipped *and* at least one deployment
running shadow mode with ≥10k records to design against real distributions.

## 5.4 Go/Rust hot path (P3, effort TBD)

**Position (unchanged from v1).** Python-only was the right call: 2–6 ms per
validation is ~0.1–0.6% of an LLM round-trip. A native port is justified only
by a *per-query gateway* deployment (sitting on every DB query, not every
LLM generation), where the budget is <100 µs p99.

**Trigger conditions (all three, written down so the debate is short):**
1. A concrete user with a gateway/proxy use case and a stated latency budget
   the Python core misses by ≥10× after 0.4's scope-index work;
2. Public API frozen at v1.0 (violation codes, result schema, policy
   surface) — a port tracks a stable spec, not a moving one;
3. The maintenance question answered: who owns the second implementation's
   conformance? (Prerequisite artifact: a language-agnostic **conformance
   corpus** — `(catalog, policy, sql) → expected result JSON` fixtures —
   which is worth building for the Python implementation's own regression
   safety regardless.)

**If triggered:** Rust core (sqlparser-rs or a sqlglot-rs binding) exposing
the same result JSON; Python keeps the control plane (reflection, config,
integrations) — the exact split the original plan predicted. First
deliverable would be the conformance corpus runner, not the port.

---

## Definition of done (phase)

Not applicable in the release-train sense — each item exits through its gate:

- [ ] 5.1 ships with the framework adapters (gate effectively open)
- [ ] 5.2 go/no-go measured and recorded (either outcome is a result;
      `extract_facets` ships regardless)
- [ ] 5.3 blocked on audit-stream data; revisit one quarter after 2.5 ships
- [ ] 5.4 trigger conditions reviewed at v1.0; conformance corpus built
      independently of the decision
