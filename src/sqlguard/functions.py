"""Dialect-aware SQL function signatures used by deterministic validation.

Keys use sqlglot's normalized ``sql_name`` because one expression can render
with different surface names in different dialects.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Final


@dataclass(frozen=True)
class Signature:
    """One accepted arity for a SQL function."""

    parameters: tuple[str, ...]
    min_args: int
    max_args: int | None

    def accepts(self, count: int) -> bool:
        return count >= self.min_args and (self.max_args is None or count <= self.max_args)

    def render(self, function: str) -> str:
        if self.max_args is None:
            arguments = ", ".join(self.parameters)
        else:
            arguments = ", ".join(self.parameters[: self.min_args])
            arguments += "".join(
                f"[, {parameter}]" for parameter in self.parameters[self.min_args :]
            )
        return f"{function}({arguments})"


@dataclass(frozen=True)
class FunctionSpec:
    """Public function name and accepted overloads."""

    name: str
    signatures: tuple[Signature, ...]


def exact(*parameters: str) -> Signature:
    count = len(parameters)
    return Signature(parameters, count, count)


def optional(*parameters: str, required: int) -> Signature:
    return Signature(parameters, required, len(parameters))


def variadic(*parameters: str, required: int) -> Signature:
    return Signature(parameters, required, None)


SHARED_SIGNATURES: Final[Mapping[str, FunctionSpec]] = {
    "abs": FunctionSpec("abs", (exact("number"),)),
    "avg": FunctionSpec("avg", (exact("expression"),)),
    "ceil": FunctionSpec("ceil", (exact("number"),)),
    "ceiling": FunctionSpec("ceiling", (exact("number"),)),
    "coalesce": FunctionSpec("coalesce", (variadic("expression", "...", required=1),)),
    "count": FunctionSpec("count", (exact("expression | *"),)),
    "exp": FunctionSpec("exp", (exact("number"),)),
    "floor": FunctionSpec("floor", (exact("number"),)),
    "length": FunctionSpec("length", (exact("string"),)),
    "ln": FunctionSpec("ln", (exact("number"),)),
    "log": FunctionSpec("log", (optional("number", "base", required=1),)),
    "lower": FunctionSpec("lower", (exact("string"),)),
    "ltrim": FunctionSpec("ltrim", (optional("string", "characters", required=1),)),
    "max": FunctionSpec("max", (exact("expression"),)),
    "min": FunctionSpec("min", (exact("expression"),)),
    "nullif": FunctionSpec("nullif", (exact("left", "right"),)),
    "power": FunctionSpec("power", (exact("number", "exponent"),)),
    "regexp_like": FunctionSpec(
        "regexp_like", (optional("string", "pattern", "flags", required=2),)
    ),
    "replace": FunctionSpec("replace", (exact("string", "from", "to"),)),
    "round": FunctionSpec("round", (optional("number", "decimals", required=1),)),
    "rtrim": FunctionSpec("rtrim", (optional("string", "characters", required=1),)),
    "sqrt": FunctionSpec("sqrt", (exact("number"),)),
    "substring": FunctionSpec("substring", (optional("string", "start", "length", required=2),)),
    "sum": FunctionSpec("sum", (exact("expression"),)),
    "upper": FunctionSpec("upper", (exact("string"),)),
}


POSTGRES_SIGNATURES: Final[Mapping[str, FunctionSpec]] = {
    "split_part": FunctionSpec("split_part", (exact("string", "delimiter", "field"),)),
    "str_to_date": FunctionSpec("to_date", (exact("string", "format"),)),
    "time_to_str": FunctionSpec("to_char", (exact("value", "format"),)),
    "timestamp_trunc": FunctionSpec(
        "date_trunc", (optional("unit", "timestamp", "time_zone", required=2),)
    ),
}


ATHENA_SIGNATURES: Final[Mapping[str, FunctionSpec]] = {
    "array_size": FunctionSpec("cardinality", (exact("array | map"),)),
    "array_to_string": FunctionSpec(
        "array_join", (optional("array", "delimiter", "null_replacement", required=2),)
    ),
    "date_add": FunctionSpec("date_add", (exact("unit", "value", "date"),)),
    "datediff": FunctionSpec("date_diff", (exact("unit", "start", "end"),)),
    "format_datetime": FunctionSpec(
        "format_datetime", (exact("timestamp", "format"),)
    ),
    "json_extract": FunctionSpec("json_extract", (exact("json", "path"),)),
    "json_extract_scalar": FunctionSpec("json_extract_scalar", (exact("json", "path"),)),
    "max": FunctionSpec("max", (optional("expression", "n", required=1),)),
    "min": FunctionSpec("min", (optional("expression", "n", required=1),)),
    "parse_datetime": FunctionSpec("parse_datetime", (exact("string", "format"),)),
    "regexp_replace": FunctionSpec(
        "regexp_replace", (optional("string", "pattern", "replacement", required=2),)
    ),
    "split_part": FunctionSpec("split_part", (exact("string", "delimiter", "index"),)),
    "str_position": FunctionSpec("strpos", (exact("string", "substring"),)),
    "str_to_time": FunctionSpec("date_parse", (exact("string", "format"),)),
    "time_to_str": FunctionSpec("date_format", (exact("timestamp", "format"),)),
    "timestamp_trunc": FunctionSpec("date_trunc", (exact("unit", "value"),)),
}


_DIALECT_SIGNATURES: Final[Mapping[str, Mapping[str, FunctionSpec]]] = {
    "postgres": POSTGRES_SIGNATURES,
    "athena": ATHENA_SIGNATURES,
    "trino": ATHENA_SIGNATURES,
}


@cache
def signature_registry(dialect: str) -> Mapping[str, FunctionSpec]:
    """Return shared signatures overlaid with dialect-specific definitions."""
    registry = dict(SHARED_SIGNATURES)
    registry.update(_DIALECT_SIGNATURES.get(dialect, {}))
    return registry
