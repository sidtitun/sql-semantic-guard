"""Safe-direction rewrites: sensitive-column handling and LIMIT enforcement.

Every rewrite here can only *narrow* what a query returns (drop columns, cap
rows) — never widen it. That invariant is what makes automatic rewriting safe
to run on machine-generated SQL.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from sqlglot import exp, parse_one
from sqlglot.optimizer.scope import ScopeType

from sqlguard.catalog import CatalogIndex, Table, match_table
from sqlguard.policy import ColumnRule, Policy
from sqlguard.semantics import (
    EXPLICIT_PROJECTION_MARK,
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

if TYPE_CHECKING:
    from sqlguard.scopeindex import ScopeIndex


def _rule_matches(
    rule: ColumnRule,
    index: CatalogIndex,
    table: Table | None,
    column_name: str,
    column_tags: frozenset[str],
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
    tags: frozenset[str] = frozenset()
    if origin is not None:
        table = origin[0]
        col = table.column(origin[1])
        if col is not None:
            tags = col.tags
        column_name = origin[1]
    return [r for r in rules if _rule_matches(r, index, table, column_name, tags)]


def _effective_rule(rules: Sequence[ColumnRule]) -> ColumnRule | None:
    priority = {"exclude_from_star": 1, "mask": 2, "deny": 3}
    return max(rules, key=lambda rule: priority[rule.action], default=None)


def _is_explicit_projection(item: exp.Expression) -> bool:
    if item.meta.get(EXPLICIT_PROJECTION_MARK):
        return True
    return isinstance(item, exp.Alias) and bool(
        item.this.meta.get(EXPLICIT_PROJECTION_MARK)
    )


def _mask_expression(
    rule: ColumnRule,
    column: exp.Column,
    origin: tuple[Table, str] | None,
    index: CatalogIndex,
    dialect: str,
) -> exp.Expression:
    assert rule.mask_with is not None
    if rule.mask_with == "null":
        if origin is not None:
            catalog_column = origin[0].column(origin[1])
            dtype = index.data_type(catalog_column) if catalog_column is not None else None
            if dtype is not None:
                return exp.Cast(this=exp.Null(), to=dtype.copy())
        return exp.Null()
    if rule.mask_with == "redact":
        return exp.Literal.string("***")

    sentinel = "__sqlguard_mask_column__"
    template: exp.Expression
    if rule.mask_with == "hash":
        if dialect == "athena":
            sql = f"TO_HEX(MD5(TO_UTF8(CAST({sentinel} AS VARCHAR))))"
        else:
            sql = f"MD5(CAST({sentinel} AS TEXT))"
        template = cast(exp.Expression, parse_one(sql, read=dialect))
    else:
        custom_template = rule.mask_template
        assert custom_template is not None
        template = custom_template

    return template.transform(
        lambda node: column.copy()
        if isinstance(node, exp.Column) and node.name == sentinel
        else node,
        copy=False,
    )


def apply_column_rules(
    tree: exp.Expression,
    index: CatalogIndex,
    policy: Policy,
    dialect: str,
    scope_index: ScopeIndex | None = None,
) -> tuple[list[Violation], list[Rewrite]]:
    """Enforce deny, mask, and star-exclusion column rules."""
    rules = list(policy.column_rules)
    if not rules:
        return [], []

    violations: list[Violation] = []
    rewrites: list[Rewrite] = []
    if scope_index is not None:
        infos, by_expr = scope_index.infos, scope_index.by_expression
    else:
        infos, by_expr = scope_infos_with_lookup(tree, index)

    starred_selects: list[tuple[exp.Select, ScopeInfo]] = []
    for info in infos:
        expr = info.expression
        if isinstance(expr, exp.Select) and expr.meta.get(STAR_MARK):
            starred_selects.append((expr, info))

    def item_root(col: exp.Column) -> tuple[exp.Select | None, exp.Expression | None]:
        node: exp.Expression | None = col
        while node is not None and not isinstance(node.parent, exp.Select):
            node = node.parent  # type: ignore[assignment]
        parent = node.parent if node is not None else None
        return (parent if isinstance(parent, exp.Select) else None), node

    seen: set[tuple[str, str]] = set()
    replacements: list[
        tuple[exp.Column, ColumnRule, tuple[Table, str] | None, exp.Expression]
    ] = []
    drops: dict[int, set[int]] = {}
    masked_items: set[tuple[int, str, str]] = set()

    for info in infos:
        for col in info.scope.columns:
            if isinstance(col.this, exp.Star) or not col.name:
                continue
            select, root_item = item_root(col)
            is_projection = bool(
                select is not None
                and root_item is not None
                and any(item is root_item for item in select.expressions)
            )
            star_derived = bool(
                is_projection
                and select is not None
                and select.meta.get(STAR_MARK)
                and root_item is not None
                and not _is_explicit_projection(root_item)
            )
            origin = resolve_column_origin(by_expr, info, col, index)
            matched = _matching_rules(rules, index, origin, col.name)
            rule = _effective_rule(matched)
            if rule is None:
                continue

            if star_derived and rule.action in ("deny", "exclude_from_star"):
                assert select is not None and root_item is not None
                drops.setdefault(id(select), set()).add(id(root_item))
                continue

            if rule.action == "deny":
                table_name = origin[0].display_name if origin else None
                key = (table_name or "", index.normalize(col.name))
                if key in seen:
                    continue
                seen.add(key)
                reason = rule.reason or "restricted by policy"
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

            if rule.action != "mask":
                continue
            if is_projection and root_item is not None:
                direct_passthrough = root_item is col or (
                    isinstance(root_item, exp.Alias) and root_item.this is col
                )
                intermediate_scope = info.scope.scope_type in (
                    ScopeType.CTE,
                    ScopeType.DERIVED_TABLE,
                )
                # Preserve a raw pass-through inside an intermediate relation
                # so its final consumer can filter correctly and mask once at
                # the result boundary. Expressions and scalar subqueries are
                # masked in place because lineage cannot safely defer them.
                if intermediate_scope and direct_passthrough:
                    continue
                replacements.append((col, rule, origin, root_item))
                continue
            mask_rules = [matched_rule for matched_rule in matched if matched_rule.action == "mask"]
            if all(mask_rule.allow_predicates for mask_rule in mask_rules):
                continue
            table_name = origin[0].display_name if origin else None
            key = (table_name or "", index.normalize(col.name))
            if key in seen:
                continue
            seen.add(key)
            reason = rule.reason or "masked columns may not be used in predicates"
            violations.append(
                Violation(
                    Code.COLUMN_DENIED,
                    Severity.ERROR,
                    f"Column {col.name!r}"
                    + (f" of table {table_name!r}" if table_name else "")
                    + f" may not be referenced outside the select list: {reason}",
                    table=table_name,
                    column=col.name,
                )
            )

    # Remove denied/excluded generated star items before replacing projected
    # columns, while the original projection identity is still available.
    for select, _info in starred_selects:
        kept: list[exp.Expression] = []
        dropped: list[str] = []
        for item in select.expressions:
            if id(item) in drops.get(id(select), set()):
                inner = item.this if isinstance(item, exp.Alias) else item
                dropped.append(inner.name if isinstance(inner, exp.Column) else item.alias_or_name)
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

    for column, rule, origin, root_item in replacements:
        if column.parent is None:
            continue  # its projection was removed by a stronger rule
        masked = _mask_expression(rule, column, origin, index, dialect)
        column.replace(masked)
        table_name = origin[0].display_name if origin else ""
        column_name = origin[1] if origin else column.name
        rewrite_key = (id(root_item), table_name, index.normalize(column_name))
        if rewrite_key in masked_items:
            continue
        masked_items.add(rewrite_key)
        rewrites.append(
            Rewrite(
                RewriteKind.COLUMN_MASKED,
                f"Masked column {column_name!r}"
                + (f" of table {table_name!r}" if table_name else ""),
                table=table_name or None,
                column=column_name,
                extra={"mask": rule.mask_with},
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
