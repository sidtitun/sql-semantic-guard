"""Guard policy: everything configurable about what the guard enforces.

A :class:`Policy` is deliberately separate from the :class:`~sqlguard.catalog.Catalog`
(facts about the warehouse) and from :class:`~sqlguard.guard.SQLGuard` (the
engine), so the same catalog can serve different policies (e.g. an internal
analyst tool vs. a customer-facing chatbot).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

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


@dataclass
class RLSRule:
    """Row-level security rule: constrain ``table`` by ``column = <param>``.

    ``table`` may be an fnmatch pattern (``"*"`` = every table that has the
    column). ``param`` names the key looked up in ``validate(..., params=...)``
    and defaults to the column name. ``on_missing_column`` controls what
    happens when a matched table lacks the column: ``"error"`` fails closed
    (recommended for exact table names), ``"skip"`` ignores that table
    (useful with wildcard patterns).
    """

    table: str
    column: str
    param: str | None = None
    schema: str | None = None
    on_missing_column: str = "error"

    def __post_init__(self) -> None:
        if not self.table or not self.column:
            raise PolicyError("RLSRule requires both table and column")
        if self.on_missing_column not in ("error", "skip"):
            raise PolicyError("RLSRule.on_missing_column must be 'error' or 'skip'")

    @property
    def param_name(self) -> str:
        return self.param or self.column

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
    """

    column: str = "*"
    table: str = "*"
    tags: frozenset[str] | None = None
    action: str = "deny"
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.action not in ("deny", "exclude_from_star"):
            raise PolicyError("ColumnRule.action must be 'deny' or 'exclude_from_star'")
        if self.tags is not None and not isinstance(self.tags, frozenset):
            self.tags = frozenset(self.tags)


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

    # Checks
    check_types: bool = True
    check_joins: bool = True
    strict_joins: bool = False  # escalate join heuristics to errors

    # Output
    pretty_sql: bool = False

    def __post_init__(self) -> None:
        for s in self.allowed_statements:
            if s.lower() != "select":
                raise PolicyError(
                    "v1 supports read-only SELECT policies; allowed_statements "
                    f"may only contain 'select', got {s!r}"
                )
        if self.rls_strategy not in _VALID_RLS_STRATEGIES:
            raise PolicyError(f"rls_strategy must be one of {_VALID_RLS_STRATEGIES}")
        if self.on_conflicting_tenant_filter not in _VALID_CONFLICT_MODES:
            raise PolicyError(f"on_conflicting_tenant_filter must be one of {_VALID_CONFLICT_MODES}")
        if self.on_missing_stats not in _VALID_ON_MISSING:
            raise PolicyError(f"on_missing_stats must be one of {_VALID_ON_MISSING}")
        for limit_name in ("default_limit", "max_limit", "max_bytes_scanned", "max_rows_scanned"):
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
