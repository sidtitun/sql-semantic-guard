"""Structured validation output: violations, rewrites, cost estimates, results.

Everything here is a plain dataclass that serializes to JSON via ``to_dict()``
so results can cross process/service boundaries, be logged, or be handed to an
LLM for self-repair (see :meth:`ValidationResult.feedback`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """How serious a violation is. Only ERROR blocks execution."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Code(str, Enum):
    """Machine-readable violation codes (stable across releases)."""

    # Parsing / statement shape
    PARSE_ERROR = "parse_error"
    MULTIPLE_STATEMENTS = "multiple_statements"
    DISALLOWED_STATEMENT = "disallowed_statement"
    DISALLOWED_COMMAND = "disallowed_command"
    NESTED_WRITE = "nested_write"
    SELECT_INTO = "select_into"
    LOCKING_CLAUSE = "locking_clause"
    FORBIDDEN_FUNCTION = "forbidden_function"

    # Semantic / catalog binding
    UNKNOWN_TABLE = "unknown_table"
    UNKNOWN_TABLE_ALIAS = "unknown_table_alias"
    UNKNOWN_COLUMN = "unknown_column"
    AMBIGUOUS_COLUMN = "ambiguous_column"
    ALIAS_MISUSE = "alias_misuse"
    TYPE_MISMATCH = "type_mismatch"
    SEMANTIC_ERROR = "semantic_error"

    # Column-level policy
    COLUMN_DENIED = "column_denied"
    EMPTY_SELECT = "empty_select"

    # Row-level security
    RLS_PARAM_MISSING = "rls_param_missing"
    RLS_CONFIG_ERROR = "rls_config_error"
    MISSING_TENANT_FILTER = "missing_tenant_filter"
    TENANT_FILTER_CONFLICT = "tenant_filter_conflict"

    # Cost / scan safety
    CARTESIAN_JOIN = "cartesian_join"
    SUSPICIOUS_JOIN = "suspicious_join"
    MISSING_PARTITION_FILTER = "missing_partition_filter"
    SCAN_BUDGET_EXCEEDED = "scan_budget_exceeded"
    MISSING_STATISTICS = "missing_statistics"
    COST_ESTIMATION_FAILED = "cost_estimation_failed"

    # Guard internals (fail closed, never crash the caller)
    INTERNAL_ERROR = "internal_error"


class RewriteKind(str, Enum):
    """Automatic, safe-direction rewrites applied to the query."""

    STAR_EXPANDED = "star_expanded"
    SENSITIVE_COLUMN_EXCLUDED = "sensitive_column_excluded"
    LIMIT_ADDED = "limit_added"
    LIMIT_CLAMPED = "limit_clamped"
    RLS_FILTER_ADDED = "rls_filter_added"
    RLS_FILTER_PRESENT = "rls_filter_present"


@dataclass
class Violation:
    """A single rule violation found in the SQL."""

    code: Code
    severity: Severity
    message: str
    table: str | None = None
    column: str | None = None
    hint: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.severity == Severity.ERROR

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "message": self.message,
            "table": self.table,
            "column": self.column,
            "hint": self.hint,
            "extra": dict(self.extra),
        }

    def __str__(self) -> str:  # pragma: no cover - convenience only
        hint = f" Hint: {self.hint}" if self.hint else ""
        return f"[{self.code.value}] {self.message}{hint}"


@dataclass
class Rewrite:
    """A record of one automatic rewrite the guard applied."""

    kind: RewriteKind
    message: str
    table: str | None = None
    column: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "table": self.table,
            "column": self.column,
            "extra": dict(self.extra),
        }


@dataclass
class TableScanEstimate:
    """Per-table scan estimate produced by a cost estimator."""

    table: str
    bytes_scanned: int | None = None
    rows_scanned: int | None = None
    partition_filtered: bool | None = None
    column_ratio: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "bytes_scanned": self.bytes_scanned,
            "rows_scanned": self.rows_scanned,
            "partition_filtered": self.partition_filtered,
            "column_ratio": self.column_ratio,
        }


@dataclass
class CostEstimate:
    """Aggregate pre-execution scan estimate.

    ``incomplete`` is True when one or more tables lacked statistics, in which
    case the totals are lower bounds over the tables that *did* have stats.
    """

    source: str = "heuristic"
    bytes_scanned: int | None = None
    rows_scanned: int | None = None
    total_cost: float | None = None  # planner units when source == explain
    per_table: list[TableScanEstimate] = field(default_factory=list)
    incomplete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "bytes_scanned": self.bytes_scanned,
            "rows_scanned": self.rows_scanned,
            "total_cost": self.total_cost,
            "per_table": [t.to_dict() for t in self.per_table],
            "incomplete": self.incomplete,
        }


@dataclass
class QueryStats:
    """Metadata gathered while validating (independent of pass/fail)."""

    statement: str = "select"
    tables: list[str] = field(default_factory=list)
    referenced_columns: dict[str, list[str]] = field(default_factory=dict)
    cost: CostEstimate | None = None
    checks_run: list[str] = field(default_factory=list)
    checks_skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "tables": list(self.tables),
            "referenced_columns": {k: list(v) for k, v in self.referenced_columns.items()},
            "cost": self.cost.to_dict() if self.cost else None,
            "checks_run": list(self.checks_run),
            "checks_skipped": list(self.checks_skipped),
        }


@dataclass
class ValidationResult:
    """Outcome of validating one SQL statement.

    ``sql`` is the rewritten, execution-safe SQL — set only when ``valid`` is
    True (fail-closed: never execute anything when the result is invalid).
    """

    valid: bool
    sql: str | None
    original_sql: str
    dialect: str
    violations: list[Violation] = field(default_factory=list)
    rewrites: list[Rewrite] = field(default_factory=list)
    stats: QueryStats = field(default_factory=QueryStats)
    # True only under Policy(enforcement="log_only") when the query carries
    # ERROR violations that WOULD block in enforcing mode. valid is True in
    # that case; use this flag to measure shadow-mode block rates.
    would_block: bool = False

    # -- convenience -------------------------------------------------------

    @property
    def is_valid(self) -> bool:
        return self.valid

    @property
    def rewritten_sql(self) -> str | None:
        return self.sql

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == Severity.WARNING]

    def __bool__(self) -> bool:
        return self.valid

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "would_block": self.would_block,
            "sql": self.sql,
            "original_sql": self.original_sql,
            "dialect": self.dialect,
            "violations": [v.to_dict() for v in self.violations],
            "rewrites": [r.to_dict() for r in self.rewrites],
            "stats": self.stats.to_dict(),
        }

    def feedback(self, include_rewrites: bool = True) -> str:
        """Render a compact, deterministic repair prompt for an LLM.

        Designed to be appended to the conversation that generated the SQL so
        the model can produce a corrected query. Errors first, then warnings,
        each with its machine code and any hint (e.g. did-you-mean).
        """
        if self.valid and not self.violations:
            return "The SQL passed validation."

        lines: list[str] = []
        errors = self.errors
        warnings = self.warnings
        if self.valid and self.would_block:
            lines.append(
                f"The SQL was allowed (shadow mode) but would be BLOCKED in "
                f"enforcing mode: {len(errors)} error(s)"
                + (f", {len(warnings)} warning(s)." if warnings else ".")
            )
        elif self.valid:
            lines.append(
                f"The SQL passed validation with {len(warnings)} warning(s)."
            )
        else:
            lines.append(
                f"The SQL failed validation with {len(errors)} error(s)"
                + (f" and {len(warnings)} warning(s)." if warnings else ".")
            )

        def _fmt(v: Violation, i: int) -> str:
            loc = ""
            if v.table and v.column:
                loc = f" (table={v.table}, column={v.column})"
            elif v.table:
                loc = f" (table={v.table})"
            elif v.column:
                loc = f" (column={v.column})"
            hint = f" {v.hint}" if v.hint else ""
            return f"  {i}. [{v.code.value}] {v.message}{hint}{loc}"

        if errors:
            lines.append("Errors (must fix):")
            lines.extend(_fmt(v, i) for i, v in enumerate(errors, 1))
        if warnings:
            lines.append("Warnings:")
            lines.extend(_fmt(v, i) for i, v in enumerate(warnings, 1))

        # Only surface rewrites when the query passed — on a rejected query
        # nothing was actually applied (``sql`` is None), so listing rewrites
        # as "applied" would mislead the model.
        if include_rewrites and self.valid and self.rewrites:
            lines.append("Rewrites applied automatically (do not add these yourself):")
            for r in self.rewrites:
                lines.append(f"  - {r.message}")

        if not self.valid:
            lines.append(
                "Regenerate the complete SQL statement fixing every error above. "
                "Only a single read-only SELECT statement is allowed."
            )
        return "\n".join(lines)
