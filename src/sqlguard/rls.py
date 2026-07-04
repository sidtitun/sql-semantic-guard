"""Row-level security: verify or inject tenant/RBAC predicates.

The invariant: no row of a governed table may flow into the result unless the
governing predicate was applied *at that table reference*. That means every
scope — CTEs, subqueries, self-joins — gets its own predicate, and placement
is join-aware:

- table in FROM, or INNER/comma-joined  -> conjunct in that scope's WHERE
- LEFT-joined table                     -> conjunct in the join's ON
  (a WHERE predicate on the nullable side would silently turn the LEFT JOIN
  into an INNER join)
- RIGHT/FULL-joined or USING-joined     -> the table reference is wrapped in
  a filtered subquery (the only placement that is always correct)

``rls_strategy="subquery"`` forces the wrap everywhere; ``"require"`` injects
nothing and instead *verifies* the predicate is already present with the right
value. All strategies detect conflicting tenant literals (an LLM trying —
or being prompt-injected — to read another tenant's rows).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlglot import exp

from sqlguard.catalog import CatalogIndex, Table, match_table
from sqlguard.policy import Policy, RLSRule
from sqlguard.semantics import ScopeInfo, SourceKind, build_scope_maps
from sqlguard.violations import (
    Code,
    Rewrite,
    RewriteKind,
    Severity,
    Violation,
)

_CONFLICT_SEVERITY = {
    "error": Severity.ERROR,
    "warn": Severity.WARNING,
}


def _conjuncts(condition: exp.Expression | None) -> list[exp.Expression]:
    if condition is None:
        return []
    if isinstance(condition, exp.And):
        return list(condition.flatten())
    return [condition]


def _scope_predicate_pool(select: exp.Select) -> list[exp.Expression]:
    pool: list[exp.Expression] = []
    where = select.args.get("where")
    if where is not None:
        pool.extend(_conjuncts(where.this))
    for join in select.args.get("joins") or []:
        pool.extend(_conjuncts(join.args.get("on")))
    return pool


def _literal_equal(a: exp.Expression, b: exp.Expression) -> bool:
    if isinstance(a, exp.Literal) and isinstance(b, exp.Literal):
        if a.is_string != b.is_string:
            return str(a.name) == str(b.name)  # tolerate 42 vs '42'
        return a == b
    return a == b


def _column_matches(
    node: exp.Expression,
    index: CatalogIndex,
    column_norm: str,
    alias_norm: str,
    sole_source: bool,
) -> bool:
    if not isinstance(node, exp.Column) or isinstance(node.this, exp.Star):
        return False
    if index.normalize(node.name) != column_norm:
        return False
    if node.table:
        return index.normalize(node.table) == alias_norm
    return sole_source


def _convert_value(value: Any) -> exp.Expression | None:
    try:
        if isinstance(value, (list, tuple, set, frozenset)):
            items = [exp.convert(v) for v in value]
            if not items:
                return None
            return items  # type: ignore[return-value]  # marker for IN
        return exp.convert(value)
    except Exception:
        return None


def _build_predicate(
    alias: str, column: str, value_expr: Any
) -> exp.Expression:
    col_ref = exp.column(column, table=alias)
    if isinstance(value_expr, list):
        return exp.In(this=col_ref, expressions=[v.copy() for v in value_expr])
    return col_ref.eq(value_expr.copy() if isinstance(value_expr, exp.Expression) else value_expr)


def apply_rls(
    tree: exp.Expression,
    index: CatalogIndex,
    policy: Policy,
    params: Mapping[str, Any],
    dialect: str,
) -> tuple[list[Violation], list[Rewrite]]:
    if not policy.rls:
        return [], []

    violations: list[Violation] = []
    rewrites: list[Rewrite] = []
    infos, _, _ = build_scope_maps(tree, index)

    # (info, alias, source-node, [predicates]) queued for injection
    injections: list[tuple[ScopeInfo, str, exp.Expression, list[exp.Expression]]] = []
    config_reported = set()

    for info in infos:
        if not info.sources:
            continue
        select = info.expression
        pool = _scope_predicate_pool(select) if isinstance(select, exp.Select) else []
        sole_source = len(info.sources) == 1

        for alias, src in info.sources.items():
            if src.kind != SourceKind.TABLE or src.table is None or src.node is None:
                continue
            table = src.table
            preds: list[exp.Expression] = []
            for rule in policy.rls:
                if not match_table(rule.table, table, index, rule.schema):
                    continue
                column_norm = index.normalize(rule.column)
                if table.column(rule.column) is None:
                    if rule.on_missing_column == "skip":
                        continue
                    key = ("missing_col", table.display_name, column_norm)
                    if key not in config_reported:
                        config_reported.add(key)
                        violations.append(
                            Violation(
                                Code.RLS_CONFIG_ERROR,
                                Severity.ERROR,
                                f"RLS rule targets {table.display_name!r}.{rule.column!r} "
                                "but the table has no such column",
                                table=table.display_name,
                                column=rule.column,
                                hint="Fix the RLSRule or set on_missing_column='skip'.",
                            )
                        )
                    continue

                have_value = rule.param_name in params and params[rule.param_name] is not None
                value_expr: Any = None
                if have_value:
                    value_expr = _convert_value(params[rule.param_name])
                    if value_expr is None:
                        key = ("bad_value", table.display_name, column_norm)
                        if key not in config_reported:
                            config_reported.add(key)
                            violations.append(
                                Violation(
                                    Code.RLS_CONFIG_ERROR,
                                    Severity.ERROR,
                                    f"RLS parameter {rule.param_name!r} has an unsupported or "
                                    f"empty value ({type(params[rule.param_name]).__name__})",
                                    table=table.display_name,
                                    column=rule.column,
                                )
                            )
                        continue

                if not have_value and not policy.rls_parameterize:
                    key = ("missing_param", rule.param_name)
                    if key not in config_reported:
                        config_reported.add(key)
                        violations.append(
                            Violation(
                                Code.RLS_PARAM_MISSING,
                                Severity.ERROR,
                                f"Missing RLS parameter {rule.param_name!r}; refusing to run "
                                f"an unfiltered query against {table.display_name!r}",
                                table=table.display_name,
                                column=rule.column,
                                hint=f"Pass params={{{rule.param_name!r}: <value>}} to validate().",
                            )
                        )
                    continue

                # Scan existing predicates: already filtered? conflicting literal?
                expected = value_expr if isinstance(value_expr, exp.Expression) else None
                already_present = False
                for conj in pool:
                    if isinstance(conj, exp.EQ):
                        left, right = conj.this, conj.expression
                        col_side, other = (left, right) if _column_matches(
                            left, index, column_norm, alias, sole_source
                        ) else (right, left) if _column_matches(
                            right, index, column_norm, alias, sole_source
                        ) else (None, None)
                        if col_side is None:
                            continue
                        if isinstance(other, exp.Placeholder):
                            if other.name == rule.param_name:
                                already_present = True
                            continue
                        if not isinstance(other, (exp.Literal, exp.Cast, exp.Boolean)):
                            continue
                        if expected is not None and _literal_equal(other, expected):
                            already_present = True
                        elif expected is not None:
                            _report_conflict(
                                violations, policy, table, rule, other, expected, dialect
                            )
                    elif isinstance(conj, exp.In) and _column_matches(
                        conj.this, index, column_norm, alias, sole_source
                    ):
                        items = list(conj.expressions)
                        if items and all(isinstance(e, (exp.Literal, exp.Cast, exp.Boolean)) for e in items):
                            if expected is not None:
                                foreign = [e for e in items if not _literal_equal(e, expected)]
                                if not foreign and len(items) >= 1:
                                    already_present = True
                                else:
                                    _report_conflict(
                                        violations, policy, table, rule,
                                        foreign[0] if foreign else items[0], expected, dialect,
                                    )

                if policy.rls_strategy == "require":
                    if already_present:
                        continue
                    want = (
                        f"{alias}.{rule.column} = "
                        + (expected.sql(dialect=dialect) if expected is not None else f":{rule.param_name}")
                    )
                    violations.append(
                        Violation(
                            Code.MISSING_TENANT_FILTER,
                            Severity.ERROR,
                            f"Query does not filter {table.display_name!r} by "
                            f"{rule.column!r} as required",
                            table=table.display_name,
                            column=rule.column,
                            hint=f"Add the condition: {want}",
                        )
                    )
                    continue

                if already_present:
                    rewrites.append(
                        Rewrite(
                            RewriteKind.RLS_FILTER_PRESENT,
                            f"Row filter on {alias}.{rule.column} already present; not duplicated",
                            table=table.display_name,
                            column=rule.column,
                        )
                    )
                    continue

                if not have_value and policy.rls_parameterize:
                    value_expr = exp.Placeholder(this=rule.param_name)
                pred = _build_predicate(alias, rule.column, value_expr)
                preds.append(pred)
                rewrites.append(
                    Rewrite(
                        RewriteKind.RLS_FILTER_ADDED,
                        f"Applied row-level filter: {pred.sql(dialect=dialect)}",
                        table=table.display_name,
                        column=rule.column,
                        extra={"predicate": pred.sql(dialect=dialect)},
                    )
                )
            if preds:
                injections.append((info, alias, src.node, preds))

    for info, alias, node, preds in injections:
        _inject(info, alias, node, preds, policy)

    return violations, rewrites


def _report_conflict(
    violations: list[Violation],
    policy: Policy,
    table: Table,
    rule: RLSRule,
    found: exp.Expression,
    expected: exp.Expression,
    dialect: str,
) -> None:
    if policy.on_conflicting_tenant_filter == "ignore":
        return
    violations.append(
        Violation(
            Code.TENANT_FILTER_CONFLICT,
            _CONFLICT_SEVERITY[policy.on_conflicting_tenant_filter],
            f"Query filters {table.display_name!r}.{rule.column!r} to "
            f"{found.sql(dialect=dialect)} but the caller context requires "
            f"{expected.sql(dialect=dialect)}",
            table=table.display_name,
            column=rule.column,
            hint="Remove that filter; row-level security is applied automatically.",
        )
    )


def _inject(
    info: ScopeInfo,
    alias: str,
    node: exp.Expression,
    preds: list[exp.Expression],
    policy: Policy,
) -> None:
    select = info.expression

    def wrap() -> None:
        _wrap_in_subquery(node, alias, preds)

    if policy.rls_strategy == "subquery" or not isinstance(select, exp.Select):
        wrap()
        return

    parent: exp.Expression | None = node.parent
    while parent is not None and not isinstance(parent, (exp.From, exp.Join)):
        parent = parent.parent

    if isinstance(parent, exp.Join):
        side = (parent.side or "").upper()
        if parent.args.get("using") or side in ("RIGHT", "FULL"):
            wrap()
            return
        if side == "LEFT":
            on = parent.args.get("on")
            if on is None:
                wrap()
            else:
                parent.set("on", exp.and_(on, *preds))
            return
        # INNER / CROSS / comma join: WHERE is equivalent and more readable
        select.where(*preds, copy=False)
        return

    # plain FROM source
    select.where(*preds, copy=False)


def _wrap_in_subquery(
    node: exp.Expression, alias: str, preds: Sequence[exp.Expression]
) -> None:
    inner_table = node.copy()
    inner_table.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
    inner = (
        exp.Select()
        .select(exp.Star())
        .from_(inner_table)
        .where(*[p.copy() for p in preds])
    )
    sub = exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias)))
    node.replace(sub)
