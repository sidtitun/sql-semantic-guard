"""Safe-direction rewrites: sensitive-column handling and LIMIT enforcement.

Every rewrite here can only *narrow* what a query returns (drop columns, cap
rows) — never widen it. That invariant is what makes automatic rewriting safe
to run on machine-generated SQL.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlglot import exp

from sqlguard.catalog import CatalogIndex, Table, match_table
from sqlguard.policy import ColumnRule, Policy
from sqlguard.semantics import (
    STAR_MARK,
    ScopeInfo,
    resolve_column_origin,
    scope_infos_with_lookup,
)
from sqlguard.violations import (
    Code,
    Rewrite,
    RewriteKind,
    Severity,
    Violation,
)


def _rule_matches(
    rule: ColumnRule,
    index: CatalogIndex,
    table: Table | None,
    column_name: str,
    column_tags: frozenset,
) -> bool:
    import fnmatch

    if not fnmatch.fnmatch(index.normalize(column_name), rule.column.lower()):
        return False
    if rule.tags is not None and not (rule.tags & column_tags):
        return False
    if rule.table != "*":
        if table is None:
            return False  # can't prove the table for a table-specific rule
        if not match_table(rule.table, table, index):
            return False
    elif rule.tags is not None and table is None:
        return False  # tag rules need catalog metadata
    return True


def _matching_rules(
    rules: Sequence[ColumnRule],
    index: CatalogIndex,
    origin: tuple[Table, str] | None,
    column_name: str,
) -> list[ColumnRule]:
    table: Table | None = None
    tags: frozenset = frozenset()
    if origin is not None:
        table = origin[0]
        col = table.column(origin[1])
        if col is not None:
            tags = col.tags
        column_name = origin[1]
    return [r for r in rules if _rule_matches(r, index, table, column_name, tags)]


def apply_column_rules(
    tree: exp.Expression,
    index: CatalogIndex,
    policy: Policy,
    dialect: str,
    scope_index=None,
) -> tuple[list[Violation], list[Rewrite]]:
    """Enforce ColumnRules.

    Order matters: violations for *explicit* references are collected first
    (before any star-expanded projection items are dropped), then matching
    items are removed from star-expanded SELECT lists.

    Dropping projection items does not change source topology, so a shared
    ``scope_index`` stays valid across this pass.
    """
    rules = list(policy.column_rules)
    if not rules:
        return [], []

    violations: list[Violation] = []
    rewrites: list[Rewrite] = []
    if scope_index is not None:
        infos, by_expr = scope_index.infos, scope_index.by_expression
    else:
        infos, by_expr = scope_infos_with_lookup(tree, index)

    # Projection items that came from a ``*`` expansion are exempt from
    # "explicit reference" violations — they get dropped instead.
    star_item_ids: set[int] = set()
    starred_selects: list[tuple[exp.Select, ScopeInfo]] = []
    for info in infos:
        expr = info.expression
        if isinstance(expr, exp.Select) and expr.meta.get(STAR_MARK):
            starred_selects.append((expr, info))
            for item in expr.expressions:
                star_item_ids.add(id(item))

    def item_root(col: exp.Column) -> exp.Expression | None:
        node: exp.Expression | None = col
        while node is not None and not isinstance(node.parent, exp.Select):
            node = node.parent
        return node

    seen: set[tuple[str, str]] = set()
    for info in infos:
        for col in info.scope.columns:
            if isinstance(col.this, exp.Star) or not col.name:
                continue
            root_item = item_root(col)
            if root_item is not None and id(root_item) in star_item_ids and (
                root_item is col
                or (isinstance(root_item, exp.Alias) and root_item.this is col)
            ):
                continue  # star-derived; handled by the drop pass
            origin = resolve_column_origin(by_expr, info, col, index)
            matched = _matching_rules(rules, index, origin, col.name)
            denies = [r for r in matched if r.action == "deny"]
            if denies:
                table_name = origin[0].display_name if origin else None
                key = (table_name or "", index.normalize(col.name))
                if key in seen:
                    continue
                seen.add(key)
                reason = denies[0].reason or "restricted by policy"
                violations.append(
                    Violation(
                        Code.COLUMN_DENIED,
                        Severity.ERROR,
                        f"Column {col.name!r}"
                        + (f" of table {table_name!r}" if table_name else "")
                        + f" may not be referenced: {reason}",
                        table=table_name,
                        column=col.name,
                    )
                )

    # Drop matching items from star-expanded select lists.
    for select, info in starred_selects:
        kept: list[exp.Expression] = []
        dropped: list[str] = []
        for item in select.expressions:
            inner = item.this if isinstance(item, exp.Alias) else item
            if isinstance(inner, exp.Column) and inner.name:
                origin = resolve_column_origin(by_expr, info, inner, index)
                if _matching_rules(rules, index, origin, inner.name):
                    dropped.append(inner.name)
                    continue
            kept.append(item)
        if not dropped:
            continue
        if not kept:
            violations.append(
                Violation(
                    Code.EMPTY_SELECT,
                    Severity.ERROR,
                    "SELECT * would expand to zero columns after removing "
                    f"restricted columns ({', '.join(dropped)})",
                    hint="Select specific permitted columns instead of *.",
                )
            )
            continue
        select.set("expressions", kept)
        rewrites.append(
            Rewrite(
                RewriteKind.SENSITIVE_COLUMN_EXCLUDED,
                f"Removed restricted column(s) from * expansion: {', '.join(sorted(set(dropped)))}",
                extra={"columns": sorted(set(dropped))},
            )
        )
    return violations, rewrites


def enforce_limit(
    root: exp.Expression, policy: Policy
) -> tuple[exp.Expression, list[Rewrite]]:
    """Add a LIMIT when missing; clamp it when above ``max_limit``.

    Applies to the outermost query only — inner limits are the author's
    business, and the outer cap bounds the result size regardless.
    """
    rewrites: list[Rewrite] = []
    if not isinstance(root, exp.Query):
        return root, rewrites

    existing = root.args.get("limit")
    current: int | None = None
    if existing is not None:
        node = None
        if isinstance(existing, exp.Limit):
            node = existing.expression
        elif isinstance(existing, exp.Fetch):
            node = existing.args.get("count")
        if isinstance(node, exp.Literal) and node.is_int:
            current = int(node.name)
        else:
            return root, rewrites  # non-literal limit: leave untouched

    if current is None:
        if policy.default_limit is not None:
            root = root.limit(policy.default_limit, copy=False)
            rewrites.append(
                Rewrite(
                    RewriteKind.LIMIT_ADDED,
                    f"Added LIMIT {policy.default_limit} (no row limit was present)",
                    extra={"limit": policy.default_limit},
                )
            )
        return root, rewrites

    if policy.max_limit is not None and current > policy.max_limit:
        root = root.limit(policy.max_limit, copy=False)
        rewrites.append(
            Rewrite(
                RewriteKind.LIMIT_CLAMPED,
                f"Clamped LIMIT {current} to the maximum of {policy.max_limit}",
                extra={"from": current, "to": policy.max_limit},
            )
        )
    return root, rewrites
