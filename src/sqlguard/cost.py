"""Pre-execution cost controls: join sanity, partition pruning, scan budgets.

The heuristic estimator is deliberately conservative and dependency-free: it
works from catalog statistics (row counts, byte sizes, partition columns)
without touching the database. On Athena — where cost *is* bytes scanned —
this is usually all you need. For Postgres you can additionally gate on the
planner itself with :class:`sqlguard.postgres.PostgresExplainEstimator`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sqlglot import exp

from sqlguard.catalog import CatalogIndex, Table
from sqlguard.policy import Policy
from sqlguard.semantics import (
    ALL_COLUMNS,
    SourceKind,
    build_scope_maps,
    collect_referenced_columns,
)
from sqlguard.violations import (
    Code,
    CostEstimate,
    Severity,
    TableScanEstimate,
    Violation,
)

# ---------------------------------------------------------------------------
# Join sanity
# ---------------------------------------------------------------------------


def _aliases_in(condition: exp.Expression) -> set[str]:
    return {
        col.table.lower()
        for col in condition.find_all(exp.Column)
        if col.table
    }


def check_joins(
    tree: exp.Expression, index: CatalogIndex, policy: Policy, dialect: str
) -> list[Violation]:
    """Heuristics for the classic LLM join failures.

    - join with no ON/USING and no linking WHERE predicate -> cartesian product
    - ON clause that references only one side -> probably a typo'd condition
    """
    if not policy.check_joins:
        return []
    severity = Severity.ERROR if policy.strict_joins else Severity.WARNING
    violations: list[Violation] = []
    infos, _, _ = build_scope_maps(tree, index)

    for info in infos:
        select = info.expression
        if not isinstance(select, exp.Select):
            continue
        joins = select.args.get("joins") or []
        if not joins:
            continue
        from_expr = select.args.get("from_") or select.args.get("from")
        prior: set[str] = set()
        if from_expr is not None and hasattr(from_expr, "this"):
            source = from_expr.this
            alias = source.alias_or_name if isinstance(source, (exp.Table, exp.Subquery)) else None
            if alias:
                prior.add(alias.lower())
        where = select.args.get("where")
        where_conjuncts: list[exp.Expression] = []
        if where is not None:
            cond = where.this
            where_conjuncts = list(cond.flatten()) if isinstance(cond, exp.And) else [cond]

        for join in joins:
            joined = join.this
            joined_alias = (
                joined.alias_or_name
                if isinstance(joined, (exp.Table, exp.Subquery, exp.Unnest, exp.Lateral))
                else None
            )
            if isinstance(joined, (exp.Unnest, exp.Lateral)) or join.args.get("using"):
                if joined_alias:
                    prior.add(joined_alias.lower())
                continue
            on = join.args.get("on")
            kind = (join.kind or "").upper()
            (join.side or "").upper()
            alias_l = joined_alias.lower() if joined_alias else None

            if on is None:
                linked = False
                if alias_l:
                    for conj in where_conjuncts:
                        aliases = _aliases_in(conj)
                        if alias_l in aliases and aliases & prior:
                            linked = True
                            break
                if not linked:
                    label = "CROSS JOIN" if kind == "CROSS" else "join with no ON condition"
                    violations.append(
                        Violation(
                            Code.CARTESIAN_JOIN,
                            severity,
                            f"{label} produces a cartesian product with "
                            f"{joined_alias or 'the joined table'!s}",
                            table=joined_alias,
                            hint="Add an explicit join condition linking the two tables.",
                        )
                    )
            else:
                aliases = _aliases_in(on)
                if alias_l and aliases and (alias_l not in aliases or not (aliases - {alias_l})):
                    violations.append(
                        Violation(
                            Code.SUSPICIOUS_JOIN,
                            Severity.WARNING,
                            f"Join condition {on.sql(dialect=dialect)!r} does not reference "
                            "both sides of the join",
                            table=joined_alias,
                            hint="A correct join condition links the joined table to a prior table.",
                        )
                    )
            if alias_l:
                prior.add(alias_l)
    return violations


# ---------------------------------------------------------------------------
# Partition filter enforcement
# ---------------------------------------------------------------------------

_CONSTRAINING = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.In,
    exp.Between,
    exp.Like,
    exp.ILike,
)


def _column_is_constrained(
    pool: Sequence[exp.Expression], alias_norm: str, column_norm: str, sole_source: bool
) -> bool:
    for cond in pool:
        for col in cond.find_all(exp.Column):
            if isinstance(col.this, exp.Star) or col.name.lower() != column_norm:
                continue
            if col.table and col.table.lower() != alias_norm:
                continue
            if not col.table and not sole_source:
                continue
            node = col.parent
            while node is not None and node is not cond.parent:
                if isinstance(node, _CONSTRAINING):
                    return True
                node = node.parent
    return False


def check_partition_filters(
    tree: exp.Expression, index: CatalogIndex, policy: Policy, dialect: str
) -> tuple[list[Violation], dict[int, bool]]:
    """For every partitioned-table reference, is any partition column constrained?

    Returns violations plus a per-occurrence map used by the heuristic
    estimator, keyed by the table node's id (``id(src.node)``) — that node is
    stable across re-traversals of the same tree, whereas ``Scope`` objects
    are freshly created on each ``build_scope_maps`` call.
    """
    require = policy.effective_require_partition_filter(dialect)
    violations: list[Violation] = []
    flags: dict[int, bool] = {}
    infos, _, _ = build_scope_maps(tree, index)

    for info in infos:
        select = info.expression
        if not isinstance(select, exp.Select):
            continue
        pool: list[exp.Expression] = []
        where = select.args.get("where")
        if where is not None:
            pool.append(where.this)
        for join in select.args.get("joins") or []:
            on = join.args.get("on")
            if on is not None:
                pool.append(on)
        sole = len(info.sources) == 1
        for alias, src in info.sources.items():
            if src.kind != SourceKind.TABLE or src.table is None:
                continue
            table = src.table
            if not table.partition_columns:
                continue
            constrained = any(
                _column_is_constrained(pool, alias, index.normalize(p), sole)
                for p in table.partition_columns
            )
            if src.node is not None:
                flags[id(src.node)] = constrained
            if not constrained and require:
                parts = ", ".join(table.partition_columns)
                violations.append(
                    Violation(
                        Code.MISSING_PARTITION_FILTER,
                        Severity.ERROR,
                        f"Table {table.display_name!r} is partitioned by ({parts}) but the "
                        "query does not constrain any partition column — this forces a "
                        "full-table scan",
                        table=table.display_name,
                        hint=f"Add a filter on {table.partition_columns[0]!r} "
                        f"(e.g. {alias}.{table.partition_columns[0]} >= DATE '2026-01-01').",
                    )
                )
    return violations, flags


# ---------------------------------------------------------------------------
# Heuristic scan estimation
# ---------------------------------------------------------------------------

_TYPE_WIDTHS: dict[exp.DataType.Type, int] = {}


def _init_widths() -> None:
    T = exp.DataType.Type
    widths = {
        "BOOLEAN": 1, "BIT": 1, "TINYINT": 1, "SMALLINT": 2, "MEDIUMINT": 3,
        "INT": 4, "BIGINT": 8, "INT128": 16, "INT256": 32,
        "FLOAT": 4, "DOUBLE": 8, "DECIMAL": 16, "BIGDECIMAL": 32,
        "DATE": 4, "DATE32": 4, "TIME": 8, "TIMETZ": 8,
        "DATETIME": 8, "DATETIME64": 8, "TIMESTAMP": 8, "TIMESTAMPTZ": 8,
        "TIMESTAMPLTZ": 8, "TIMESTAMPNTZ": 8, "TIMESTAMP_S": 8,
        "TIMESTAMP_MS": 8, "TIMESTAMP_NS": 8,
        "UUID": 16, "JSON": 128, "JSONB": 128, "VARIANT": 128, "SUPER": 128,
        "ARRAY": 256, "STRUCT": 256, "MAP": 256, "OBJECT": 256, "NESTED": 256,
        "BINARY": 64, "VARBINARY": 64, "BLOB": 512, "BYTES": 64,
        "TEXT": 64, "MEDIUMTEXT": 256, "LONGTEXT": 512,
        "CHAR": 8, "NCHAR": 8, "VARCHAR": 32, "NVARCHAR": 32,
    }
    for name, width in widths.items():
        t = getattr(T, name, None)
        if t is not None:
            _TYPE_WIDTHS[t] = width


_init_widths()
_DEFAULT_WIDTH = 16


def _column_width(index: CatalogIndex, table: Table, name: str) -> int:
    col = table.column(name)
    if col is None:
        return _DEFAULT_WIDTH
    if col.avg_width is not None:
        return col.avg_width
    dt = index.data_type(col)
    if dt is None:
        return _DEFAULT_WIDTH
    if dt.this in (exp.DataType.Type.VARCHAR, exp.DataType.Type.NVARCHAR, exp.DataType.Type.CHAR):
        params = dt.expressions
        if params and isinstance(params[0], exp.DataTypeParam):
            try:
                return min(int(params[0].this.name), 256)
            except Exception:  # pragma: no cover
                pass
    return _TYPE_WIDTHS.get(dt.this, _DEFAULT_WIDTH)


@dataclass
class EstimateInputs:
    tree: exp.Expression
    index: CatalogIndex
    policy: Policy
    dialect: str
    partition_flags: dict[int, bool] = field(default_factory=dict)


class CostEstimator(Protocol):
    """Anything that can estimate a query's cost before execution."""

    def estimate(
        self, sql: str, inputs: EstimateInputs
    ) -> tuple[CostEstimate | None, list[Violation]]:  # pragma: no cover
        ...


class HeuristicCostEstimator:
    """Static bytes/rows estimate from catalog statistics.

    Per table reference::

        bytes = total_bytes            (or row_count * sum(column widths))
              * column_ratio           (columnar engines scan referenced columns)
              * partition_selectivity  (when a partition filter is present)

    Multiple references to the same table count multiply — that is what the
    engine will scan. Estimates are heuristics for *budgeting*, not billing.
    """

    def estimate(
        self, sql: str, inputs: EstimateInputs
    ) -> tuple[CostEstimate | None, list[Violation]]:
        index, policy, dialect = inputs.index, inputs.policy, inputs.dialect
        violations: list[Violation] = []
        referenced = collect_referenced_columns(inputs.tree, index)
        infos, _, _ = build_scope_maps(inputs.tree, index)

        per_table: list[TableScanEstimate] = []
        total_bytes = 0
        total_rows = 0
        any_bytes = False
        any_rows = False
        incomplete = False
        missing_reported: set[str] = set()

        for info in infos:
            for _alias, src in info.sources.items():
                if src.kind != SourceKind.TABLE or src.table is None:
                    continue
                table = src.table
                key = (
                    index.normalize(table.schema) if table.schema else None,
                    index.normalize(table.name),
                )
                est = TableScanEstimate(table=table.display_name)

                columnar = (
                    table.columnar if table.columnar is not None else dialect == "athena"
                )
                ratio = 1.0
                if columnar and table.columns:
                    refs = referenced.get(key, ALL_COLUMNS)
                    if refs is not ALL_COLUMNS:
                        all_width = sum(
                            _column_width(index, table, c.name) for c in table.columns
                        )
                        ref_width = sum(
                            _column_width(index, table, c) for c in refs  # type: ignore[union-attr]
                        )
                        ratio = max(ref_width / all_width if all_width else 1.0, 0.02)
                est.column_ratio = round(ratio, 4)

                part_factor = 1.0
                if table.partition_columns:
                    filtered = (
                        inputs.partition_flags.get(id(src.node))
                        if src.node is not None
                        else None
                    )
                    est.partition_filtered = filtered
                    if filtered:
                        part_factor = policy.partition_selectivity

                base_bytes: int | None = table.total_bytes
                if base_bytes is None and table.row_count is not None:
                    base_bytes = table.row_count * max(
                        sum(_column_width(index, table, c.name) for c in table.columns), 1
                    )
                if base_bytes is not None:
                    est.bytes_scanned = int(base_bytes * ratio * part_factor)
                    total_bytes += est.bytes_scanned
                    any_bytes = True
                if table.row_count is not None:
                    est.rows_scanned = int(table.row_count * part_factor)
                    total_rows += est.rows_scanned
                    any_rows = True
                if base_bytes is None and table.row_count is None:
                    incomplete = True
                    if (
                        policy.on_missing_stats != "ignore"
                        and table.display_name not in missing_reported
                    ):
                        missing_reported.add(table.display_name)
                        violations.append(
                            Violation(
                                Code.MISSING_STATISTICS,
                                Severity.ERROR
                                if policy.on_missing_stats == "error"
                                else Severity.WARNING,
                                f"No statistics for table {table.display_name!r}; "
                                "scan cost cannot be bounded",
                                table=table.display_name,
                                hint="Add row_count/total_bytes to the catalog entry.",
                            )
                        )
                per_table.append(est)

        estimate = CostEstimate(
            source="heuristic",
            bytes_scanned=total_bytes if any_bytes else None,
            rows_scanned=total_rows if any_rows else None,
            per_table=per_table,
            incomplete=incomplete,
        )
        violations.extend(_budget_violations(estimate, policy))
        return estimate, violations


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024 or unit == "PiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{n} B"  # pragma: no cover


def _budget_violations(estimate: CostEstimate, policy: Policy) -> list[Violation]:
    violations: list[Violation] = []
    if (
        policy.max_bytes_scanned is not None
        and estimate.bytes_scanned is not None
        and estimate.bytes_scanned > policy.max_bytes_scanned
    ):
        worst = sorted(
            (t for t in estimate.per_table if t.bytes_scanned),
            key=lambda t: -(t.bytes_scanned or 0),
        )[:3]
        detail = ", ".join(f"{t.table}={_human_bytes(t.bytes_scanned or 0)}" for t in worst)
        violations.append(
            Violation(
                Code.SCAN_BUDGET_EXCEEDED,
                Severity.ERROR,
                f"Estimated scan of {_human_bytes(estimate.bytes_scanned)} exceeds the "
                f"budget of {_human_bytes(policy.max_bytes_scanned)} ({detail})",
                hint="Narrow the query: filter partition columns, select fewer columns, "
                "or query a smaller table.",
                extra={
                    "estimated_bytes": estimate.bytes_scanned,
                    "max_bytes": policy.max_bytes_scanned,
                },
            )
        )
    if (
        policy.max_rows_scanned is not None
        and estimate.rows_scanned is not None
        and estimate.rows_scanned > policy.max_rows_scanned
    ):
        violations.append(
            Violation(
                Code.SCAN_BUDGET_EXCEEDED,
                Severity.ERROR,
                f"Estimated {estimate.rows_scanned:,} rows scanned exceeds the "
                f"budget of {policy.max_rows_scanned:,}",
                extra={
                    "estimated_rows": estimate.rows_scanned,
                    "max_rows": policy.max_rows_scanned,
                },
            )
        )
    return violations
