"""Parsing and the statement gate: the first, hardest line of defense.

Everything that is not a single read-only SELECT is rejected here — DML/DDL
roots, writable CTEs (``WITH d AS (DELETE ...) SELECT ...``), ``SELECT INTO``,
locking clauses (``FOR UPDATE``), and any statement sqlglot can only represent
as an opaque ``Command`` (``VACUUM``, ``CALL``, ``UNLOAD``, ``MSCK`` ...) —
opaque means unverifiable, and unverifiable means blocked.
"""

from __future__ import annotations

from typing import cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from sqlguard.violations import Code, Severity, Violation


def _existing(*names: str) -> tuple[type, ...]:
    return tuple(t for t in (getattr(exp, n, None) for n in names) if isinstance(t, type))


# Nested write / DDL / session-state nodes. Presence anywhere in the tree is a
# violation even when the root is a SELECT.
WRITE_NODES = _existing("Insert", "Update", "Delete", "Merge")
DDL_NODES = _existing(
    "Create",
    "Drop",
    "Alter",
    "TruncateTable",
    "Grant",
    "Revoke",
    "Copy",
    "LoadData",
    "Directory",
    "Kill",
    "Pragma",
    "Attach",
    "Detach",
    "Analyze",
    "Set",
    "Use",
    "Transaction",
    "Commit",
    "Rollback",
    "Refresh",
    "Export",
    "Install",
)
COMMAND_NODES = _existing("Command")

_STATEMENT_LABELS = {
    "Insert": "INSERT",
    "Update": "UPDATE",
    "Delete": "DELETE",
    "Merge": "MERGE",
    "Create": "CREATE",
    "Drop": "DROP",
    "Alter": "ALTER",
    "TruncateTable": "TRUNCATE",
    "Grant": "GRANT",
    "Revoke": "REVOKE",
    "Copy": "COPY",
    "Set": "SET",
    "Use": "USE",
    "Transaction": "BEGIN/TRANSACTION",
    "Commit": "COMMIT",
    "Rollback": "ROLLBACK",
    "Values": "VALUES",
}


def _label(node: exp.Expression) -> str:
    return _STATEMENT_LABELS.get(type(node).__name__, type(node).__name__.upper())


def _snippet(node: exp.Expression, dialect: str, limit: int = 90) -> str:
    try:
        s = node.sql(dialect=dialect)
    except Exception:  # pragma: no cover - defensive
        s = str(node)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def parse_statement(
    sql: str, dialect: str
) -> tuple[exp.Expression | None, list[Violation]]:
    """Parse ``sql`` and require exactly one statement.

    Returns ``(root, violations)``; ``root`` is None when nothing safe to
    analyze was produced (parse failure or multiple statements).
    """
    violations: list[Violation] = []
    if not sql or not sql.strip():
        return None, [
            Violation(Code.PARSE_ERROR, Severity.ERROR, "Empty SQL input")
        ]
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except (ParseError, TokenError) as e:
        detail = str(e).split("\n", 1)[0]
        for err in getattr(e, "errors", [])[:1]:
            line, col = err.get("line"), err.get("col")
            if line is not None:
                detail += f" (line {line}, column {col})"
        return None, [
            Violation(
                Code.PARSE_ERROR,
                Severity.ERROR,
                f"SQL could not be parsed as {dialect}: {detail}",
            )
        ]
    if not statements:
        return None, [
            Violation(Code.PARSE_ERROR, Severity.ERROR, "Empty SQL input")
        ]
    if len(statements) > 1:
        return None, [
            Violation(
                Code.MULTIPLE_STATEMENTS,
                Severity.ERROR,
                f"Found {len(statements)} statements; exactly one SELECT is allowed",
                hint="Remove extra statements and any trailing SQL after the first semicolon.",
            )
        ]
    root = cast(exp.Expression, statements[0])
    # A parenthesized SELECT parses as a Subquery root; unwrap it.
    while isinstance(root, exp.Subquery):
        inner = root.unnest()
        if inner is root:  # pragma: no cover - defensive
            break
        root = cast(exp.Expression, inner)
    return root, violations


def statement_gate(root: exp.Expression, dialect: str) -> list[Violation]:
    """Reject anything that is not a pure read-only SELECT."""
    violations: list[Violation] = []

    if not isinstance(root, (exp.Select, exp.SetOperation)):
        if isinstance(root, COMMAND_NODES):
            return [
                Violation(
                    Code.DISALLOWED_COMMAND,
                    Severity.ERROR,
                    f"Unrecognized or administrative command is blocked: {_snippet(root, dialect)}",
                    hint="Only plain SELECT queries are allowed.",
                )
            ]
        return [
            Violation(
                Code.DISALLOWED_STATEMENT,
                Severity.ERROR,
                f"{_label(root)} statements are blocked; this interface is read-only",
                extra={"statement": _label(root)},
            )
        ]

    seen = set()
    for walked in root.walk():
        node = cast(exp.Expression, walked)
        if isinstance(node, WRITE_NODES + DDL_NODES):
            key = (type(node).__name__, id(node))
            if key in seen:  # pragma: no cover
                continue
            seen.add(key)
            violations.append(
                Violation(
                    Code.NESTED_WRITE,
                    Severity.ERROR,
                    f"Nested {_label(node)} is blocked (e.g. writable CTE): "
                    f"{_snippet(node, dialect)}",
                )
            )
        elif isinstance(node, COMMAND_NODES):
            violations.append(
                Violation(
                    Code.DISALLOWED_COMMAND,
                    Severity.ERROR,
                    f"Unrecognized command fragment is blocked: {_snippet(node, dialect)}",
                )
            )
        elif isinstance(node, exp.Into):
            violations.append(
                Violation(
                    Code.SELECT_INTO,
                    Severity.ERROR,
                    "SELECT INTO creates a table and is blocked; use a plain SELECT",
                )
            )
        elif isinstance(node, exp.Lock):
            violations.append(
                Violation(
                    Code.LOCKING_CLAUSE,
                    Severity.ERROR,
                    "Locking clauses (FOR UPDATE / FOR SHARE) are blocked on a read-only path",
                )
            )
    return violations


def _function_names(node: exp.Expression) -> list[str]:
    """Candidate lowercase names a function node might be known by."""
    names = []
    if isinstance(node, exp.Anonymous):
        raw = node.name
        if raw:
            names.append(raw.lower())
    elif isinstance(node, exp.Func):
        try:
            names.append(node.sql_name().lower())
        except Exception:  # pragma: no cover - defensive
            pass
        names.append(type(node).__name__.lower())
    return names


def function_gate(
    root: exp.Expression,
    denylist: frozenset[str],
    allowlist: frozenset[str] | None,
    dialect: str,
) -> list[Violation]:
    """Enforce the function deny/allow lists."""
    violations: list[Violation] = []
    reported = set()
    for found in root.find_all(exp.Func):
        node = cast(exp.Expression, found)
        names = _function_names(node)
        if not names:
            continue
        display = names[0]
        hit = [n for n in names if n in denylist]
        if hit:
            if display not in reported:
                reported.add(display)
                violations.append(
                    Violation(
                        Code.FORBIDDEN_FUNCTION,
                        Severity.ERROR,
                        f"Function {display}() is blocked by policy",
                        extra={"function": display},
                    )
                )
            continue
        if allowlist is not None and not any(n in allowlist for n in names):
            if display not in reported:
                reported.add(display)
                violations.append(
                    Violation(
                        Code.FORBIDDEN_FUNCTION,
                        Severity.ERROR,
                        f"Function {display}() is not on the allowlist",
                        hint="Allowed functions: " + ", ".join(sorted(allowlist)[:20]),
                        extra={"function": display},
                    )
                )
    return violations
