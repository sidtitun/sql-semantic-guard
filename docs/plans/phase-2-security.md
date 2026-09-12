# Phase 2 — Security depth

> Roadmap: [Phase 2](../ROADMAP.md#phase-2--security-depth-enterprise-asks) · Target: **v0.3–v0.4** · Total effort: ~20 days
>
> Goal: cover the full enterprise guardrail matrix (the "7-layer" deployments
> build by hand today): masking, richer RLS, roles, complexity ceilings,
> auditability, and a safe rollout mode. Every item defaults *off* — existing
> policies behave identically after upgrade.

## Sequencing

```
2.7 shadow mode (1 d, ship first — de-risks every later rollout)
2.5 audit hooks ──▶ (5.3 policy tuning consumes the stream later)
2.1 masking ──▶ 2.3 role policy sets (roles compose masking/RLS per role)
2.2 expression RLS (independent) · 2.4 complexity budgets (independent)
2.6 paramstyle matrix (independent)
```

---

## 2.1 Column masking (P0, 4 d)

**Ask.** Compliance rarely wants a hard block on PII — they want analysts to
*see the shape, not the value*. v1 only has `deny`/`exclude_from_star`.

**Design.**
- `ColumnRule(action="mask", mask_with="left({col}, 4) || '****'")`. The
  template is parsed at `Policy` construction: substitute `{col}` with a
  sentinel column, `sqlglot.parse_one(..., dialect)` — parse failure ⇒
  `PolicyError` at build time, never at query time. Built-ins for the lazy:
  `mask_with="null"` (typed NULL: `CAST(NULL AS <col type>)`),
  `"hash"` (`md5(CAST({col} AS TEXT))` pg / `to_hex(md5(to_utf8({col})))`
  trino — per-dialect table), `"redact"` (`'***'`).
- Application point: `rewrite.apply_column_rules`, which already resolves the
  physical origin of every projection item and every explicit column:
  - projection items (star-expanded *and* explicit): replace the column node
    with the mask expression, preserving the output alias
    (`mask(...) AS email`) — note `Rewrite(kind=COLUMN_MASKED)`;
  - references in WHERE/GROUP BY/ORDER BY/JOIN-ON: **not maskable** without
    changing semantics ⇒ `Code.COLUMN_DENIED` by default; new
    `ColumnRule.allow_predicates: bool = False` opts a column into unmasked
    *filtering* (member checks) while masking display — a real compliance
    pattern ("filter by email, never display it").
- Type honesty: masking changes the output type (docs call-out); `"null"`
  built-in is the type-preserving option.
- Interaction rules: `deny` beats `mask` beats `exclude_from_star` when
  multiple rules match one column (most restrictive wins; deterministic).

**New:** `ColumnRule.action="mask"`, `ColumnRule.mask_with`,
`ColumnRule.allow_predicates`, `RewriteKind.COLUMN_MASKED`.

**Tests:** star expansion masks with alias preserved; explicit select masked;
WHERE reference blocked by default / allowed with `allow_predicates`; laundering
through a CTE still masked (lineage reuse); precedence deny>mask; bad template
⇒ `PolicyError`; hash built-in renders per dialect.

## 2.2 Expression RLS rules (P1, 4 d)

**Ask.** Real tenancy is rarely one equality: soft-delete filters, region
lists, effective-dating.

**Design.**
- `RLSRule(table="orders", predicate="region IN :regions AND deleted_at IS NULL")`
  — `predicate` mutually exclusive with `column`. Parsed at construction
  (dialect-aware); named placeholders collected; unknown columns in the
  predicate validated against the target table **at guard build** (same
  eager fail-closed treatment as today's column rules).
- Application (in `rls.apply_rls`): copy the parsed template per occurrence;
  rewrite every unqualified `exp.Column` to `table=alias`; substitute
  placeholders from `params` via `exp.convert` (`list/tuple` → IN expansion);
  any placeholder missing ⇒ `rls_param_missing` (fail closed, existing code).
  Placement reuses `_inject` unchanged (WHERE / LEFT-ON / wrap) — the
  machinery is already predicate-shaped.
- Conflict detection & `require` strategy stay equality-only in this
  iteration: expression rules dedupe by structural equality of the
  substituted predicate; documented limitation.

**New:** `RLSRule.predicate` (constructor path), no new codes.

**Tests:** multi-condition predicate injected per scope/join-aware; list param
→ IN; missing one of two params fails closed; unqualified columns get the
alias; bad predicate column ⇒ `PolicyError` at build; dedupe when identical
predicate already present.

## 2.3 Role-based policy sets (P1, 4 d)

**Design.**
```python
@dataclass
class PolicyOverlay:                       # everything optional
    add_rls: Sequence[RLSRule] = ()
    add_column_rules: Sequence[ColumnRule] = ()
    max_bytes_scanned: int | None = None   # overrides when set
    default_limit: int | None = None
    ...same shape as Policy's scalar knobs...

class PolicySet:
    base: Policy
    roles: Mapping[str, PolicyOverlay]
    def resolve(self, role: str | None) -> Policy   # merged, cached
```
- Merge semantics: rules are **additive** (base ∪ overlay — overlays can only
  tighten, never remove a base rule: monotonic-restriction guarantee, the
  property that makes role review tractable); scalars override when set.
- `SQLGuard(catalog, policy_set)` accepted anywhere a `Policy` is; guard
  resolves+validates every role eagerly at build (config errors surface per
  role, at boot) and caches one merged `Policy` per role. `CatalogIndex` and
  `MappingSchema` are shared — per-role marginal cost ≈ zero.
- `guard.validate(sql, params=…, role="support")`; unknown role ⇒
  `PolicyError`; `role=None` ⇒ base. `role` recorded in `QueryStats` and the
  audit record (2.5). `policy_prompt(role=…)` renders the merged view.

**Tests:** two roles, different visibility on one guard; overlay tightens
budget; unknown role raises; monotonicity (overlay cannot resurrect a
base-denied column); eager per-role config validation; thread-safety across
roles reusing the existing concurrency test pattern.

## 2.4 Complexity budgets (P1, 2 d)

**Outcome (implemented 2026-09-12).** All five optional ceilings are enforced
immediately after the read-only statement gate and before semantic analysis.
Measured counters are exposed in `QueryStats.complexity`; violations identify
the exact metric, measured value, and configured limit. Enforcing mode exits
early, while shadow mode completes the protected pipeline and reports
`would_block=True` for rollout measurement.

**Design.** Cheap counters over the parsed tree, run inside the statement
gate stage (pre-semantics — reject monsters before spending analysis time):
`max_joins` (all scopes), `max_subquery_depth` (scope-tree depth),
`max_ctes`, `max_union_branches`, `max_expression_nodes` (total walk count —
the generic bomb ceiling). All `Policy` fields, all default `None` (off);
docs recommend `joins=8, depth=4, ctes=8, branches=6, nodes=5000` for
chatbot-facing deployments. Violation: `Code.COMPLEXITY_EXCEEDED` (ERROR)
with the measured value and ceiling in `extra`.

**Tests:** each ceiling triggers precisely at N+1; combined with shadow mode;
zero overhead when unset (bench).

## 2.5 Audit & observability hooks (P1, 3 d)

**Design.**
```python
@dataclass(frozen=True)
class AuditRecord:
    at: datetime; dialect: str; role: str | None
    sql_sha256: str                 # hash, not text — PII-safe default
    param_keys: tuple[str, ...]     # names only, never values
    valid: bool; would_block: bool  # would_block ≠ valid under shadow mode
    violation_codes: tuple[str, ...]
    rewrite_kinds: tuple[str, ...]
    tables: tuple[str, ...]
    estimated_bytes: int | None
    duration_ms: float
```
- `Policy.audit_sink: Callable[[AuditRecord], None] | None`. Called in a
  `finally` inside `guard.validate` (records even internal-error paths). Sink
  exceptions are swallowed and logged — auditing must never break serving.
  `Policy.audit_include_sql: bool = False` opts into raw SQL for shops whose
  audit store is itself access-controlled.
- OpenTelemetry as an optional extra `[otel]`: `sqlguard.otel.instrument(guard)`
  wraps `validate` in a span (attributes mirror `AuditRecord`) and maintains
  counters `sqlguard.validations{verdict}` and
  `sqlguard.violations{code}` — no OTel import unless used.

**Tests:** sink receives records for valid/invalid/internal-error; sink raise
is swallowed; no raw SQL by default; duration populated; codes match result.

## 2.6 Driver paramstyle matrix (P2, 2 d)

**Design.** `Policy.paramstyle: "named" | "pyformat" | "qmark" | "numeric" | None`
(None = dialect default: pyformat for postgres/psycopg, named for athena —
today's behavior, unchanged). Implemented as a post-render pass over
`exp.Placeholder` nodes in deterministic tree order; **`qmark`/`numeric`
require ordered values**, so `ValidationResult` gains
`parameters: dict[str, Any] | list[Any] | None` — the exact object to pass to
the driver (`cursor.execute(result.sql, result.parameters)`). In literal mode
it's `None`. Docs table maps psycopg / asyncpg (numeric) / pyathena (pyformat)
to settings.

**Tests:** each style renders + parameters object matches positions; repeated
placeholder (same param twice) duplicates values correctly in ordered styles;
round-trip against sqlite3 (qmark) in-suite as a real-driver smoke test.

## 2.7 Shadow mode (P1, 1 d — ship first)

**Design.** `Policy.enforcement: "block" | "log_only" = "block"`. Under
`log_only`, after the pipeline completes:
- hard-stop classes stay blocking (nothing sane to execute):
  `parse_error`, `multiple_statements`, `disallowed_statement/command`,
  `nested_write`, `select_into`, `locking_clause`, `internal_error`;
- every other ERROR is preserved in `violations` but the result is emitted
  with `valid=True`, rewritten `sql` (rewrites still applied — they're
  safe-direction by construction), and `result.would_block=True`;
- `AuditRecord.would_block` carries the truth for measurement.

This gives deployments a measured would-block rate before flipping to
`"block"` — the #1 adoption objection ("will it break my users?") answered
with data.

**New:** `Policy.enforcement`, `ValidationResult.would_block: bool = False`.

**Tests:** semantic error passes through with `would_block=True` and rewritten
sql; DELETE still blocked in shadow; audit reflects both flags; RLS injection
still applied in shadow (log-only ≠ unprotected).

---

## Definition of done (phase)

- [ ] All seven items merged, each behind an off-by-default policy field
- [ ] Upgrade test: a v0.2 policy object validates byte-identically on v0.4
- [ ] Docs: masking cookbook, role-set example, shadow-mode rollout guide,
      paramstyle driver table, audit schema reference
- [ ] The FastAPI reference service (4.5) consumes audit + roles end-to-end
