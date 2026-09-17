"""Catalog-aware semantic validation: name binding, scopes, and types.

This is the layer a plain AST parser can't give you. Instead of asking "is
this valid SQL?", it asks "does this SQL make sense against *this* warehouse?"

Design note: sqlglot's ``qualify()`` raises on the *first* unknown column, but
an LLM repair loop wants *every* problem in one round trip. So we do our own
scope-aware binding pass (collecting all violations, with did-you-mean hints)
and only then hand the tree to ``qualify()`` for star expansion and canonical
qualification.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, cast

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, ScopeType, traverse_scope

from sqlguard.catalog import CatalogIndex, Column, Table
from sqlguard.policy import Policy
from sqlguard.violations import Code, Severity, Violation

if TYPE_CHECKING:
    from sqlguard.scopeindex import ScopeIndex

STAR_MARK = "sqlguard_star"

# Column types through which dotted access (``alias.field``) is plausible even
# when ``alias`` is not a table: struct/map/json members, or unknown types.
_STRUCTY_TYPES = {
    exp.DataType.Type.STRUCT,
    exp.DataType.Type.OBJECT,
    exp.DataType.Type.MAP,
    exp.DataType.Type.JSON,
    exp.DataType.Type.JSONB,
    exp.DataType.Type.VARIANT,
    exp.DataType.Type.SUPER,
}

_ISO_DATEISH = re.compile(
    r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?)?$"
)
_NUMERICISH = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


class SourceKind(Enum):
    TABLE = "table"  # physical table resolved in the catalog
    DERIVED = "derived"  # subquery/CTE with known output names
    DERIVED_OPAQUE = "derived_opaque"  # subquery/CTE with unknowable outputs
    UNKNOWN_TABLE = "unknown_table"  # physical reference we couldn't resolve
    OTHER = "other"  # UNNEST, table functions, VALUES, ...


@dataclass
class Source:
    alias: str
    kind: SourceKind
    node: exp.Expression | None = None  # tree node (exp.Table when physical)
    table: Table | None = None
    outputs: set[str] | None = None
    scope: Scope | None = None  # source scope for derived tables


@dataclass
class ScopeInfo:
    scope: Scope
    sources: dict[str, Source] = field(default_factory=dict)
    parent: ScopeInfo | None = None
    can_correlate: bool = False

    @property
    def expression(self) -> exp.Expression:
        return cast(exp.Expression, self.scope.expression)


def _declared_derived_outputs(
    source_scope: Scope, index: CatalogIndex
) -> set[str] | None:
    """Return explicit output names, leaving star-containing scopes unresolved."""
    try:
        expression = cast(exp.Expression, source_scope.expression)
        names = getattr(expression, "named_selects", None)
    except Exception:
        return None
    if not names or any(n == "*" for n in names):
        return None
    return {index.normalize(n) for n in names}


def _resolve_derived_outputs(
    infos: Sequence[ScopeInfo], by_scope_id: dict[int, ScopeInfo], index: CatalogIndex
) -> None:
    """Resolve stars in derived sources through known physical/derived inputs."""
    memo: dict[int, set[str] | None] = {}

    def source_outputs(
        source: Source, depth: int, visiting: set[int]
    ) -> set[str] | None:
        if source.kind == SourceKind.TABLE and source.table is not None:
            return {index.normalize(column.name) for column in source.table.columns}
        if source.scope is not None:
            return scope_outputs(source.scope, depth + 1, visiting)
        return None

    def scope_outputs(scope: Scope, depth: int, visiting: set[int]) -> set[str] | None:
        scope_id = id(scope)
        if scope_id in memo:
            return memo[scope_id]
        if depth > 8 or scope_id in visiting:
            return None
        info = by_scope_id.get(scope_id)
        if info is None:
            return None
        expression = info.expression
        declared = _declared_derived_outputs(scope, index)
        if declared is not None:
            memo[scope_id] = declared
            return declared
        if not isinstance(expression, exp.Select):
            memo[scope_id] = None
            return None

        visiting.add(scope_id)
        outputs: set[str] = set()
        try:
            for item in expression.expressions:
                star: exp.Star | None = None
                selected_sources: list[Source]
                if isinstance(item, exp.Star):
                    star = item
                    selected_sources = list(info.sources.values())
                elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                    star = item.this
                    selected = info.sources.get(index.normalize(item.table))
                    if selected is None:
                        return None
                    selected_sources = [selected]
                else:
                    name = item.alias_or_name
                    if not name:
                        return None
                    outputs.add(index.normalize(name))
                    continue

                # Star modifiers that rename/replace/filter outputs are left
                # opaque until their exact engine semantics are modeled.
                if any(star.args.get(key) for key in ("replace", "rename", "ilike")):
                    return None
                expanded: set[str] = set()
                for source in selected_sources:
                    names = source_outputs(source, depth, visiting)
                    if names is None:
                        return None
                    expanded.update(names)
                excluded = star.args.get("except_") or []
                expanded.difference_update(
                    index.normalize(exclusion.name)
                    for exclusion in excluded
                    if isinstance(exclusion, exp.Expression) and exclusion.name
                )
                outputs.update(expanded)
        finally:
            visiting.remove(scope_id)
        memo[scope_id] = outputs
        return outputs

    for info in infos:
        for source in info.sources.values():
            if source.scope is None:
                continue
            source.outputs = scope_outputs(source.scope, 0, set())
            source.kind = (
                SourceKind.DERIVED
                if source.outputs is not None
                else SourceKind.DERIVED_OPAQUE
            )


def build_scope_maps(
    tree: exp.Expression, index: CatalogIndex
) -> tuple[list[ScopeInfo], list[Violation], list[str]]:
    """Traverse all scopes, resolving every FROM/JOIN source.

    Returns (scope infos, unknown-table violations, resolved table display names).
    """
    violations: list[Violation] = []
    infos: list[ScopeInfo] = []
    by_scope_id: dict[int, ScopeInfo] = {}
    reported_unknown: set[tuple[str, str]] = set()
    resolved_tables: dict[tuple[str | None, str], Table] = {}

    scopes = traverse_scope(tree)
    for scope in scopes:
        info = ScopeInfo(scope=scope)
        info.can_correlate = scope.scope_type == ScopeType.SUBQUERY or (
            scope.scope_type == ScopeType.DERIVED_TABLE
            and scope.expression.find_ancestor(exp.Lateral) is not None
        )
        try:
            selected = scope.selected_sources
        except Exception:  # pragma: no cover - defensive against exotic SQL
            selected = {}
        for alias, (node, source) in selected.items():
            alias_norm = index.normalize(alias)
            if isinstance(source, exp.Table):
                table, hint = index.resolve(source.name, source.db)
                if table is not None:
                    info.sources[alias_norm] = Source(
                        alias=alias_norm,
                        kind=SourceKind.TABLE,
                        node=cast(exp.Expression, node),
                        table=table,
                    )
                    key = (
                        index.normalize(table.schema) if table.schema else None,
                        index.normalize(table.name),
                    )
                    resolved_tables[key] = table
                else:
                    display = f"{source.db}.{source.name}" if source.db else source.name
                    rkey = (index.normalize(source.db or ""), index.normalize(source.name))
                    if rkey not in reported_unknown:
                        reported_unknown.add(rkey)
                        violations.append(
                            Violation(
                                Code.UNKNOWN_TABLE,
                                Severity.ERROR,
                                f"Table {display!r} does not exist in the catalog",
                                table=display,
                                hint=hint,
                            )
                        )
                    info.sources[alias_norm] = Source(
                        alias=alias_norm,
                        kind=SourceKind.UNKNOWN_TABLE,
                        node=cast(exp.Expression, node),
                    )
            elif isinstance(source, Scope):
                outputs = _declared_derived_outputs(source, index)
                kind = SourceKind.DERIVED if outputs is not None else SourceKind.DERIVED_OPAQUE
                info.sources[alias_norm] = Source(
                    alias=alias_norm,
                    kind=kind,
                    node=cast(exp.Expression, node),
                    outputs=outputs,
                    scope=source,
                )
            else:
                info.sources[alias_norm] = Source(
                    alias=alias_norm,
                    kind=SourceKind.OTHER,
                    node=cast(exp.Expression, node),
                )
        by_scope_id[id(scope)] = info
        infos.append(info)

    for info in infos:
        parent = getattr(info.scope, "parent", None)
        if parent is not None:
            info.parent = by_scope_id.get(id(parent))

    _resolve_derived_outputs(infos, by_scope_id, index)

    table_names = sorted(t.display_name for t in resolved_tables.values())
    return infos, violations, table_names


def _lookup_alias(info: ScopeInfo, alias_norm: str) -> Source | None:
    cur: ScopeInfo | None = info
    while cur is not None:
        src = cur.sources.get(alias_norm)
        if src is not None:
            return src
        if not cur.can_correlate:
            return None
        cur = cur.parent
    return None


def _own_output_names(info: ScopeInfo) -> set[str]:
    """Names a bare column may legally refer to as the query's own output.

    For a SELECT scope these are only *explicit* aliases — a bare passthrough
    column name in the projection resolves (or fails) against the sources, so
    treating it as "its own output" would mask unknown-column errors. For set
    operations (UNION ...), ORDER BY may reference any output name; operand
    scopes already validated those names against real sources.
    """
    expr = info.expression
    if isinstance(expr, exp.SetOperation):
        try:
            return {n.lower() for n in expr.named_selects if n != "*"}
        except Exception:
            return set()
    if isinstance(expr, exp.Select):
        return {
            e.alias.lower()
            for e in expr.selects
            if isinstance(e, exp.Alias) and e.alias
        }
    return set()


def _nested_member_hint(index: CatalogIndex, member: str, members: dict[str, exp.DataType]) -> str:
    matches = difflib.get_close_matches(index.normalize(member), list(members), n=3, cutoff=0.5)
    fields = ", ".join(sorted(members)[:12])
    suggestion = f" Did you mean: {', '.join(matches)}?" if matches else ""
    return f"Available fields: {fields}.{suggestion}".rstrip()


def _map_value_type(dtype: exp.DataType) -> exp.DataType | None:
    if dtype.this != exp.DataType.Type.MAP or len(dtype.expressions) < 2:
        return None
    value = dtype.expressions[1]
    return value if isinstance(value, exp.DataType) else None


def _array_element_type(dtype: exp.DataType) -> exp.DataType | None:
    if dtype.this != exp.DataType.Type.ARRAY or not dtype.expressions:
        return None
    element = dtype.expressions[0]
    return element if isinstance(element, exp.DataType) else None


def _validate_type_path(
    dtype: exp.DataType | None,
    steps: Sequence[tuple[str, str | None]],
    index: CatalogIndex,
    display: str,
    root_members: dict[str, exp.DataType] | None = None,
) -> Violation | None:
    current = dtype
    cached_members = root_members
    for operation, value in steps:
        if current is None or current.this in {
            exp.DataType.Type.UNKNOWN,
            exp.DataType.Type.OBJECT,
            exp.DataType.Type.JSON,
            exp.DataType.Type.JSONB,
            exp.DataType.Type.VARIANT,
            exp.DataType.Type.SUPER,
        }:
            return None
        if operation == "index":
            current = _array_element_type(current) or _map_value_type(current)
            cached_members = None
            if current is None:
                return None
            continue
        assert value is not None
        if current.this == exp.DataType.Type.MAP:
            current = _map_value_type(current)
            continue
        members = cached_members or index.data_type_members(current)
        cached_members = None
        if members is None:
            return Violation(
                Code.UNKNOWN_COLUMN,
                Severity.ERROR,
                f"Cannot access field {value!r} on non-STRUCT expression {display!r}",
                column=display,
                hint="Remove the field access or correct the catalog type.",
            )
        member_norm = index.normalize(value)
        if member_norm not in members:
            return Violation(
                Code.UNKNOWN_COLUMN,
                Severity.ERROR,
                f"Nested field {value!r} does not exist in {display!r}",
                column=display,
                hint=_nested_member_hint(index, value, members),
            )
        current = members[member_norm]
    return None


def _physical_column_matches(
    info: ScopeInfo, index: CatalogIndex, name: str
) -> tuple[list[tuple[Table, Column]], bool]:
    matches: list[tuple[Table, Column]] = []
    opaque = False
    for source in info.sources.values():
        if source.kind == SourceKind.TABLE and source.table is not None:
            column = source.table.column(name)
            if column is not None:
                matches.append((source.table, column))
        elif source.kind == SourceKind.DERIVED and source.outputs:
            opaque = opaque or index.normalize(name) in source.outputs
        else:
            opaque = True
    return matches, opaque


def _resolve_struct_column(
    info: ScopeInfo, index: CatalogIndex, column: exp.Column
) -> tuple[bool, Violation | None]:
    parts = [part.name for part in column.parts if part.name]
    if len(parts) < 2:
        return False, None
    matches, opaque = _physical_column_matches(info, index, parts[0])
    if len(matches) > 1:
        return True, Violation(
            Code.AMBIGUOUS_COLUMN,
            Severity.ERROR,
            f"Nested column base {parts[0]!r} is ambiguous",
            column=parts[0],
            hint="Qualify the source table before accessing nested fields.",
        )
    if len(matches) == 1 and not opaque:
        table, catalog_column = matches[0]
        dtype = index.data_type(catalog_column)
        steps = [("field", part) for part in parts[1:]]
        violation = _validate_type_path(
            dtype,
            steps,
            index,
            ".".join(parts),
            root_members=index.struct_members(catalog_column),
        )
        if violation is not None:
            violation.table = table.display_name
        return True, violation
    if matches or opaque:
        return True, None
    return False, None


def _dot_access_parts(node: exp.Expression) -> tuple[exp.Column | None, list[tuple[str, str | None]]]:
    if isinstance(node, exp.Column) and not node.table:
        return node, []
    if isinstance(node, exp.Bracket):
        base, steps = _dot_access_parts(node.this)
        return base, [*steps, ("index", None)]
    if isinstance(node, exp.Dot):
        base, steps = _dot_access_parts(node.this)
        field = node.expression.name if isinstance(node.expression, exp.Expression) else None
        if not field:
            return None, []
        return base, [*steps, ("field", field)]
    return None, []


def _available_aliases(info: ScopeInfo) -> str:
    return ", ".join(sorted(info.sources)) or "(none)"


def _suggest_from_sources(info: ScopeInfo, index: CatalogIndex, name: str) -> str | None:
    import difflib

    candidates: list[str] = []
    for src in info.sources.values():
        if src.kind == SourceKind.TABLE and src.table is not None:
            candidates.extend(c.name.lower() for c in src.table.columns)
        elif src.kind == SourceKind.DERIVED and src.outputs:
            candidates.extend(src.outputs)
    matches = difflib.get_close_matches(name.lower(), candidates, n=3, cutoff=0.55)
    if matches:
        return "Did you mean: " + ", ".join(dict.fromkeys(matches)) + "?"
    return None


@dataclass
class BindResult:
    violations: list[Violation] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    has_unknown_tables: bool = False


_CLAUSE_OK_FOR_ALIAS = (exp.Group, exp.Order, exp.Qualify)


def columns_by_owner(
    infos: Sequence[ScopeInfo],
) -> list[tuple[ScopeInfo, list[exp.Column]]]:
    """Attribute each column to the innermost scope that lexically contains it.

    This intentionally does *not* use ``Scope.columns`` /
    ``Scope.external_columns``: those rely on column resolution that is only
    trustworthy on a *qualified* tree, and on a raw tree they bubble
    unresolvable bare columns up to the parent scope (making a subquery's own
    columns look like the parent's). Lexical containment is unambiguous.
    """
    owner_by_expr_id: dict[int, ScopeInfo] = {id(i.expression): i for i in infos}
    buckets: dict[int, list[exp.Column]] = {id(i.expression): [] for i in infos}
    for info in infos:
        root = info.expression
        for col in root.find_all(exp.Column):
            node = col.parent
            while node is not None:
                owner = owner_by_expr_id.get(id(node))
                if owner is not None:
                    if owner.expression is root:
                        buckets[id(root)].append(col)
                    break
                node = node.parent
    return [(info, buckets[id(info.expression)]) for info in infos]


def dots_by_owner(infos: Sequence[ScopeInfo]) -> list[tuple[ScopeInfo, list[exp.Dot]]]:
    """Attribute top-level dotted-access expressions to their lexical scope."""
    owner_by_expr_id: dict[int, ScopeInfo] = {id(i.expression): i for i in infos}
    buckets: dict[int, list[exp.Dot]] = {id(i.expression): [] for i in infos}
    for info in infos:
        root = info.expression
        for dot in root.find_all(exp.Dot):
            if isinstance(dot.parent, exp.Dot):
                continue
            node = dot.parent
            while node is not None:
                owner = owner_by_expr_id.get(id(node))
                if owner is not None:
                    if owner.expression is root:
                        buckets[id(root)].append(dot)
                    break
                node = node.parent
    return [(info, buckets[id(info.expression)]) for info in infos]


def bind_names(tree: exp.Expression, index: CatalogIndex) -> BindResult:
    """Validate every table and column reference against the catalog."""
    result = BindResult()
    infos, table_violations, tables = build_scope_maps(tree, index)
    result.violations.extend(table_violations)
    result.tables = tables
    result.has_unknown_tables = any(
        v.code == Code.UNKNOWN_TABLE for v in table_violations
    )
    seen: set[tuple[str, str, str]] = set()

    def report(v: Violation) -> None:
        key = (v.code.value, v.table or "", v.column or "")
        if key not in seen:
            seen.add(key)
            result.violations.append(v)

    owned_dots = {id(info.expression): dots for info, dots in dots_by_owner(infos)}
    for info, owned_columns in columns_by_owner(infos):
        own_outputs: set[str] | None = None
        for col in owned_columns:
            # ``t.*`` — only the alias must exist
            if isinstance(col.this, exp.Star):
                if col.table and _lookup_alias(info, index.normalize(col.table)) is None:
                    report(
                        Violation(
                            Code.UNKNOWN_TABLE_ALIAS,
                            Severity.ERROR,
                            f"{col.sql()} references unknown table or alias {col.table!r}",
                            column=col.sql(),
                            hint=f"Aliases in scope: {_available_aliases(info)}",
                        )
                    )
                continue

            name_norm = index.normalize(col.name) if col.name else ""
            if not name_norm:
                continue

            if col.table:
                alias_norm = index.normalize(col.table)
                src = _lookup_alias(info, alias_norm)
                if src is None:
                    handled, nested_violation = _resolve_struct_column(info, index, col)
                    if handled:
                        if nested_violation is not None:
                            report(nested_violation)
                        continue
                    report(
                        Violation(
                            Code.UNKNOWN_TABLE_ALIAS,
                            Severity.ERROR,
                            f"Column {col.sql()!r} references unknown table or alias {col.table!r}",
                            column=col.name,
                            hint=f"Aliases in scope: {_available_aliases(info)}",
                        )
                    )
                elif src.kind == SourceKind.TABLE and src.table is not None:
                    if src.table.column(col.name) is None:
                        report(
                            Violation(
                                Code.UNKNOWN_COLUMN,
                                Severity.ERROR,
                                f"Column {col.name!r} does not exist on table "
                                f"{src.table.display_name!r}",
                                table=src.table.display_name,
                                column=col.name,
                                hint=CatalogIndex.suggest_columns(src.table, col.name),
                            )
                        )
                elif src.kind == SourceKind.DERIVED and src.outputs is not None:
                    if name_norm not in src.outputs:
                        outs = ", ".join(sorted(src.outputs)[:12])
                        output_matches = difflib.get_close_matches(
                            name_norm, sorted(src.outputs), n=3, cutoff=0.5
                        )
                        suggestion = (
                            f"Did you mean: {', '.join(output_matches)}? "
                            if output_matches
                            else ""
                        )
                        report(
                            Violation(
                                Code.UNKNOWN_COLUMN,
                                Severity.ERROR,
                                f"Subquery/CTE {col.table!r} has no output column {col.name!r}",
                                table=col.table,
                                column=col.name,
                                hint=f"{suggestion}Its output columns are: {outs}",
                            )
                        )
                # DERIVED_OPAQUE / UNKNOWN_TABLE / OTHER: nothing provable
                continue

            # bare column
            known_hits: list[Source] = []
            opaque_present = False
            for src in info.sources.values():
                if src.kind == SourceKind.TABLE and src.table is not None:
                    if src.table.column(col.name) is not None:
                        known_hits.append(src)
                elif src.kind == SourceKind.DERIVED and src.outputs is not None:
                    if name_norm in src.outputs:
                        known_hits.append(src)
                else:
                    opaque_present = True

            if len(known_hits) > 1:
                srcs = ", ".join(sorted(s.alias for s in known_hits))
                report(
                    Violation(
                        Code.AMBIGUOUS_COLUMN,
                        Severity.ERROR,
                        f"Column {col.name!r} is ambiguous; it exists in: {srcs}",
                        column=col.name,
                        hint=f"Qualify it, e.g. {known_hits[0].alias}.{col.name}",
                    )
                )
                continue
            if len(known_hits) == 1 or opaque_present:
                continue

            # correlated reference?
            cur = info
            found = False
            while cur.can_correlate and cur.parent is not None:
                cur = cur.parent
                for src in cur.sources.values():
                    if src.kind == SourceKind.TABLE and src.table is not None:
                        if src.table.column(col.name) is not None:
                            found = True
                    elif src.kind == SourceKind.DERIVED and src.outputs is not None:
                        if name_norm in src.outputs:
                            found = True
                    else:
                        found = True  # opaque ancestor: can't disprove
                if found:
                    break
            if found:
                continue

            # No real column matches. It may still be a legal reference to the
            # query's own output alias — but only in GROUP BY / ORDER BY /
            # QUALIFY. Postgres and Trino/Athena reject output aliases in
            # WHERE, HAVING, JOIN ON, and the SELECT list itself, so flag those
            # (sqlglot's qualify() would otherwise silently inline them).
            if own_outputs is None:
                own_outputs = _own_output_names(info)
            if name_norm in own_outputs:
                clause = col.find_ancestor(
                    exp.Group, exp.Order, exp.Qualify, exp.Where, exp.Having, exp.Join
                )
                if isinstance(clause, _CLAUSE_OK_FOR_ALIAS):
                    continue
                label = type(clause).__name__.upper() if clause else "SELECT list"
                report(
                    Violation(
                        Code.ALIAS_MISUSE,
                        Severity.ERROR,
                        f"SELECT alias {col.name!r} cannot be referenced in the "
                        f"{label}; only GROUP BY and ORDER BY may use output aliases",
                        column=col.name,
                        hint=f"Repeat the underlying expression instead of {col.name!r}.",
                    )
                )
                continue

            report(
                Violation(
                    Code.UNKNOWN_COLUMN,
                    Severity.ERROR,
                    f"Column {col.name!r} does not exist in any table in scope",
                    column=col.name,
                    hint=_suggest_from_sources(info, index, col.name),
                )
            )

        for dot in owned_dots.get(id(info.expression), []):
            base, steps = _dot_access_parts(dot)
            if base is None or not steps:
                continue
            physical_matches, opaque = _physical_column_matches(info, index, base.name)
            if len(physical_matches) != 1 or opaque:
                continue
            table, catalog_column = physical_matches[0]
            violation = _validate_type_path(
                index.data_type(catalog_column), steps, index, dot.sql(dialect=index.dialect)
            )
            if violation is not None:
                violation.table = table.display_name
                report(violation)
    return result


# ---------------------------------------------------------------------------
# Qualification (star expansion, canonical aliases)
# ---------------------------------------------------------------------------


def mark_star_selects(tree: exp.Expression) -> int:
    """Tag SELECTs that use ``*`` so post-expansion passes can tell star-derived
    projection items from ones the author typed explicitly."""
    count = 0
    for sel in tree.find_all(exp.Select):
        for e in sel.expressions:
            if isinstance(e, exp.Star) or (
                isinstance(e, exp.Column) and isinstance(e.this, exp.Star)
            ):
                sel.meta[STAR_MARK] = True
                count += 1
                break
    return count


def qualify_tree(
    tree: exp.Expression, index: CatalogIndex, policy: Policy, dialect: str
) -> tuple[exp.Expression, bool, Violation | None]:
    """Run sqlglot qualification on a copy; fall back to the raw tree on error.

    Returns (tree, qualified, violation). ``qualified`` False means downstream
    passes work on the unqualified tree (checks that need types are skipped).
    """
    working = tree.copy()
    try:
        qualified = qualify(
            working,
            schema=index.mapping_schema(),
            dialect=dialect,
            db=index.default_schema if index.has_schemas else None,
            expand_stars=policy.expand_star,
            validate_qualify_columns=True,
            quote_identifiers=False,
            identify=False,
        )
        return qualified, True, None
    except SqlglotError as e:
        msg = str(e).split("\n", 1)[0]
        return tree, False, Violation(
            Code.SEMANTIC_ERROR,
            Severity.ERROR,
            f"Query could not be fully analyzed: {msg}",
            hint="Simplify the query structure and try again.",
        )


# ---------------------------------------------------------------------------
# Type checking
# ---------------------------------------------------------------------------

_NUM = "numeric"
_TEXT = "text"
_TIME = "temporal"
_BOOL = "boolean"
_COMPLEX = "complex"

_COMPLEX_TYPES = _STRUCTY_TYPES | {
    exp.DataType.Type.ARRAY,
    exp.DataType.Type.NESTED,
}


def _family(dtype: exp.DataType | None) -> str | None:
    if dtype is None or not isinstance(dtype, exp.DataType):
        return None
    t = dtype.this
    if t == exp.DataType.Type.UNKNOWN:
        return None
    if t in exp.DataType.NUMERIC_TYPES:
        return _NUM
    if t in exp.DataType.TEXT_TYPES:
        return _TEXT
    if t in exp.DataType.TEMPORAL_TYPES:
        return _TIME
    if t == exp.DataType.Type.BOOLEAN:
        return _BOOL
    if t in _COMPLEX_TYPES:
        return _COMPLEX
    return None


def _col_info(node: exp.Expression) -> tuple[str | None, str | None]:
    if isinstance(node, exp.Column):
        return node.name or None, node.table or None
    return None, None


def _check_pair(
    left: exp.Expression, right: exp.Expression, context: exp.Expression, dialect: str
) -> Violation | None:
    lf, rf = _family(left.type), _family(right.type)
    if lf is None or rf is None or lf == rf:
        return None

    def make(severity: Severity, msg: str, hint: str | None = None) -> Violation:
        col, tbl = _col_info(left)
        if col is None:
            col, tbl = _col_info(right)
        snippet = context.sql(dialect=dialect)
        if len(snippet) > 90:
            snippet = snippet[:87] + "..."
        return Violation(
            Code.TYPE_MISMATCH,
            severity,
            f"{msg} in {snippet!r}",
            table=tbl,
            column=col,
            hint=hint,
        )

    # Literal-aware rules: engines coerce *some* literals.
    for lit, other_f, lit_f in ((right, lf, rf), (left, rf, lf)):
        if isinstance(lit, exp.Literal):
            value = lit.name
            if lit.is_string and other_f == _TIME:
                if _ISO_DATEISH.match(value):
                    return None
                return make(
                    Severity.ERROR,
                    f"String {value!r} is not a valid date/timestamp",
                    hint="Use an ISO format like '2026-01-31' or '2026-01-31 12:00:00'.",
                )
            if lit.is_string and other_f == _NUM:
                if _NUMERICISH.match(value):
                    return None
                return make(
                    Severity.ERROR,
                    f"String {value!r} compared against a numeric column",
                    hint="Use an unquoted numeric literal.",
                )
            if lit_f == _NUM and other_f == _TEXT:
                return make(
                    Severity.ERROR,
                    "Numeric literal compared against a text column",
                    hint="Quote the value, or CAST the column.",
                )
            if lit_f == _NUM and other_f == _BOOL:
                return make(
                    Severity.ERROR,
                    "Numeric literal compared against a boolean column",
                    hint="Use TRUE/FALSE.",
                )
            if lit.is_string and other_f == _BOOL:
                return make(
                    Severity.WARNING,
                    f"String {value!r} compared against a boolean column",
                    hint="Use TRUE/FALSE.",
                )
            if lit_f == _NUM and other_f == _TIME:
                return make(
                    Severity.ERROR,
                    "Numeric literal compared against a date/timestamp column",
                    hint="Use a date literal like DATE '2026-01-31'.",
                )
    return make(
        Severity.WARNING,
        f"Comparison between {lf} and {rf} expressions",
        hint="Add an explicit CAST if this is intended.",
    )


_CMP_NODES = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


def annotate_query_types(
    tree: exp.Expression, index: CatalogIndex, dialect: str
) -> exp.Expression | None:
    """Annotate once for all type consumers; absence means analysis was skipped."""
    try:
        return annotate_types(tree, schema=index.mapping_schema(), dialect=dialect)
    except Exception:
        return None


def check_types(
    tree: exp.Expression, index: CatalogIndex, dialect: str
) -> list[Violation]:
    """Flag comparisons whose operand types cannot work on the engine."""
    annotated = annotate_query_types(tree, index, dialect)
    return check_comparison_types(annotated, dialect) if annotated is not None else []


def check_comparison_types(annotated: exp.Expression, dialect: str) -> list[Violation]:
    """Check comparisons on an already annotated tree."""
    violations: list[Violation] = []
    seen: set[str] = set()

    def add(v: Violation | None) -> None:
        if v is not None and v.message not in seen:
            seen.add(v.message)
            violations.append(v)

    for node in annotated.walk():
        if isinstance(node, _CMP_NODES):
            add(_check_pair(node.this, node.expression, node, dialect))
        elif isinstance(node, exp.Between):
            add(_check_pair(node.this, node.args["low"], node, dialect))
            add(_check_pair(node.this, node.args["high"], node, dialect))
        elif isinstance(node, exp.In):
            if node.args.get("query") or node.args.get("unnest"):
                continue
            for item in node.expressions:
                add(_check_pair(node.this, item, node, dialect))
    return violations


def check_function_types(tree: exp.Expression, dialect: str, policy: Policy) -> list[Violation]:
    """Warn only when every modeled overload conflicts with a known argument."""
    from sqlguard.analyzer import _function_names
    from sqlguard.functions import TYPED_OVERLOADS, signature_registry

    registry = signature_registry(dialect)
    denied = policy.effective_function_denylist(dialect)
    violations: list[Violation] = []
    for node in tree.find_all(exp.Func):
        # Anonymous/schema-qualified calls may resolve to user-defined overloads.
        if isinstance(node, exp.Anonymous) or isinstance(node.parent, exp.Dot):
            continue
        names = _function_names(cast(exp.Expression, node))
        if any(name in denied for name in names):
            continue
        if policy.function_allowlist is not None and not any(
            name in policy.function_allowlist for name in names
        ):
            continue
        name = node.sql_name().lower()
        spec = registry.get(name)
        overloads = TYPED_OVERLOADS.get(name)
        if spec is None or not overloads:
            continue
        if not any(sig.accepts(len(list(node.iter_expressions()))) for sig in spec.signatures):
            continue
        if name == "substring" and dialect != "postgres":
            overloads = overloads[:1]
        conflicts = []
        for overload in overloads:
            mismatch = []
            for slot, expected in zip(overload.slots, overload.families):
                argument = node.args.get(slot)
                if not isinstance(argument, exp.Expression):
                    continue
                actual = _family(argument.type)
                # Literals may use engine-specific implicit input conversion.
                if isinstance(argument, (exp.Literal, exp.Null, exp.Placeholder, exp.Parameter)):
                    continue
                if actual is not None and expected is not None and actual != expected:
                    mismatch.append({"argument": slot, "actual": actual, "expected": expected})
            conflicts.append(mismatch)
        if all(conflicts):
            violations.append(Violation(
                Code.FUNCTION_MISUSE, Severity.WARNING,
                f"Known argument types conflict with modeled overloads of {spec.name}()",
                hint="Check argument types; add an explicit CAST only if the conversion is intended.",
                extra={"function": spec.name, "dialect": dialect,
                       "failure": "argument_type", "overload_conflicts": conflicts},
            ))
    return violations


# ---------------------------------------------------------------------------
# Aggregation correctness
# ---------------------------------------------------------------------------


def _nearest_select(node: exp.Expression) -> exp.Select | None:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Select):
            return parent
        parent = parent.parent
    return None


def _is_windowed_aggregate(node: exp.Expression, select: exp.Select) -> bool:
    parent = node.parent
    while parent is not None and parent is not select:
        if isinstance(parent, exp.Window):
            return True
        parent = parent.parent
    return False


def _group_aggregates(expression: exp.Expression, select: exp.Select) -> list[exp.AggFunc]:
    return [
        aggregate
        for aggregate in expression.find_all(exp.AggFunc)
        if _nearest_select(cast(exp.Expression, aggregate)) is select
        and not _is_windowed_aggregate(cast(exp.Expression, aggregate), select)
    ]


def _columns_outside_aggregates(
    expression: exp.Expression, select: exp.Select
) -> list[exp.Column]:
    columns: list[exp.Column] = []
    for column in expression.find_all(exp.Column):
        if _nearest_select(column) is not select:
            continue
        parent = column.parent
        inside_aggregate = False
        while parent is not None and parent is not select:
            if isinstance(parent, exp.AggFunc):
                inside_aggregate = True
                break
            parent = parent.parent
        if not inside_aggregate:
            columns.append(column)
    return columns


def check_aggregation(
    tree: exp.Expression,
    index: CatalogIndex,
    dialect: str,
    scope_index: ScopeIndex | None = None,
) -> list[Violation]:
    """Reject aggregation shapes that engines would fail or misinterpret."""
    if scope_index is not None:
        infos = scope_index.infos
        by_expression = scope_index.by_expression
    else:
        infos, by_expression = scope_infos_with_lookup(tree, index)

    # Most analytical queries are not aggregate queries. Avoid repeated
    # per-expression walks on that hot path.
    if tree.find(exp.Group) is None and tree.find(exp.AggFunc) is None:
        return []

    violations: list[Violation] = []
    reported: set[tuple[int, str, str]] = set()
    for info in infos:
        select = info.expression
        if not isinstance(select, exp.Select):
            continue

        group = select.args.get("group")
        group_expressions = list(group.expressions) if isinstance(group, exp.Group) else []
        group_keys: set[str] = set()
        group_origins: set[tuple[tuple[str | None, str], str]] = set()
        for expression in group_expressions:
            resolved = expression
            if isinstance(expression, exp.Literal) and not expression.is_string:
                try:
                    ordinal = int(expression.this)
                except (TypeError, ValueError):
                    ordinal = 0
                if 1 <= ordinal <= len(select.expressions):
                    resolved = select.expressions[ordinal - 1]
            if isinstance(resolved, exp.Alias):
                resolved = resolved.this
            group_keys.add(resolved.sql(dialect=dialect))
            for column in resolved.find_all(exp.Column):
                origin = resolve_column_origin(by_expression, info, column, index)
                if origin is not None:
                    group_origins.add((index.table_key(origin[0]), index.normalize(origin[1])))

        functionally_grouped: set[tuple[str | None, str]] = set()
        if dialect == "postgres":
            for source in info.sources.values():
                table = source.table
                if table is None or not table.primary_key:
                    continue
                key = index.table_key(table)
                if all((key, index.normalize(column)) in group_origins for column in table.primary_key):
                    functionally_grouped.add(key)

        conditions: list[tuple[str, exp.Expression]] = []
        where = select.args.get("where")
        if isinstance(where, exp.Where):
            conditions.append(("WHERE", where.this))
        for join in select.args.get("joins") or []:
            on = join.args.get("on")
            if isinstance(on, exp.Expression):
                conditions.append(("JOIN ON", on))
        for location, condition in conditions:
            if _group_aggregates(condition, select):
                report_key = (id(select), Code.AGGREGATE_IN_WHERE.value, location)
                if report_key not in reported:
                    reported.add(report_key)
                    violations.append(
                        Violation(
                            Code.AGGREGATE_IN_WHERE,
                            Severity.ERROR,
                            f"Aggregate functions are not allowed in {location}",
                            hint="Move aggregate filters to HAVING.",
                        )
                    )

        expressions_to_check = list(select.expressions)
        having = select.args.get("having")
        if isinstance(having, exp.Having):
            expressions_to_check.append(having.this)
        order = select.args.get("order")
        if isinstance(order, exp.Order):
            expressions_to_check.extend(order.expressions)

        has_group_aggregate = any(
            _group_aggregates(expression, select) for expression in expressions_to_check
        )
        if not group_expressions and not has_group_aggregate:
            continue

        select_aliases = {
            index.normalize(item.alias): item.this
            for item in select.expressions
            if isinstance(item, exp.Alias) and item.alias
        }
        for expression in expressions_to_check:
            unaliased = expression.this if isinstance(expression, (exp.Alias, exp.Ordered)) else expression
            if (
                isinstance(unaliased, exp.Column)
                and not unaliased.table
                and index.normalize(unaliased.name) in select_aliases
            ):
                unaliased = select_aliases[index.normalize(unaliased.name)]
            if unaliased.sql(dialect=dialect) in group_keys:
                continue
            for column in _columns_outside_aggregates(unaliased, select):
                column_key = column.sql(dialect=dialect)
                if column_key in group_keys:
                    continue
                origin = resolve_column_origin(by_expression, info, column, index)
                if origin is not None and index.table_key(origin[0]) in functionally_grouped:
                    continue
                report_key = (id(select), Code.GROUP_BY_VIOLATION.value, column_key)
                if report_key in reported:
                    continue
                reported.add(report_key)
                violations.append(
                    Violation(
                        Code.GROUP_BY_VIOLATION,
                        Severity.ERROR,
                        f"Column {column_key!r} must appear in GROUP BY or be aggregated",
                        column=column.name,
                        hint=f"Add {column_key} to GROUP BY or wrap it in an aggregate.",
                    )
                )
    return violations


def check_allowed_values(
    tree: exp.Expression,
    index: CatalogIndex,
    policy: Policy,
    dialect: str,
    scope_index: ScopeIndex | None = None,
) -> list[Violation]:
    """Validate equality and IN literals against catalog enum/domain metadata."""
    if not index.has_allowed_values:
        return []
    if scope_index is not None:
        infos = scope_index.infos
        by_expression = scope_index.by_expression
    else:
        infos, by_expression = scope_infos_with_lookup(tree, index)
    info_by_select = {
        id(info.expression): info for info in infos if isinstance(info.expression, exp.Select)
    }
    violations: list[Violation] = []
    seen: set[tuple[str, str, str]] = set()

    def inspect(column_expr: exp.Column, literal: exp.Expression) -> None:
        if not isinstance(literal, exp.Literal) or not literal.is_string:
            return
        select = _nearest_select(column_expr)
        info = info_by_select.get(id(select)) if select is not None else None
        if info is None:
            return
        origin = resolve_column_origin(by_expression, info, column_expr, index)
        if origin is None:
            return
        table, column_name = origin
        column = table.column(column_name)
        if column is None or column.allowed_values is None:
            return
        actual = literal.name
        allowed = column.allowed_values
        normalize = str.casefold if policy.case_insensitive_enums else (lambda value: value)
        if normalize(actual) in {normalize(value) for value in allowed}:
            return
        key = (table.display_name, column.name, actual)
        if key in seen:
            return
        seen.add(key)
        matches = difflib.get_close_matches(
            normalize(actual),
            [normalize(value) for value in allowed],
            n=1,
            cutoff=0.6,
        )
        suggestion = None
        if matches:
            match = next(value for value in allowed if normalize(value) == matches[0])
            suggestion = f" Did you mean {match!r}?"
        shown = ", ".join(repr(value) for value in allowed[:12])
        violations.append(
            Violation(
                Code.UNKNOWN_VALUE,
                Severity.ERROR,
                f"Value {actual!r} is not allowed for "
                f"{table.display_name}.{column.name}.{suggestion or ''}",
                table=table.display_name,
                column=column.name,
                hint=f"Allowed values: {shown}",
                extra={"value": actual, "allowed_values": list(allowed[:12])},
            )
        )

    for node in tree.walk():
        if isinstance(node, (exp.EQ, exp.NEQ)):
            left, right = node.this, node.expression
            if isinstance(left, exp.Column):
                inspect(left, right)
            if isinstance(right, exp.Column):
                inspect(right, left)
        elif isinstance(node, exp.In) and isinstance(node.this, exp.Column):
            if node.args.get("query") or node.args.get("unnest"):
                continue
            for literal in node.expressions:
                inspect(node.this, literal)
    return violations


# ---------------------------------------------------------------------------
# Column origin resolution + referenced-column collection (used by rewrite/cost)
# ---------------------------------------------------------------------------


def resolve_column_origin(
    infos_by_scope: dict[int, ScopeInfo],
    info: ScopeInfo,
    col: exp.Column,
    index: CatalogIndex,
    _depth: int = 0,
) -> tuple[Table, str] | None:
    """Trace a column reference to its physical (table, column), following
    CTE/derived projections when they are plain column pass-throughs."""
    if _depth > 12 or isinstance(col.this, exp.Star) or not col.name:
        return None
    name_norm = index.normalize(col.name)

    src: Source | None = None
    if col.table:
        src = _lookup_alias(info, index.normalize(col.table))
    else:
        hits = []
        for s in info.sources.values():
            if s.kind == SourceKind.TABLE and s.table is not None:
                if s.table.column(col.name) is not None:
                    hits.append(s)
            elif s.kind == SourceKind.DERIVED and s.outputs is not None:
                if name_norm in s.outputs:
                    hits.append(s)
        if len(hits) == 1:
            src = hits[0]
    if src is None:
        return None
    if src.kind == SourceKind.TABLE and src.table is not None:
        if src.table.column(col.name) is not None:
            return src.table, name_norm
        return None
    if src.kind == SourceKind.DERIVED and src.scope is not None:
        expr = src.scope.expression
        if isinstance(expr, exp.SetOperation):
            expr = expr.this
        if not isinstance(expr, exp.Select):
            return None
        target: exp.Column | None = None
        for item in expr.selects:
            out_name = item.alias_or_name
            if out_name and index.normalize(out_name) == name_norm:
                inner = item.this if isinstance(item, exp.Alias) else item
                if isinstance(inner, exp.Column):
                    target = inner
                break
        if target is None:
            return None
        inner_info = infos_by_scope.get(id(expr))
        if inner_info is None:
            return None
        return resolve_column_origin(infos_by_scope, inner_info, target, index, _depth + 1)
    return None


def scope_infos_with_lookup(
    tree: exp.Expression, index: CatalogIndex
) -> tuple[list[ScopeInfo], dict[int, ScopeInfo]]:
    infos, _, _ = build_scope_maps(tree, index)
    by_expr = {id(i.expression): i for i in infos}
    return infos, by_expr


ALL_COLUMNS = None  # sentinel: every column of the table is referenced


def collect_referenced_columns(
    tree: exp.Expression,
    index: CatalogIndex,
    scope_index: ScopeIndex | None = None,
) -> dict[tuple[str | None, str], set[str] | None]:
    """Which physical columns does the (final) tree touch, per table?

    Value ``None`` means "all columns" (an unexpanded ``*``). Keys are
    normalized ``(schema, table)`` pairs. Pass ``scope_index`` (a
    :class:`sqlguard.scopeindex.ScopeIndex` over ``tree``) to reuse
    already-built scope maps.
    """
    if scope_index is not None:
        infos, by_expr = scope_index.infos, scope_index.by_expression
    else:
        infos, by_expr = scope_infos_with_lookup(tree, index)
    referenced: dict[tuple[str | None, str], set[str] | None] = {}

    def key_of(table: Table) -> tuple[str | None, str]:
        return (
            index.normalize(table.schema) if table.schema else None,
            index.normalize(table.name),
        )

    def mark_all(table: Table) -> None:
        referenced[key_of(table)] = ALL_COLUMNS

    def mark(table: Table, column: str) -> None:
        key = key_of(table)
        if key in referenced and referenced[key] is ALL_COLUMNS:
            return
        cur = referenced.get(key)
        if not isinstance(cur, set):
            cur = set()
        cur.add(column)
        referenced[key] = cur

    for info, owned_columns in columns_by_owner(infos):
        expr = info.expression
        if isinstance(expr, exp.Select):
            has_bare_star = any(isinstance(e, exp.Star) for e in expr.expressions)
            if has_bare_star:
                for src in info.sources.values():
                    if src.kind == SourceKind.TABLE and src.table is not None:
                        mark_all(src.table)
        for col in owned_columns:
            if isinstance(col.this, exp.Star):
                if col.table:
                    star_src = _lookup_alias(info, index.normalize(col.table))
                    if (
                        star_src
                        and star_src.kind == SourceKind.TABLE
                        and star_src.table is not None
                    ):
                        mark_all(star_src.table)
                continue
            origin = resolve_column_origin(by_expr, info, col, index)
            if origin is not None:
                mark(origin[0], origin[1])
        # tables selected but never column-referenced should still appear
        for src in info.sources.values():
            if src.kind == SourceKind.TABLE and src.table is not None:
                referenced.setdefault(key_of(src.table), set())
    return referenced
