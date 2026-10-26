"""Typed query intent and deterministic checks against the final SQL AST.

This module deliberately does not interpret natural language.  An application,
planner, or human supplies :class:`QueryIntent`; SQLGuard checks the SQL against
that independent contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlglot import exp

from sqlguard.catalog import CatalogIndex
from sqlguard.violations import Code, IntentCheckResult, Severity, Violation

_AGGREGATIONS = frozenset({"avg", "count", "max", "min", "sum"})
_FILTER_OPERATORS = frozenset(
    {"eq", "neq", "gt", "gte", "lt", "lte", "in", "between", "is_null", "not_null"}
)
_TIME_GRAINS = frozenset({"year", "quarter", "month", "week", "day", "hour"})
_DIRECTIONS = frozenset({"asc", "desc"})


def _clean_identifier(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must be a non-empty string")
    return cleaned


@dataclass(frozen=True)
class MeasureIntent:
    """One requested aggregate measure over a physical column.

    ``column=None`` is valid for ``COUNT(*)``. ``alias`` may identify the
    expected output name and is also usable by :class:`OrderIntent`.
    """

    aggregation: str
    column: str | None = None
    table: str | None = None
    alias: str | None = None
    distinct: bool | None = None

    def __post_init__(self) -> None:
        aggregation = self.aggregation.lower().strip()
        if aggregation not in _AGGREGATIONS:
            raise ValueError(f"aggregation must be one of {sorted(_AGGREGATIONS)}")
        object.__setattr__(self, "aggregation", aggregation)
        if self.column is not None:
            object.__setattr__(self, "column", _clean_identifier(self.column, "column"))
        if self.table is not None:
            object.__setattr__(self, "table", _clean_identifier(self.table, "table"))
        if self.alias is not None:
            object.__setattr__(self, "alias", _clean_identifier(self.alias, "alias"))
        if self.column is None and aggregation != "count":
            raise ValueError("only count may omit its column (COUNT(*))")


@dataclass(frozen=True)
class DimensionIntent:
    """One requested projected/grouped dimension."""

    column: str
    table: str | None = None
    alias: str | None = None
    time_grain: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "column", _clean_identifier(self.column, "column"))
        if self.table is not None:
            object.__setattr__(self, "table", _clean_identifier(self.table, "table"))
        if self.alias is not None:
            object.__setattr__(self, "alias", _clean_identifier(self.alias, "alias"))
        if self.time_grain is not None:
            grain = self.time_grain.lower().strip()
            if grain not in _TIME_GRAINS:
                raise ValueError(f"time_grain must be one of {sorted(_TIME_GRAINS)}")
            object.__setattr__(self, "time_grain", grain)


@dataclass(frozen=True)
class FilterIntent:
    """A required filter. Values are compared but excluded from diagnostics."""

    column: str
    operator: str = "eq"
    values: Sequence[Any] = ()
    table: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "column", _clean_identifier(self.column, "column"))
        if self.table is not None:
            object.__setattr__(self, "table", _clean_identifier(self.table, "table"))
        operator = self.operator.lower().strip()
        if operator not in _FILTER_OPERATORS:
            raise ValueError(f"operator must be one of {sorted(_FILTER_OPERATORS)}")
        object.__setattr__(self, "operator", operator)
        if isinstance(self.values, (str, bytes)):
            raise TypeError("values must be a sequence; wrap a scalar in a tuple")
        values = tuple(self.values)
        expected = 0 if operator in {"is_null", "not_null"} else 2 if operator == "between" else 1
        if operator == "in":
            if not values:
                raise ValueError("in filters require at least one value")
        elif len(values) != expected:
            raise ValueError(f"{operator} filters require {expected} value(s)")
        object.__setattr__(self, "values", values)


@dataclass(frozen=True)
class OrderIntent:
    """Required output ordering by projection alias or physical column."""

    field: str
    direction: str = "asc"

    def __post_init__(self) -> None:
        object.__setattr__(self, "field", _clean_identifier(self.field, "field"))
        direction = self.direction.lower().strip()
        if direction not in _DIRECTIONS:
            raise ValueError(f"direction must be one of {sorted(_DIRECTIONS)}")
        object.__setattr__(self, "direction", direction)


@dataclass(frozen=True)
class QueryIntent:
    """An independently produced contract for what a query must answer."""

    measures: Sequence[MeasureIntent] = ()
    dimensions: Sequence[DimensionIntent] = ()
    filters: Sequence[FilterIntent] = ()
    ordering: Sequence[OrderIntent] = ()
    distinct: bool | None = None
    limit: int | None = None
    expected_grain: Sequence[str] = ()

    def __post_init__(self) -> None:
        for name, expected_type in (
            ("measures", MeasureIntent),
            ("dimensions", DimensionIntent),
            ("filters", FilterIntent),
            ("ordering", OrderIntent),
        ):
            values = tuple(getattr(self, name))
            if not all(isinstance(value, expected_type) for value in values):
                raise TypeError(f"{name} must contain only {expected_type.__name__} values")
            object.__setattr__(self, name, values)
        if self.limit is not None and self.limit <= 0:
            raise ValueError("limit must be positive or None")
        if isinstance(self.expected_grain, (str, bytes)):
            raise TypeError("expected_grain must be a sequence of field names")
        grain = tuple(_clean_identifier(value, "expected_grain item") for value in self.expected_grain)
        object.__setattr__(self, "expected_grain", grain)

    def to_dict(self, *, include_values: bool = False) -> dict[str, Any]:
        """Serialize the contract; filter values remain private by default."""
        filters: list[dict[str, Any]] = []
        for item in self.filters:
            serialized: dict[str, Any] = {
                "table": item.table,
                "column": item.column,
                "operator": item.operator,
                "value_count": len(item.values),
            }
            if include_values:
                serialized["values"] = list(item.values)
            filters.append(serialized)
        return {
            "measures": [
                {
                    "aggregation": item.aggregation,
                    "table": item.table,
                    "column": item.column,
                    "alias": item.alias,
                    "distinct": item.distinct,
                }
                for item in self.measures
            ],
            "dimensions": [
                {
                    "table": item.table,
                    "column": item.column,
                    "alias": item.alias,
                    "time_grain": item.time_grain,
                }
                for item in self.dimensions
            ],
            "filters": filters,
            "ordering": [
                {"field": item.field, "direction": item.direction} for item in self.ordering
            ],
            "distinct": self.distinct,
            "limit": self.limit,
            "expected_grain": list(self.expected_grain),
        }


@dataclass(frozen=True)
class _ObservedMeasure:
    aggregation: str
    table: str | None
    column: str | None
    alias: str | None
    distinct: bool


@dataclass(frozen=True)
class _ObservedDimension:
    table: str | None
    column: str
    alias: str | None
    time_grain: str | None
    grouped: bool


@dataclass(frozen=True)
class _ObservedFilter:
    table: str | None
    column: str
    operator: str
    values: tuple[Any, ...]


def _norm(value: str | None, index: CatalogIndex) -> str | None:
    return index.normalize(value) if value else None


def _column(
    expr: exp.Expression,
    index: CatalogIndex,
    table_aliases: Mapping[str, str] | None = None,
) -> tuple[str | None, str] | None:
    node = expr if isinstance(expr, exp.Column) else next(expr.find_all(exp.Column), None)
    if not isinstance(node, exp.Column) or isinstance(node.this, exp.Star):
        return None
    table = _norm(node.table, index)
    if table is not None and table_aliases is not None:
        table = table_aliases.get(table, table)
    return table, index.normalize(node.name)


def _table_aliases(select: exp.Select, index: CatalogIndex) -> dict[str, str]:
    """Map outer-scope SQL aliases to catalog table identities."""
    sources: list[exp.Expression] = []
    from_ = select.args.get("from_")
    if isinstance(from_, exp.From) and isinstance(from_.this, exp.Expression):
        sources.append(from_.this)
    sources.extend(
        join.this
        for join in select.args.get("joins") or []
        if isinstance(join, exp.Join) and isinstance(join.this, exp.Expression)
    )
    aliases: dict[str, str] = {}
    for source in sources:
        if not isinstance(source, exp.Table):
            continue
        catalog_table, _ = index.resolve(source.name, source.db)
        if catalog_table is None:
            continue
        identity = index.normalize(catalog_table.display_name)
        aliases[index.normalize(source.alias_or_name)] = identity
        aliases[identity] = identity
    return aliases


def _field_matches(
    table: str | None, column: str | None, expected_table: str | None, expected_column: str | None,
    index: CatalogIndex,
) -> bool:
    if column is None or expected_column is None:
        return column is None and expected_column is None
    if column != index.normalize(expected_column):
        return False
    return expected_table is None or table == index.normalize(expected_table)


def _literal_value(node: exp.Expression, params: Mapping[str, Any]) -> tuple[bool, Any]:
    while isinstance(node, (exp.Paren, exp.Cast)):
        node = node.this
    if isinstance(node, exp.Placeholder):
        if node.name in params:
            return True, params[node.name]
        return False, None
    if isinstance(node, exp.Null):
        return True, None
    if isinstance(node, exp.Boolean):
        return True, node.this
    if isinstance(node, exp.Literal):
        if node.is_string:
            return True, node.this
        try:
            return True, node.to_py()
        except Exception:
            return True, node.this
    return False, None


def _scope_nodes(expression: exp.Expression) -> list[exp.Expression]:
    return [
        node
        for node in expression.walk(
            prune=lambda node: node is not expression and isinstance(node, exp.Query)
        )
        if isinstance(node, exp.Expression)
    ]


def _extract_filter(
    node: exp.Expression,
    index: CatalogIndex,
    params: Mapping[str, Any],
    table_aliases: Mapping[str, str],
) -> _ObservedFilter | None:
    operators: dict[type[exp.Expression], str] = {
        exp.EQ: "eq",
        exp.NEQ: "neq",
        exp.GT: "gt",
        exp.GTE: "gte",
        exp.LT: "lt",
        exp.LTE: "lte",
    }
    reverse = {"gt": "lt", "gte": "lte", "lt": "gt", "lte": "gte"}
    operator = operators.get(type(node))
    if operator is not None:
        left, right = node.this, node.expression
        field = _column(left, index, table_aliases)
        known, value = _literal_value(right, params)
        if field is None or not known:
            field = _column(right, index, table_aliases)
            known, value = _literal_value(left, params)
            operator = reverse.get(operator, operator)
        if field is not None and known:
            return _ObservedFilter(field[0], field[1], operator, (value,))
        return None
    if isinstance(node, exp.In):
        field = _column(node.this, index, table_aliases)
        values: list[Any] = []
        expressions = list(node.expressions)
        if not expressions and isinstance(node.args.get("field"), exp.Expression):
            expressions = [node.args["field"]]
        for item in expressions:
            known, value = _literal_value(item, params)
            if not known:
                return None
            if isinstance(value, (list, tuple, set, frozenset)):
                values.extend(value)
            else:
                values.append(value)
        if field is not None and values:
            return _ObservedFilter(field[0], field[1], "in", tuple(values))
    if isinstance(node, exp.Between):
        field = _column(node.this, index, table_aliases)
        low_ok, low = _literal_value(node.args["low"], params)
        high_ok, high = _literal_value(node.args["high"], params)
        if field is not None and low_ok and high_ok:
            return _ObservedFilter(field[0], field[1], "between", (low, high))
    if isinstance(node, exp.Is):
        field = _column(node.this, index, table_aliases)
        if field is not None and isinstance(node.expression, exp.Null):
            operator = "not_null" if node.args.get("negate") else "is_null"
            return _ObservedFilter(field[0], field[1], operator, ())
    if isinstance(node, exp.Not) and isinstance(node.this, exp.Is):
        field = _column(node.this.this, index, table_aliases)
        if field is not None and isinstance(node.this.expression, exp.Null):
            return _ObservedFilter(field[0], field[1], "not_null", ())
    return None


def _group_expressions(select: exp.Select) -> list[exp.Expression]:
    group = select.args.get("group")
    if not isinstance(group, exp.Group):
        return []
    resolved: list[exp.Expression] = []
    aliases = {item.alias: item.this for item in select.expressions if isinstance(item, exp.Alias)}
    for expression in group.expressions:
        if isinstance(expression, exp.Literal) and expression.is_int:
            position = int(expression.this) - 1
            if 0 <= position < len(select.expressions):
                item = select.expressions[position]
                resolved.append(item.this if isinstance(item, exp.Alias) else item)
                continue
        if isinstance(expression, exp.Column) and not expression.table and expression.name in aliases:
            resolved.append(aliases[expression.name])
        else:
            resolved.append(expression)
    return resolved


def _time_grain(expr: exp.Expression) -> str | None:
    for node in expr.walk():
        if node.key not in {"datetrunc", "timestamptrunc", "timetrunc"}:
            continue
        unit = node.args.get("unit")
        if isinstance(unit, exp.Expression):
            value = unit.name or unit.this
        else:
            value = unit
        if value is not None:
            return str(value).lower()
    return None


def _extract(
    tree: exp.Expression, index: CatalogIndex, params: Mapping[str, Any]
) -> tuple[
    list[_ObservedMeasure], list[_ObservedDimension], list[_ObservedFilter],
    list[tuple[str, str]], bool | None, int | None, list[str],
]:
    if not isinstance(tree, exp.Select):
        return [], [], [], [], None, None, ["outer_query_shape"]
    table_aliases = _table_aliases(tree, index)
    grouped = _group_expressions(tree)
    grouped_sql = {item.sql() for item in grouped}
    measures: list[_ObservedMeasure] = []
    dimensions: list[_ObservedDimension] = []
    for projection in tree.expressions:
        alias = projection.alias if isinstance(projection, exp.Alias) else None
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        aggregate = next(expression.find_all(exp.AggFunc), None)
        if aggregate is not None:
            ref = _column(aggregate, index, table_aliases)
            measures.append(
                _ObservedMeasure(
                    aggregate.key,
                    ref[0] if ref else None,
                    ref[1] if ref else None,
                    _norm(alias, index),
                    any(isinstance(item, exp.Distinct) for item in aggregate.walk()),
                )
            )
            continue
        ref = _column(expression, index, table_aliases)
        if ref is not None:
            dimensions.append(
                _ObservedDimension(
                    ref[0], ref[1], _norm(alias, index), _time_grain(expression),
                    expression.sql() in grouped_sql,
                )
            )
    filters: list[_ObservedFilter] = []
    for container_name in ("where", "having"):
        container = tree.args.get(container_name)
        if isinstance(container, exp.Expression):
            for node in _scope_nodes(container):
                observed = _extract_filter(node, index, params, table_aliases)
                if observed is not None and observed not in filters:
                    filters.append(observed)
    for join in tree.args.get("joins") or []:
        condition = join.args.get("on")
        if isinstance(condition, exp.Expression):
            for node in _scope_nodes(condition):
                observed = _extract_filter(node, index, params, table_aliases)
                if observed is not None and observed not in filters:
                    filters.append(observed)
    ordering: list[tuple[str, str]] = []
    order = tree.args.get("order")
    if isinstance(order, exp.Order):
        for item in order.expressions:
            expression = item.this if isinstance(item, exp.Ordered) else item
            ref = _column(expression, index)
            field = (
                index.normalize(expression.name)
                if isinstance(expression, exp.Column)
                else ref[1] if ref else index.normalize(expression.sql())
            )
            ordering.append((field, "desc" if item.args.get("desc") else "asc"))
    limit_value: int | None = None
    limit = tree.args.get("limit")
    if isinstance(limit, exp.Limit) and isinstance(limit.expression, exp.Literal):
        try:
            limit_value = int(limit.expression.this)
        except (TypeError, ValueError):
            pass
    return measures, dimensions, filters, ordering, tree.args.get("distinct") is not None, limit_value, []


def _safe_value(value: Any) -> tuple[str, str]:
    """Typed private comparison key; never included in a result or violation."""
    return type(value).__qualname__, repr(value)


def check_query_intent(
    tree: exp.Expression,
    intent: QueryIntent,
    index: CatalogIndex,
    params: Mapping[str, Any],
    enforcement: str,
) -> tuple[IntentCheckResult, list[Violation]]:
    """Compare a typed intent with the final SQL and return privacy-safe findings."""
    measures, dimensions, filters, ordering, distinct, limit, undecidable = _extract(
        tree, index, params
    )
    confirmed: list[str] = []
    missing: list[str] = []
    conflicting: list[str] = []
    severity = Severity.ERROR if enforcement == "block" else Severity.WARNING
    violations: list[Violation] = []

    for reason in undecidable:
        violations.append(
            Violation(
                Code.INTENT_UNDECIDABLE,
                severity,
                "The SQL shape cannot be deterministically compared with QueryIntent",
                hint="Use one outer SELECT so the requested result contract can be verified.",
                extra={"intent_constraint": reason},
            )
        )

    def report(code: Code, key: str, message: str, hint: str) -> None:
        target = missing if code is Code.INTENT_CONSTRAINT_MISSING else conflicting
        target.append(key)
        violations.append(
            Violation(code, severity, message, hint=hint, extra={"intent_constraint": key})
        )

    for expected_measure in intent.measures:
        measure_candidates = [
            item for item in measures
            if _field_matches(
                item.table,
                item.column,
                expected_measure.table,
                expected_measure.column,
                index,
            )
        ]
        measure_matches = [
            item for item in measure_candidates
            if item.aggregation == expected_measure.aggregation
            and (
                expected_measure.alias is None
                or item.alias == index.normalize(expected_measure.alias)
            )
            and (
                expected_measure.distinct is None
                or item.distinct == expected_measure.distinct
            )
        ]
        key = f"measure:{expected_measure.alias or expected_measure.column or '*'}"
        if measure_matches:
            confirmed.append(key)
        elif measure_candidates:
            report(
                Code.INTENT_CONSTRAINT_CONFLICT,
                key,
                "The SQL uses a different aggregation, alias, or distinctness for a required measure",
                f"Use the requested {expected_measure.aggregation.upper()} measure and output identity.",
            )
        else:
            report(
                Code.INTENT_CONSTRAINT_MISSING,
                key,
                "The SQL is missing a required measure",
                f"Add the requested {expected_measure.aggregation.upper()} measure.",
            )

    has_aggregation = bool(measures)
    for expected_dimension in intent.dimensions:
        dimension_candidates = [
            item for item in dimensions
            if _field_matches(
                item.table,
                item.column,
                expected_dimension.table,
                expected_dimension.column,
                index,
            )
        ]
        dimension_matches = [
            item for item in dimension_candidates
            if (
                expected_dimension.alias is None
                or item.alias == index.normalize(expected_dimension.alias)
            )
            and (
                expected_dimension.time_grain is None
                or item.time_grain == expected_dimension.time_grain
            )
            and (not has_aggregation or item.grouped)
        ]
        key = f"dimension:{expected_dimension.alias or expected_dimension.column}"
        if dimension_matches:
            confirmed.append(key)
        elif dimension_candidates:
            report(
                Code.INTENT_CONSTRAINT_CONFLICT,
                key,
                "The SQL projects a required dimension with the wrong grain, alias, or grouping",
                "Use the requested time grain and include the dimension in GROUP BY when aggregating.",
            )
        else:
            report(
                Code.INTENT_CONSTRAINT_MISSING,
                key,
                "The SQL is missing a required output dimension",
                "Project and group by the requested dimension.",
            )

    for expected_filter in intent.filters:
        filter_candidates = [
            item for item in filters
            if _field_matches(
                item.table,
                item.column,
                expected_filter.table,
                expected_filter.column,
                index,
            )
        ]
        expected_values = tuple(
            sorted(_safe_value(value) for value in expected_filter.values)
        )
        filter_matches = [
            item for item in filter_candidates
            if item.operator == expected_filter.operator
            and tuple(sorted(_safe_value(value) for value in item.values)) == expected_values
        ]
        qualifier = f"{expected_filter.table}." if expected_filter.table else ""
        key = f"filter:{qualifier}{expected_filter.column}:{expected_filter.operator}"
        if filter_matches:
            confirmed.append(key)
        elif filter_candidates:
            report(
                Code.INTENT_CONSTRAINT_CONFLICT,
                key,
                "The SQL filters a required field with a different operator or value",
                "Use the trusted filter operator and value supplied in QueryIntent.",
            )
        else:
            report(
                Code.INTENT_CONSTRAINT_MISSING,
                key,
                "The SQL is missing a required filter",
                "Add the trusted filter supplied in QueryIntent.",
            )

    for expected_order in intent.ordering:
        target = index.normalize(expected_order.field)
        key = f"order:{expected_order.field}:{expected_order.direction}"
        order_candidates = [item for item in ordering if item[0] == target]
        if (target, expected_order.direction) in ordering:
            confirmed.append(key)
        elif order_candidates:
            report(
                Code.INTENT_CONSTRAINT_CONFLICT,
                key,
                "The SQL orders a required field in the wrong direction",
                f"Order {expected_order.field} {expected_order.direction.upper()}.",
            )
        else:
            report(
                Code.INTENT_CONSTRAINT_MISSING,
                key,
                "The SQL is missing a required ordering",
                f"Order by {expected_order.field} {expected_order.direction.upper()}.",
            )

    if intent.distinct is not None:
        key = f"distinct:{str(intent.distinct).lower()}"
        if distinct == intent.distinct:
            confirmed.append(key)
        else:
            report(
                Code.INTENT_CONSTRAINT_CONFLICT,
                key,
                "The SQL DISTINCT behavior conflicts with the query intent",
                "Add or remove SELECT DISTINCT to match QueryIntent.",
            )
    if intent.limit is not None:
        key = f"limit:{intent.limit}"
        if limit == intent.limit:
            confirmed.append(key)
        elif limit is None:
            report(
                Code.INTENT_CONSTRAINT_MISSING,
                key,
                "The SQL is missing the requested row limit",
                f"Use LIMIT {intent.limit}.",
            )
        else:
            report(
                Code.INTENT_CONSTRAINT_CONFLICT,
                key,
                "The SQL row limit conflicts with the query intent",
                f"Use LIMIT {intent.limit}.",
            )

    if intent.expected_grain:
        actual = {
            item.alias or item.column for item in dimensions if item.grouped or not has_aggregation
        }
        for field_name in intent.expected_grain:
            key = f"grain:{field_name}"
            if index.normalize(field_name) in actual:
                confirmed.append(key)
            else:
                report(
                    Code.INTENT_CONSTRAINT_MISSING,
                    key,
                    "The SQL output grain is missing a required dimension",
                    f"Return one row per {field_name} as required by QueryIntent.",
                )

    observed = {
        "measure_count": len(measures),
        "dimension_count": len(dimensions),
        "filter_count": len(filters),
        "ordering_count": len(ordering),
        "distinct": distinct,
        "limit": limit,
    }
    return (
        IntentCheckResult(
            confirmed=confirmed,
            missing=missing,
            conflicting=conflicting,
            undecidable=undecidable,
            observed=observed,
        ),
        violations,
    )
