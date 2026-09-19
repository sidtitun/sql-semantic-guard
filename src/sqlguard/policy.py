"""Guard policy: everything configurable about what the guard enforces.

A :class:`Policy` is deliberately separate from the :class:`~sqlguard.catalog.Catalog`
(facts about the warehouse) and from :class:`~sqlguard.guard.SQLGuard` (the
engine), so the same catalog can serve different policies (e.g. an internal
analyst tool vs. a customer-facing chatbot).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from sqlguard.errors import PolicyError

# Dangerous-by-default functions, per dialect. These either write state, take
# locks, sleep, read the filesystem, or open network connections.
DEFAULT_FUNCTION_DENYLISTS: dict[str, frozenset[str]] = {
    "postgres": frozenset(
        {
            "pg_sleep",
            "pg_sleep_for",
            "pg_sleep_until",
            "pg_read_file",
            "pg_read_binary_file",
            "pg_ls_dir",
            "pg_ls_waldir",
            "pg_stat_file",
            "pg_terminate_backend",
            "pg_cancel_backend",
            "pg_reload_conf",
            "pg_rotate_logfile",
            "pg_logical_emit_message",
            "pg_create_logical_replication_slot",
            "pg_drop_replication_slot",
            "pg_switch_wal",
            "pg_create_restore_point",
            "pg_promote",
            "pg_export_snapshot",
            "pg_notify",
            "lo_import",
            "lo_export",
            "lo_unlink",
            "dblink",
            "dblink_connect",
            "dblink_exec",
            "nextval",
            "setval",
            "set_config",
            "pg_advisory_lock",
            "pg_advisory_lock_shared",
            "pg_advisory_xact_lock",
            "pg_advisory_xact_lock_shared",
            "pg_try_advisory_lock",
            "pg_try_advisory_lock_shared",
        }
    ),
    "athena": frozenset(),
}

_VALID_ON_MISSING = ("ignore", "warn", "error")
_VALID_RLS_STRATEGIES = ("predicate", "subquery", "require")
_VALID_CONFLICT_MODES = ("error", "warn", "ignore")
_MASK_BUILTINS = frozenset({"null", "hash", "redact"})
_MASK_COLUMN_SENTINEL = "__sqlguard_mask_column__"
_PARAM_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@lru_cache(maxsize=256)
def _parse_rls_predicate(predicate: str, dialect: str | None) -> exp.Expression:
    try:
        statements = parse(predicate, read=dialect)
    except ParseError as exc:
        raise PolicyError(f"invalid RLS predicate: {exc}") from exc
    if len(statements) != 1:
        raise PolicyError("RLS predicate must be exactly one SQL expression")
    expression = statements[0]
    if not isinstance(expression, exp.Expression):
        raise PolicyError("RLS predicate must be exactly one SQL expression")
    if isinstance(expression, exp.Query) or any(
        isinstance(
            node,
            (exp.Query, exp.Table, exp.Command, exp.Alias, exp.AggFunc, exp.Window),
        )
        for node in expression.walk()
    ):
        raise PolicyError(
            "RLS predicate must be a row-level scalar condition without "
            "subqueries, aggregates, or windows"
        )
    columns = list(expression.find_all(exp.Column))
    if any(column.table or isinstance(column.this, exp.Star) for column in columns):
        raise PolicyError("RLS predicate columns must be unqualified and may not use *")
    placeholders = list(expression.find_all(exp.Placeholder))
    if any(not _PARAM_NAME.fullmatch(str(placeholder.name)) for placeholder in placeholders):
        raise PolicyError("RLS predicate placeholders must use named :parameter syntax")
    return expression


@dataclass
class RLSRule:
    """Row-level security rule for an equality or predicate template.

    ``table`` may be an fnmatch pattern (``"*"`` = every table that has the
    required columns). Equality rules use ``column`` and optional ``param``.
    Expression rules instead use an unqualified scalar ``predicate`` with
    named placeholders, for example ``"region IN :regions AND deleted_at IS
    NULL"``. Exactly one of ``column`` and ``predicate`` is required.
    """

    table: str
    column: str | None = None
    param: str | None = None
    schema: str | None = None
    on_missing_column: str = "error"
    predicate: str | None = None

    def __post_init__(self) -> None:
        if not self.table:
            raise PolicyError("RLSRule requires table")
        if bool(self.column) == bool(self.predicate):
            raise PolicyError("RLSRule requires exactly one of column or predicate")
        if self.predicate is not None:
            if self.param is not None:
                raise PolicyError("param is only valid for column-based RLS rules")
            self.predicate = self.predicate.strip()
            if not self.predicate:
                raise PolicyError("RLSRule predicate must be non-empty")
            _parse_rls_predicate(self.predicate, None)
        if self.on_missing_column not in ("error", "skip"):
            raise PolicyError("RLSRule.on_missing_column must be 'error' or 'skip'")

    @property
    def param_name(self) -> str:
        if self.column is None:
            raise PolicyError("expression RLS rules have multiple named parameters")
        return self.param or self.column

    @property
    def is_expression(self) -> bool:
        return self.predicate is not None

    def predicate_template(self, dialect: str) -> exp.Expression:
        if self.predicate is None:
            raise PolicyError("column-based RLS rules do not have a predicate template")
        return _parse_rls_predicate(self.predicate, dialect).copy()

    def predicate_columns(self, dialect: str) -> frozenset[str]:
        return frozenset(
            column.name for column in self.predicate_template(dialect).find_all(exp.Column)
        )

    def predicate_params(self, dialect: str) -> frozenset[str]:
        return frozenset(
            placeholder.name
            for placeholder in self.predicate_template(dialect).find_all(exp.Placeholder)
        )

    @property
    def is_pattern(self) -> bool:
        return any(ch in self.table for ch in "*?[")


@dataclass
class ColumnRule:
    """Column access rule.

    ``action="deny"``: any reference to a matching column (select list, WHERE,
    ORDER BY, ...) is an error, and the column is silently dropped from
    ``SELECT *`` expansion.

    ``action="exclude_from_star"``: the column is dropped from ``SELECT *``
    expansion but explicit references are allowed (e.g. huge blob/embedding
    columns you don't want in exploratory queries).

    Matching: fnmatch patterns on table and column names, and/or catalog tag
    membership (``tags={"pii"}`` matches columns tagged ``pii``).

    ``action="mask"`` replaces matching projected values. ``mask_with`` may
    be one of ``"null"``, ``"hash"``, or ``"redact"``, or a SQL expression
    containing ``{col}``, such as ``"left({col}, 4) || '****'"``. References
    in predicates are blocked unless ``allow_predicates=True``.
    """

    column: str = "*"
    table: str = "*"
    tags: frozenset[str] | None = None
    action: str = "deny"
    reason: str | None = None
    mask_with: str | None = None
    allow_predicates: bool = False
    _mask_template: exp.Expression | None = field(
        init=False, repr=False, compare=False, default=None
    )

    def __post_init__(self) -> None:
        if self.action not in ("deny", "exclude_from_star", "mask"):
            raise PolicyError(
                "ColumnRule.action must be 'deny', 'exclude_from_star', or 'mask'"
            )
        if self.tags is not None and not isinstance(self.tags, frozenset):
            self.tags = frozenset(self.tags)
        if self.action != "mask":
            if self.mask_with is not None:
                raise PolicyError("mask_with is only valid for action='mask'")
            if self.allow_predicates:
                raise PolicyError("allow_predicates is only valid for action='mask'")
            return
        if not isinstance(self.mask_with, str) or not self.mask_with.strip():
            raise PolicyError("ColumnRule action='mask' requires mask_with")
        self.mask_with = self.mask_with.strip()
        if self.mask_with.lower() in _MASK_BUILTINS:
            self.mask_with = self.mask_with.lower()
            return
        if "{col}" not in self.mask_with:
            raise PolicyError("custom mask_with expressions must contain {col}")
        rendered = self.mask_with.replace("{col}", _MASK_COLUMN_SENTINEL)
        try:
            statements = parse(f"SELECT {rendered}")
        except ParseError as exc:
            raise PolicyError(f"invalid mask_with expression: {exc}") from exc
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise PolicyError("mask_with must be one SQL expression")
        expressions = statements[0].expressions
        if len(expressions) != 1 or any(
            isinstance(node, exp.Query) for node in expressions[0].walk()
        ):
            raise PolicyError("mask_with must be one scalar SQL expression")
        columns = list(expressions[0].find_all(exp.Column))
        if not columns or any(column.name != _MASK_COLUMN_SENTINEL for column in columns):
            raise PolicyError("mask_with may only reference the {col} placeholder")
        self._mask_template = expressions[0]

    @property
    def mask_template(self) -> exp.Expression | None:
        """Return a defensive copy of a validated custom mask expression."""
        return self._mask_template.copy() if self._mask_template is not None else None


@dataclass
class Policy:
    """Everything the guard enforces. All limits fail closed on ERROR."""

    # Statement shape. v1 is read-only by design; anything except SELECT
    # (including set operations over SELECTs) is rejected.
    allowed_statements: tuple[str, ...] = ("select",)

    # Row-level security
    rls: Sequence[RLSRule] = ()
    rls_strategy: str = "predicate"  # predicate | subquery | require
    rls_parameterize: bool = False  # inject placeholders instead of literals
    on_conflicting_tenant_filter: str = "error"  # error | warn | ignore

    # Column-level policy
    column_rules: Sequence[ColumnRule] = ()

    # Function policy. None => per-dialect defaults; a set replaces them.
    function_denylist: frozenset[str] | None = None
    extra_function_denylist: frozenset[str] = frozenset()
    function_allowlist: frozenset[str] | None = None  # strict mode when set

    # Rewrites
    expand_star: bool = True
    default_limit: int | None = 1000
    max_limit: int | None = 10_000

    # Cost / scan safety
    max_bytes_scanned: int | None = None
    max_rows_scanned: int | None = None
    require_partition_filter: bool | None = None  # None => True on Athena
    partition_selectivity: float = 0.1
    on_missing_stats: str = "warn"  # ignore | warn | error

    # Parsed-query complexity. These cheap, pre-semantics ceilings protect the
    # validator and downstream planners from machine-generated query bombs.
    # None keeps the check off for backwards compatibility.
    max_joins: int | None = None
    max_subquery_depth: int | None = None
    max_ctes: int | None = None
    max_union_branches: int | None = None
    max_expression_nodes: int | None = None

    # Checks
    check_types: bool = True
    check_function_signatures: bool = True
    check_aggregation: bool = True
    case_insensitive_enums: bool = False
    check_joins: bool = True
    strict_joins: bool = False  # escalate join heuristics to errors
    require_declared_join_paths: bool = False

    # Enforcement. "block" (default) fails validation on any ERROR.
    # "log_only" (shadow mode) keeps semantic/policy errors as recorded
    # violations but lets the (rewritten) query through with
    # result.would_block=True, so a deployment can measure its would-block
    # rate before enforcing. Hard-stop classes — anything that leaves nothing
    # sane to execute (non-SELECT statements, parse failures, nested writes,
    # locking clauses, internal errors) — always block, even in shadow mode.
    enforcement: str = "block"  # block | log_only

    # Output
    pretty_sql: bool = False

    def __post_init__(self) -> None:
        for s in self.allowed_statements:
            if s.lower() != "select":
                raise PolicyError(
                    "v1 supports read-only SELECT policies; allowed_statements "
                    f"may only contain 'select', got {s!r}"
                )
        if self.enforcement not in ("block", "log_only"):
            raise PolicyError("enforcement must be 'block' or 'log_only'")
        if self.rls_strategy not in _VALID_RLS_STRATEGIES:
            raise PolicyError(f"rls_strategy must be one of {_VALID_RLS_STRATEGIES}")
        if self.on_conflicting_tenant_filter not in _VALID_CONFLICT_MODES:
            raise PolicyError(f"on_conflicting_tenant_filter must be one of {_VALID_CONFLICT_MODES}")
        if self.on_missing_stats not in _VALID_ON_MISSING:
            raise PolicyError(f"on_missing_stats must be one of {_VALID_ON_MISSING}")
        for limit_name in (
            "default_limit",
            "max_limit",
            "max_bytes_scanned",
            "max_rows_scanned",
            "max_joins",
            "max_subquery_depth",
            "max_ctes",
            "max_union_branches",
            "max_expression_nodes",
        ):
            v = getattr(self, limit_name)
            if v is not None and v <= 0:
                raise PolicyError(f"{limit_name} must be positive or None")
        if (
            self.default_limit is not None
            and self.max_limit is not None
            and self.default_limit > self.max_limit
        ):
            raise PolicyError("default_limit cannot exceed max_limit")
        if not (0 < self.partition_selectivity <= 1):
            raise PolicyError("partition_selectivity must be in (0, 1]")
        if self.function_denylist is not None and not isinstance(self.function_denylist, frozenset):
            self.function_denylist = frozenset(x.lower() for x in self.function_denylist)
        if not isinstance(self.extra_function_denylist, frozenset):
            self.extra_function_denylist = frozenset(x.lower() for x in self.extra_function_denylist)
        if self.function_allowlist is not None and not isinstance(self.function_allowlist, frozenset):
            self.function_allowlist = frozenset(x.lower() for x in self.function_allowlist)
        self.rls = tuple(self.rls)
        self.column_rules = tuple(self.column_rules)

    def effective_function_denylist(self, dialect: str) -> frozenset[str]:
        base = (
            self.function_denylist
            if self.function_denylist is not None
            else DEFAULT_FUNCTION_DENYLISTS.get(dialect, frozenset())
        )
        return frozenset(x.lower() for x in base) | self.extra_function_denylist

    def effective_require_partition_filter(self, dialect: str) -> bool:
        if self.require_partition_filter is not None:
            return self.require_partition_filter
        return dialect == "athena"

    @property
    def complexity_limits_enabled(self) -> bool:
        """Whether any pre-semantics query-shape ceiling is configured."""
        return any(
            value is not None
            for value in (
                self.max_joins,
                self.max_subquery_depth,
                self.max_ctes,
                self.max_union_branches,
                self.max_expression_nodes,
            )
        )
