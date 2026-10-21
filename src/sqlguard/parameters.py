"""Render driver bindings from AST placeholders, in emitted SQL order."""
from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any

from sqlglot import exp


class ParameterError(ValueError):
    """A query cannot be paired with a complete set of driver parameters."""


def render_parameters(
    tree: exp.Expression, dialect: str, style: str, params: Mapping[str, Any], pretty: bool
) -> tuple[str, dict[str, Any] | list[Any] | None]:
    # Sentinels are introduced only at actual AST bind nodes, never by searching
    # user SQL for ':name'. Determine ordering from SQL, not AST walk order.
    original = tree.sql(dialect=dialect, pretty=pretty)
    prefix = "sqlguard_bind_" + uuid.uuid4().hex + "_"
    while prefix in original:
        prefix = "sqlguard_bind_" + uuid.uuid4().hex + "_"
    bindings: dict[str, Any] = {}

    def replace(node: exp.Expression) -> exp.Expression:
        if "sqlguard_bind_value" in node.meta:
            value = node.meta["sqlguard_bind_value"]
        elif isinstance(node, exp.Placeholder):
            name = node.name
            if not name or name not in params:
                raise ParameterError("A named SQL parameter is missing; supply every bind value")
            value = params[name]
        elif isinstance(node, exp.Parameter):
            raise ParameterError("Use named input placeholders, not native positional parameters")
        else:
            return node
        marker = f"{prefix}{len(bindings)}"
        bindings[marker] = value
        return exp.Var(this=marker)

    rendered = tree.transform(replace, copy=True).sql(dialect=dialect, pretty=pretty)
    if not bindings:
        return rendered, None
    ordered: list[Any] = []
    named: dict[str, Any] = {}

    def emit(match: re.Match[str]) -> str:
        marker = match.group()
        ordered.append(bindings[marker])
        name = f"p{len(ordered)}"
        named[name] = bindings[marker]
        if style == "qmark":
            return "?"
        if style == "numeric":
            return f":{len(ordered)}"
        if style == "named":
            return f":{name}"
        return f"%({name})s"

    # DB-API pyformat requires escaping non-placeholder percent signs too.
    if style == "pyformat":
        rendered = rendered.replace("%", "%%")
    rendered = re.sub(re.escape(prefix) + r"\d+", emit, rendered)
    return rendered, ordered if style in {"qmark", "numeric"} else named
