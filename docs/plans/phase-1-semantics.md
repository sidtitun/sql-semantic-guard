# Phase 1 — Deeper semantic validation

> Roadmap: [Phase 1](../ROADMAP.md#phase-1--deeper-semantic-validation-the-differentiator) · Target: **v0.3** · Total effort: ~21 days
>
> Goal: catch the remaining *"runs fine, answers the wrong question"* failure
> classes. Everything here rides on richer catalog metadata, so the catalog
> model changes land first and every reflector learns to fill them.

## Sequencing

```
metadata groundwork (FKs, PKs, allowed_values on Table/Column)
  ├──▶ 1.1 FK-aware joins ──▶ (unlocks 3.2 join selectivity later)
  ├──▶ 1.2 aggregation correctness (needs PKs for functional dependency)
  └──▶ 1.5 enum/domain validation
1.3 nested types and 1.6 derived-star are independent; 1.4 last (data work).
```

## Catalog metadata groundwork (prerequisite, 2 d)

```python
@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]           # on this table
    ref_table: str                     # "schema.table" or bare
    ref_columns: tuple[str, ...]

@dataclass
class Table:
    ...existing...
    primary_key: tuple[str, ...] = ()
    foreign_keys: tuple[ForeignKey, ...] = ()

@dataclass
class Column:
    ...existing...
    allowed_values: tuple[str, ...] | None = None
```
- `Catalog.__post_init__` validates: PK/FK columns exist on the table;
  FK `ref_table` resolvable in the catalog (else `CatalogError` — config
  bug, fail at build).
- `from_dict`/`to_dict` round-trip the new keys (`primary_key`,
  `foreign_keys: [{columns, ref_table, ref_columns}]`, column
  `allowed_values`).
- Reflectors: `reflect.py` fills PK/FK from `inspector.get_pk_constraint` /
  `get_foreign_keys`; Glue has no FK concept — left for the dbt importer
  (item 3.5) and hand-written catalogs.
- `CatalogIndex` gains `fk_edges()` → normalized adjacency:
  `{(tableA, tableB): [(colsA, colsB), ...]}` (both directions), cached.

---

## 1.1 FK-aware join validation (P0, 5 d)

**The failure it kills.** `JOIN customers c ON o.id = c.id` — parses, type
checks (bigint=bigint), runs, silently returns garbage. v1's
`suspicious_join` only catches one-sided conditions; this catches *wrong-key*
conditions.

**Design.** Extend `cost.check_joins` (it already walks joins per scope with
prior-alias tracking):
- For each equi-conjunct `a.x = b.y` in a join's ON (split conjuncts with the
  existing `_conjuncts` helper) where both sides resolve to physical tables
  via the scope index:
  - If `fk_edges()` has ≥1 edge between the two tables and **none** of the ON
    equi-pairs matches any declared edge ⇒ `Code.INVALID_JOIN_PATH`
    (WARNING; ERROR under `strict_joins`) with the declared pair in the
    hint: *"declared relationship is orders.customer_id = customers.id"*.
  - If the tables have **no** declared relationship: silent by default;
    under new `Policy.require_declared_join_paths: bool = False` (opt-in for
    curated marts) ⇒ `Code.UNDECLARED_JOIN` WARNING.
- Composite keys: a declared edge matches only if *every* column pair of the
  edge appears among the ON equi-pairs.
- Multi-hop is out of scope for v0.3 (documented): only direct edges are
  judged. (Join-path *suggestion* across hops is a Phase 3 follow-up once the
  FK graph proves reliable.)

**False-positive control.** Analytics legitimately joins on non-FK columns
(e.g. self-join on `customer_id`, date-spine joins). Hence: warning-severity
default, matching-any-edge (not exact-set) semantics, and self-joins exempt
unless the edge itself is self-referential.

**New:** `Code.INVALID_JOIN_PATH`, `Code.UNDECLARED_JOIN`,
`Policy.require_declared_join_paths`.

**Tests** (`test_joins_fk.py`): wrong key flagged w/ correct hint; right key
clean; composite FK partial match flagged; self-join exempt; strict mode
blocks; undeclared-pairs opt-in; CTE-wrapped join resolved through lineage.

## 1.2 Aggregation correctness (P1, 3 d)

**The failure it kills.** `SELECT status, sum(amount) FROM orders` — Postgres
and Trino both reject it at runtime; v1 passes it. Also `WHERE sum(x) > 10`.

**Design.** New pass in `semantics.py` (`check_aggregation(tree, scope_index,
index)`), post-qualification, per SELECT scope:
1. Collect group-by expression keys: canonical `expr.sql(dialect)` strings;
   resolve ordinals (`GROUP BY 1` → selects[0]) and alias references
   (already inlined by qualify's `expand_alias_refs`).
2. Classify each select item: *aggregate* (contains `exp.AggFunc` outside a
   windowed `exp.Window` — window functions are not GROUP BY aggregates),
   *grouped* (canonical sql ∈ group keys, or all its column refs are
   functionally dependent — see 3), else *bare*.
3. **Functional dependency:** if the group keys cover a table's full
   `primary_key`, every column of that table counts as grouped (Postgres
   semantics; Trino rejects — so this relaxation applies only when
   `dialect == "postgres"`).
4. Violations: bare item with grouping present ⇒
   `Code.GROUP_BY_VIOLATION` (ERROR) naming the column and the fix
   ("add to GROUP BY or wrap in an aggregate"); mixed bare+aggregate with
   *no* GROUP BY ⇒ same code; `exp.AggFunc` inside WHERE/JOIN-ON ⇒
   `Code.AGGREGATE_IN_WHERE` (ERROR, hint: "use HAVING"); HAVING/ORDER BY
   bare columns checked with the same classifier.

**New:** `Code.GROUP_BY_VIOLATION`, `Code.AGGREGATE_IN_WHERE`,
`Policy.check_aggregation: bool = True`.

**Tests:** the classic miss; ordinal + alias group keys; PK functional
dependency (postgres pass / athena flag); window function not misclassified;
`HAVING count(*) > 1` clean; aggregate-in-where; CTE scopes independent.

## 1.3 Nested-type modeling (P1, 4 d)

**Today.** `payload.referrer` is *tolerated* via the struct-plausibility
heuristic — `payload.refferer` (typo) passes silently.

**Design.**
- Catalog types like `struct<referrer:string,ua:string>` already parse via
  `exp.DataType.build(..., dialect)`; member defs sit in
  `DataType.expressions` as `exp.ColumnDef`s. Add
  `CatalogIndex.struct_members(column) -> dict[str, exp.DataType] | None`
  (cached; None for non-structs/unparseable).
- In `bind_names`, upgrade `_struct_access_plausible` into
  `_resolve_struct_access`: when `col.table` matches a struct column of a
  local source, validate `col.name` ∈ members ⇒ pass; ∉ ⇒
  `Code.UNKNOWN_COLUMN` with member-list did-you-mean ("payload has fields:
  referrer, ua"). Unparseable/opaque member sets keep today's tolerant path.
- Deeper chains (`a.b.c` arriving as `exp.Dot`): resolve one level at a time
  through nested `STRUCT` member types; bail out (tolerant) at the first
  non-struct/unknown hop. MAP types: any key allowed, value type used for
  type checks.
- Feed member types into `annotate_types` where sqlglot supports nested
  schemas so `payload.ua = 5` becomes a `type_mismatch`.

**Tests** (`test_structs.py`, athena-focused): valid member; typo'd member
with suggestion; two-level nesting; map access tolerated; unparseable struct
falls back silently; member type mismatch flagged.

## 1.4 Function signature checks (P2, 4 d)

**Design.** Data, not code: `src/sqlguard/functions.py` holds
`SIGNATURES: dict[str, list[Sig]]` where
`Sig(min_args, max_args, arg_families: tuple[str|None,...], variadic_family)`,
seeded for ~100 high-traffic functions in three layers: ANSI shared, postgres
extras, trino/athena extras (`date_trunc`, `date_parse`, `split_part`,
`coalesce`, `substr`, `regexp_like`, …). A check pass (in the function gate,
which already visits every `exp.Func`):
- Arity: outside [min,max] ⇒ `Code.FUNCTION_MISUSE` (ERROR) with the correct
  signature in the hint.
- Family check (post-annotate only, reusing `_family` from type checks):
  argument family conflicts ⇒ WARNING (families are approximate).
- `exp.Anonymous` (unknown to the table): skipped — unknown ≠ wrong; the
  allowlist mode already exists for strictness.

**New:** `Code.FUNCTION_MISUSE`, `Policy.check_function_signatures = True`.

**Tests:** `date_trunc(created_at)` (missing unit) flagged with signature
hint; correct calls clean per dialect; unknown UDF untouched; variadic
`coalesce` ok at any arity ≥1.

## 1.5 Enum / domain validation (P1, 2 d)

**Design.** With `Column.allowed_values` in place: during type checks (the
pass already walks EQ/NEQ/IN/Between with annotated columns), when one side
resolves to a column with `allowed_values` and the other is a string literal:
membership check (exact match; `case_insensitive_enums: bool = False` policy
knob) ⇒ `Code.UNKNOWN_VALUE` (ERROR) with `difflib` did-you-mean
("did you mean 'shipped'?") and the value list (≤12 shown). `IN` lists check
every element. Non-equality comparisons (`>`, LIKE) are exempt.

Population paths: hand-written catalogs, dbt `accepted_values` (item 3.5),
and a helper `sqlguard.reflect.profile_allowed_values(engine, table, column,
max_distinct=50)` (explicit opt-in; runs `SELECT DISTINCT … LIMIT 51` and
refuses if >50 values — enums only, not free text).

**New:** `Code.UNKNOWN_VALUE`, `Policy.case_insensitive_enums`.

**Tests:** typo'd status blocked w/ suggestion; valid value clean; IN with one
bad element flagged; case-insensitive mode; non-enum column unaffected;
range predicates exempt.

## 1.6 Derived-star resolution (P2, 3 d)

**Today.** `SELECT sub.bogus FROM (SELECT * FROM orders) sub` passes: the
derived source's outputs are `['*']` ⇒ opaque ⇒ fail-open. This is v1's main
deliberate blind spot.

**Design.** In `build_scope_maps._derived_outputs`, when `named_selects`
contains `*`: compute *effective outputs* by resolving the source scope's own
sources — union the column names of TABLE sources (from the catalog) and
recursively-resolved DERIVED sources; memoize per scope id; recursion capped
at depth 8. Only stays opaque when an underlying source is genuinely opaque
(UNNEST, VALUES, unknown table). `SELECT t.*, 1 AS x` merges star expansion
with explicit names. Ambiguity from star over a join (duplicate output names)
keeps duplicate names in the set — membership checks still work.

This upgrades `bind_names`, column-rule lineage, and RLS conflict scanning
simultaneously (they all consume `Source.outputs`).

**Tests:** the motivating case now fails with did-you-mean; valid member
passes; star-over-join derived table; nested two-level star; UNNEST-backed
derived stays tolerant; recursive CTE doesn't infinite-loop (depth cap).

---

## Definition of done (phase)

- [ ] All six items merged with their test files; suite ≥220 tests
- [ ] Catalog round-trips PK/FK/allowed_values through `to_dict`/`from_dict`
- [ ] SQLAlchemy reflector fills PK/FK against the 0.2 integration database
- [ ] Docs: one page per new `Code` (invalid_join_path, undeclared_join,
      group_by_violation, aggregate_in_where, function_misuse, unknown_value)
- [ ] Benchmark gate still green (new passes ride the shared scope index)
