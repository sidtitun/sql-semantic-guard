"""SQLGuard: the validation pipeline that ties every check together.

Pipeline (each stage appends structured violations; ERROR blocks execution)::

    parse -> statement gate -> function gate -> name binding -> qualification
          -> type checks -> column policy -> row-level security -> limits
          -> join sanity -> partition filters -> cost estimation -> render

The guard is stateless per call and thread-safe: build one per (catalog,
policy, dialect) and share it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

from sqlguard import analyzer, cost, rewrite, rls, semantics
from sqlguard.catalog import Catalog, CatalogIndex, match_table
from sqlguard.errors import PolicyError, ValidationFailed
from sqlguard.policy import Policy
from sqlguard.scopeindex import ScopeIndex
from sqlguard.violations import (
    Code,
    QueryStats,
    Rewrite,
    RewriteKind,
    Severity,
    ValidationResult,
    Violation,
)

logger = logging.getLogger("sqlguard")

_DIALECT_ALIASES = {
    "postgresql": "postgres",
    "psql": "postgres",
    "pg": "postgres",
    "awsathena": "athena",
}

# Violations that leave nothing sane to execute; they block even under
# Policy(enforcement="log_only") shadow mode.
_HARD_STOP_CODES = frozenset(
    {
        Code.PARSE_ERROR,
        Code.MULTIPLE_STATEMENTS,
        Code.DISALLOWED_STATEMENT,
        Code.DISALLOWED_COMMAND,
        Code.NESTED_WRITE,
        Code.SELECT_INTO,
        Code.LOCKING_CLAUSE,
        Code.INTERNAL_ERROR,
    }
)


class SQLGuard:
    """Validate, constrain, and rewrite LLM-generated SQL before execution.

    Args:
        catalog: the tables the query is allowed to touch (ground truth).
        policy: what to enforce; defaults to a sane read-only policy.
        dialect: ``"postgres"`` or ``"athena"`` (any sqlglot dialect parses,
            but these two are what the test suite pins down).
        estimators: cost estimators to run, in order. Defaults to the
            catalog-statistics heuristic. Estimators after the first only run
            when the query has no blocking errors (so e.g. EXPLAIN-based
            estimators never see invalid SQL).
    """

    def __init__(
        self,
        catalog: Catalog,
        policy: Policy | None = None,
        dialect: str = "postgres",
        estimators: Sequence[cost.CostEstimator] | None = None,
    ) -> None:
        raw = (dialect or "postgres").lower().strip()
        self.dialect = _DIALECT_ALIASES.get(raw, raw)
        try:
            Dialect.get_or_raise(self.dialect)
        except Exception as e:
            raise PolicyError(f"Unknown SQL dialect {dialect!r}: {e}") from e
        self.catalog = catalog
        self.policy = policy or Policy()
        self.index = CatalogIndex(catalog, self.dialect)
        self.estimators: list[cost.CostEstimator] = (
            list(estimators) if estimators is not None else [cost.HeuristicCostEstimator()]
        )
        self._validate_rls_config()

    # ------------------------------------------------------------------ #

    def _validate_rls_config(self) -> None:
        """Surface RLS misconfiguration at construction, not per query."""
        for rule in self.policy.rls:
            matched = [
                t
                for t in self.catalog.tables
                if match_table(rule.table, t, self.index, rule.schema)
            ]
            if not rule.is_pattern and not matched:
                raise PolicyError(
                    f"RLS rule targets table {rule.table!r} which is not in the catalog"
                )
            if rule.on_missing_column == "error":
                missing = [t.display_name for t in matched if t.column(rule.column) is None]
                if missing:
                    raise PolicyError(
                        f"RLS rule column {rule.column!r} missing on matched table(s): "
                        + ", ".join(sorted(missing)[:5])
                        + "; fix the rule or set on_missing_column='skip'"
                    )

    # ------------------------------------------------------------------ #

    def validate(
        self, sql: str, params: Mapping[str, Any] | None = None
    ) -> ValidationResult:
        """Validate one statement. Never raises for problems *in the SQL*."""
        try:
            return self._validate(sql, dict(params or {}))
        except Exception as e:  # pragma: no cover - safety net
            logger.exception("sqlguard internal error while validating")
            return ValidationResult(
                valid=False,
                sql=None,
                original_sql=sql,
                dialect=self.dialect,
                violations=[
                    Violation(
                        Code.INTERNAL_ERROR,
                        Severity.ERROR,
                        f"Validator error ({type(e).__name__}); failing closed",
                        hint="Simplify the query or report this as a sqlguard bug.",
                    )
                ],
            )

    def validate_or_raise(
        self, sql: str, params: Mapping[str, Any] | None = None
    ) -> ValidationResult:
        """Like :meth:`validate`, raising :class:`ValidationFailed` on errors."""
        result = self.validate(sql, params)
        if not result.valid:
            raise ValidationFailed(result)
        return result

    # ------------------------------------------------------------------ #

    def _validate(self, sql: str, params: dict[str, Any]) -> ValidationResult:
        policy, dialect, index = self.policy, self.dialect, self.index
        violations: list[Violation] = []
        rewrites: list[Rewrite] = []
        stats = QueryStats()
        run, skipped = stats.checks_run, stats.checks_skipped
        all_checks = [
            "statement_gate",
            "function_policy",
            "name_binding",
            "qualification",
            "type_checks",
            "column_policy",
            "row_level_security",
            "limit_enforcement",
            "join_checks",
            "partition_filters",
            "cost_estimation",
            "output_audit",
        ]

        def finalize(tree: exp.Expression | None) -> ValidationResult:
            has_errors = any(v.is_error for v in violations)
            valid = not has_errors
            would_block = False
            if (
                has_errors
                and policy.enforcement == "log_only"
                and tree is not None
                and not any(v.code in _HARD_STOP_CODES for v in violations if v.is_error)
            ):
                # Shadow mode: record everything, block nothing blockable-only.
                # Rewrites (RLS, limits, column drops) were still applied — the
                # query runs protected; would_block carries the truth for
                # measurement. Hard-stop classes never reach here (tree is
                # None or their codes match).
                valid = True
                would_block = True
            out_sql = None
            if valid and tree is not None:
                out_sql = tree.sql(dialect=dialect, pretty=policy.pretty_sql)

                # Treat the renderer as a security boundary. Parser recovery can
                # occasionally build an AST whose rendered form is not valid SQL;
                # never return such output as executable, even in shadow mode.
                run.append("output_audit")
                audit_root, audit_violations = analyzer.parse_statement(out_sql, dialect)
                audit_gate = (
                    analyzer.statement_gate(audit_root, dialect)
                    if audit_root is not None and not audit_violations
                    else []
                )
                if audit_root is None or audit_violations or audit_gate:
                    violations.append(
                        Violation(
                            Code.INTERNAL_ERROR,
                            Severity.ERROR,
                            "Rendered SQL failed the final read-only safety audit; failing closed",
                            hint="Regenerate the query or report this as a sqlguard bug.",
                        )
                    )
                    valid = False
                    would_block = False
                    out_sql = None

            skipped.extend(c for c in all_checks if c not in run and c not in skipped)
            return ValidationResult(
                valid=valid,
                sql=out_sql,
                original_sql=sql,
                dialect=dialect,
                violations=violations,
                rewrites=rewrites,
                stats=stats,
                would_block=would_block,
            )

        # 1. parse ---------------------------------------------------------
        run.append("parse")
        root, parse_violations = analyzer.parse_statement(sql, dialect)
        violations.extend(parse_violations)
        if root is None:
            return finalize(None)
        stats.statement = type(root).__name__.lower()

        # 2. statement gate --------------------------------------------------
        run.append("statement_gate")
        gate = analyzer.statement_gate(root, dialect)
        violations.extend(gate)
        if gate:
            # Not a plain SELECT: nothing downstream is meaningful or safe.
            return finalize(None)

        # 3. function policy -------------------------------------------------
        run.append("function_policy")
        violations.extend(
            analyzer.function_gate(
                root,
                policy.effective_function_denylist(dialect),
                policy.function_allowlist,
                dialect,
            )
        )

        # 4. name binding -----------------------------------------------------
        run.append("name_binding")
        bind = semantics.bind_names(root, index)
        violations.extend(bind.violations)
        stats.tables = bind.tables
        binding_failed = any(v.is_error for v in bind.violations)

        # 5. qualification (star expansion, canonical names) -------------------
        tree: exp.Expression = root
        qualified = False
        if binding_failed:
            skipped.extend(["qualification", "type_checks"])
        else:
            run.append("qualification")
            had_star = semantics.mark_star_selects(tree) > 0
            tree, qualified, qv = semantics.qualify_tree(tree, index, policy, dialect)
            if qv is not None:
                violations.append(qv)
            if qualified and had_star and policy.expand_star:
                rewrites.append(
                    Rewrite(
                        RewriteKind.STAR_EXPANDED,
                        "Expanded SELECT * to explicit columns",
                    )
                )

        # 6. type checks --------------------------------------------------------
        if qualified and policy.check_types:
            run.append("type_checks")
            violations.extend(semantics.check_types(tree, index, dialect))
        elif not binding_failed and not policy.check_types:
            skipped.append("type_checks")

        # Shared scope index: built once here, reused by every remaining stage.
        # Predicate/limit/projection mutations don't change source topology;
        # apply_rls invalidates it on the one mutation that does (subquery wraps).
        scope_index = ScopeIndex(tree, index)

        # 7. column policy --------------------------------------------------------
        run.append("column_policy")
        col_violations, col_rewrites = rewrite.apply_column_rules(
            tree, index, policy, dialect, scope_index=scope_index
        )
        violations.extend(col_violations)
        rewrites.extend(col_rewrites)

        # 8. row-level security ------------------------------------------------
        run.append("row_level_security")
        rls_violations, rls_rewrites = rls.apply_rls(
            tree, index, policy, params, dialect, scope_index=scope_index
        )
        violations.extend(rls_violations)
        rewrites.extend(rls_rewrites)

        # 9. limit enforcement ---------------------------------------------------
        run.append("limit_enforcement")
        new_tree, limit_rewrites = rewrite.enforce_limit(tree, policy)
        rewrites.extend(limit_rewrites)
        if new_tree is not tree:  # defensive: builders normally mutate in place
            tree = new_tree
            scope_index = ScopeIndex(tree, index)

        # 10. join sanity ---------------------------------------------------------
        if policy.check_joins:
            run.append("join_checks")
            violations.extend(
                cost.check_joins(tree, index, policy, dialect, scope_index=scope_index)
            )
        else:
            skipped.append("join_checks")

        # 11. partition filters ------------------------------------------------
        run.append("partition_filters")
        part_violations, part_flags = cost.check_partition_filters(
            tree, index, policy, dialect, scope_index=scope_index
        )
        violations.extend(part_violations)

        # 12. cost estimation ---------------------------------------------------
        run.append("cost_estimation")
        inputs = cost.EstimateInputs(
            tree=tree,
            index=index,
            policy=policy,
            dialect=dialect,
            partition_flags=part_flags,
            scope_index=scope_index,
        )
        rendered = tree.sql(dialect=dialect)
        for i, estimator in enumerate(self.estimators):
            if i > 0 and any(v.is_error for v in violations):
                break  # secondary estimators (e.g. EXPLAIN) need runnable SQL
            estimate, est_violations = estimator.estimate(rendered, inputs)
            violations.extend(est_violations)
            if estimate is not None:
                stats.cost = estimate

        # stats: referenced columns on the final tree -----------------------
        referenced = semantics.collect_referenced_columns(
            tree, index, scope_index=scope_index
        )
        for (schema, name), cols in sorted(referenced.items()):
            display = f"{schema}.{name}" if schema else name
            stats.referenced_columns[display] = (
                ["*"] if cols is semantics.ALL_COLUMNS else sorted(cols or ())
            )

        return finalize(tree)

    # ------------------------------------------------------------------ #

    def policy_prompt(self, max_tables: int = 50) -> str:
        """A schema+rules block to include in the SQL-generation prompt.

        Prevention beats repair: telling the model the real schema and the
        house rules up front cuts violation rates dramatically.
        """
        lines: list[str] = [f"You are writing {self.dialect} SQL. Available tables:"]
        for t in self.catalog.tables[:max_tables]:
            cols = ", ".join(f"{c.name} {c.type}" for c in t.columns)
            extras = ""
            if t.partition_columns:
                extras = f" [partitioned by {', '.join(t.partition_columns)}]"
            lines.append(f"- {t.display_name}({cols}){extras}")
        if len(self.catalog.tables) > max_tables:
            lines.append(f"- ... and {len(self.catalog.tables) - max_tables} more tables")

        lines.append("Rules:")
        lines.append("- Write exactly one read-only SELECT statement. No writes, DDL, or commands.")
        lines.append("- Only reference the tables and columns listed above.")
        if self.policy.rls:
            cols = sorted({r.column for r in self.policy.rls})
            lines.append(
                f"- Row-level security on {', '.join(cols)} is applied automatically; "
                "do not add those filters yourself."
            )
        if self.policy.default_limit:
            lines.append(
                f"- Results are capped at {self.policy.default_limit} rows unless you "
                "specify a smaller LIMIT."
            )
        denied = [r for r in self.policy.column_rules if r.action == "deny"]
        if denied:
            descriptions: list[str] = []
            for r in denied:
                if r.tags:
                    descriptions.append("any column tagged " + "/".join(sorted(r.tags)))
                elif r.column != "*":
                    scope = "" if r.table == "*" else f" on {r.table}"
                    descriptions.append(f"{r.column}{scope}")
            if not descriptions:
                # Concrete restricted columns resolved from the catalog.
                concrete = self._resolve_denied_columns(denied)
                descriptions.extend(concrete)
            if descriptions:
                lines.append(
                    "- Never reference these restricted columns: "
                    + ", ".join(dict.fromkeys(descriptions))
                )
        parted = [t for t in self.catalog.tables if t.partition_columns]
        if parted and self.policy.effective_require_partition_filter(self.dialect):
            lines.append(
                "- Always filter partitioned tables on a partition column "
                "(otherwise the query is rejected)."
            )
        return "\n".join(lines)

    def _resolve_denied_columns(self, denied) -> list[str]:
        """Concrete ``table.column`` names matched by wildcard/tag deny rules."""
        from sqlguard.catalog import match_table
        from sqlguard.rewrite import _rule_matches

        names: list[str] = []
        for t in self.catalog.tables:
            for c in t.columns:
                for r in denied:
                    if r.table != "*" and not match_table(r.table, t, self.index):
                        continue
                    if _rule_matches(r, self.index, t, c.name, c.tags):
                        names.append(f"{t.name}.{c.name}")
                        break
        return names

    # ------------------------------------------------------------------ #

    @classmethod
    def from_database(
        cls,
        db_url: str,
        schemas: Sequence[str] | None = None,
        dialect: str | None = None,
        policy: Policy | None = None,
        include_stats: bool = True,
        estimators: Sequence[cost.CostEstimator] | None = None,
        **policy_kwargs: Any,
    ) -> SQLGuard:
        """Reflect a live database into a catalog and build a guard.

        Requires the ``sqlalchemy`` extra: ``pip install sql-semantic-guard[postgres]``.
        Extra keyword arguments are passed through to :class:`Policy`.
        """
        try:
            from sqlalchemy import create_engine
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "SQLGuard.from_database requires SQLAlchemy: "
                "pip install 'sql-semantic-guard[postgres]'"
            ) from e
        from sqlguard.reflect import catalog_from_sqlalchemy

        engine = create_engine(db_url)
        catalog = catalog_from_sqlalchemy(engine, schemas=schemas, include_stats=include_stats)
        if dialect is None:
            dialect = engine.dialect.name
        if policy is None and policy_kwargs:
            policy = Policy(**policy_kwargs)
        return cls(catalog=catalog, policy=policy, dialect=dialect, estimators=estimators)
