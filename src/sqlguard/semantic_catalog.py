"""Governed business semantics bound to the trusted physical catalog.

The public semantic view deliberately omits physical mappings. SQL validation
uses the full local mapping and its canonical digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from sqlguard.catalog import CatalogIndex, Table
from sqlguard.errors import CatalogError
from sqlguard.intent import FilterIntent, MeasureIntent, QueryIntent, check_query_intent
from sqlguard.violations import Code, Severity, Violation

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_AGGREGATIONS = frozenset({"avg", "count", "max", "min", "sum"})
_CARDINALITIES = frozenset({"one_to_one", "one_to_many", "many_to_one", "many_to_many"})
_DIMENSION_KINDS = frozenset({"categorical", "time"})
_DIRECTIONS = frozenset({"forward", "both"})
_FILTER_OPERATORS = frozenset(
    {"eq", "neq", "gt", "gte", "lt", "lte", "in", "between", "is_null", "not_null"}
)


def _semantic_id(value: str, label: str) -> str:
    value = value.strip()
    if not _ID.fullmatch(value):
        raise CatalogError(f"{label} must match {_ID.pattern!r}")
    return value


def _text(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise CatalogError(f"{label} must be a non-empty string")
    return value


def _tuple_of_text(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise CatalogError(f"{label} must be a sequence")
    return tuple(_text(value, label) for value in values)


@dataclass(frozen=True)
class SemanticEvidence:
    """Governance provenance for one semantic catalog snapshot."""

    source: str
    version: str
    owner: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, "evidence source"))
        object.__setattr__(self, "version", _text(self.version, "evidence version"))


@dataclass(frozen=True)
class SemanticEntity:
    """Business entity mapped to one physical table and native grain."""

    id: str
    table: str
    grain: Sequence[str]
    aliases: Sequence[str] = ()
    description: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _semantic_id(self.id, "entity id"))
        object.__setattr__(self, "table", _text(self.table, "entity table"))
        grain = _tuple_of_text(self.grain, "entity grain")
        if not grain:
            raise CatalogError("entity grain must not be empty")
        object.__setattr__(self, "grain", grain)
        object.__setattr__(self, "aliases", _tuple_of_text(self.aliases, "entity alias"))


@dataclass(frozen=True)
class SemanticDimension:
    """Governed dimension mapped to one entity column."""

    id: str
    entity: str
    column: str
    kind: str = "categorical"
    native_time_grain: str | None = None
    aliases: Sequence[str] = ()
    description: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _semantic_id(self.id, "dimension id"))
        object.__setattr__(self, "entity", _semantic_id(self.entity, "dimension entity"))
        object.__setattr__(self, "column", _text(self.column, "dimension column"))
        kind = self.kind.lower().strip()
        if kind not in _DIMENSION_KINDS:
            raise CatalogError(f"dimension kind must be one of {sorted(_DIMENSION_KINDS)}")
        object.__setattr__(self, "kind", kind)
        if self.native_time_grain is not None and kind != "time":
            raise CatalogError("native_time_grain is only valid for time dimensions")
        object.__setattr__(self, "aliases", _tuple_of_text(self.aliases, "dimension alias"))


@dataclass(frozen=True)
class SemanticFilter:
    """Metric-owned physical predicate; values stay local by default."""

    column: str
    operator: str = "eq"
    values: Sequence[Any] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "column", _text(self.column, "semantic filter column"))
        operator = self.operator.lower().strip()
        if operator not in _FILTER_OPERATORS:
            raise CatalogError(
                f"semantic filter operator must be one of {sorted(_FILTER_OPERATORS)}"
            )
        object.__setattr__(self, "operator", operator)
        if isinstance(self.values, (str, bytes)):
            raise CatalogError("semantic filter values must be a sequence")
        values = tuple(self.values)
        if not all(value is None or isinstance(value, (str, int, float, bool)) for value in values):
            raise CatalogError("semantic filter values must be scalar JSON literals")
        try:
            json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise CatalogError("semantic filter values must be JSON-serializable") from exc
        # Reuse QueryIntent's arity contract.
        FilterIntent(self.column, operator, values)
        object.__setattr__(self, "values", values)


@dataclass(frozen=True)
class SemanticMeasure:
    """Governed aggregate definition at an entity's physical grain."""

    id: str
    entity: str
    aggregation: str
    column: str | None = None
    distinct: bool = False
    source_grain: Sequence[str] = ()
    mandatory_filters: Sequence[SemanticFilter] = ()
    additive_dimensions: frozenset[str] | None = None
    return_type: str | None = None
    aliases: Sequence[str] = ()
    description: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _semantic_id(self.id, "measure id"))
        object.__setattr__(self, "entity", _semantic_id(self.entity, "measure entity"))
        aggregation = self.aggregation.lower().strip()
        if aggregation not in _AGGREGATIONS:
            raise CatalogError(f"measure aggregation must be one of {sorted(_AGGREGATIONS)}")
        object.__setattr__(self, "aggregation", aggregation)
        if self.column is not None:
            object.__setattr__(self, "column", _text(self.column, "measure column"))
        if self.column is None and aggregation != "count":
            raise CatalogError("only count measures may omit the physical column")
        object.__setattr__(self, "source_grain", _tuple_of_text(self.source_grain, "source grain"))
        filters = tuple(self.mandatory_filters)
        if not all(isinstance(item, SemanticFilter) for item in filters):
            raise CatalogError("mandatory_filters must contain SemanticFilter values")
        object.__setattr__(self, "mandatory_filters", filters)
        if self.additive_dimensions is not None:
            object.__setattr__(
                self,
                "additive_dimensions",
                frozenset(
                    _semantic_id(item, "additive dimension") for item in self.additive_dimensions
                ),
            )
        object.__setattr__(self, "aliases", _tuple_of_text(self.aliases, "measure alias"))


@dataclass(frozen=True)
class RelationshipRole:
    """Governed direct relationship with direction-specific cardinality."""

    id: str
    from_entity: str
    to_entity: str
    from_columns: Sequence[str]
    to_columns: Sequence[str]
    cardinality: str
    allowed_direction: str = "both"
    description: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _semantic_id(self.id, "relationship role id"))
        object.__setattr__(self, "from_entity", _semantic_id(self.from_entity, "from_entity"))
        object.__setattr__(self, "to_entity", _semantic_id(self.to_entity, "to_entity"))
        from_columns = _tuple_of_text(self.from_columns, "relationship from column")
        to_columns = _tuple_of_text(self.to_columns, "relationship to column")
        if not from_columns or len(from_columns) != len(to_columns):
            raise CatalogError("relationship columns must be non-empty and have equal length")
        object.__setattr__(self, "from_columns", from_columns)
        object.__setattr__(self, "to_columns", to_columns)
        cardinality = self.cardinality.lower().strip()
        if cardinality not in _CARDINALITIES:
            raise CatalogError(f"cardinality must be one of {sorted(_CARDINALITIES)}")
        object.__setattr__(self, "cardinality", cardinality)
        direction = self.allowed_direction.lower().strip()
        if direction not in _DIRECTIONS:
            raise CatalogError(f"allowed_direction must be one of {sorted(_DIRECTIONS)}")
        object.__setattr__(self, "allowed_direction", direction)

    def fans_out_from(self, entity_id: str) -> bool:
        if entity_id == self.from_entity:
            return self.cardinality in {"one_to_many", "many_to_many"}
        if entity_id == self.to_entity and self.allowed_direction == "both":
            return self.cardinality in {"many_to_one", "many_to_many"}
        return False


@dataclass(frozen=True)
class SemanticCatalog:
    """Immutable, versioned business semantics and local physical mappings."""

    version: str
    entities: Sequence[SemanticEntity]
    measures: Sequence[SemanticMeasure] = ()
    dimensions: Sequence[SemanticDimension] = ()
    relationships: Sequence[RelationshipRole] = ()
    evidence: Sequence[SemanticEvidence] = ()
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", _text(self.version, "semantic catalog version"))
        for name, expected_type in (
            ("entities", SemanticEntity),
            ("measures", SemanticMeasure),
            ("dimensions", SemanticDimension),
            ("relationships", RelationshipRole),
            ("evidence", SemanticEvidence),
        ):
            values = tuple(getattr(self, name))
            if not all(isinstance(item, expected_type) for item in values):
                raise CatalogError(f"{name} must contain only {expected_type.__name__} values")
            object.__setattr__(self, name, values)
        entities = self._unique(self.entities, "entity")
        measures = self._unique(self.measures, "measure")
        dimensions = self._unique(self.dimensions, "dimension")
        self._unique(self.relationships, "relationship role")
        for measure in self.measures:
            if measure.entity not in entities:
                raise CatalogError(
                    f"measure {measure.id!r} references unknown entity {measure.entity!r}"
                )
            if measure.additive_dimensions is not None:
                unknown = set(measure.additive_dimensions) - set(dimensions)
                if unknown:
                    raise CatalogError(
                        f"measure {measure.id!r} has unknown additive dimensions {sorted(unknown)}"
                    )
        for dimension in self.dimensions:
            if dimension.entity not in entities:
                raise CatalogError(
                    f"dimension {dimension.id!r} references unknown entity {dimension.entity!r}"
                )
        for relationship in self.relationships:
            if relationship.from_entity not in entities or relationship.to_entity not in entities:
                raise CatalogError(f"relationship {relationship.id!r} references an unknown entity")
        overlap = set(measures) & set(dimensions)
        if overlap:
            raise CatalogError(f"semantic measure/dimension ids overlap: {sorted(overlap)}")
        canonical = json.dumps(
            self.to_dict(include_physical=True, include_values=True),
            sort_keys=True,
            separators=(",", ":"),
        )
        object.__setattr__(self, "digest", hashlib.sha256(canonical.encode()).hexdigest())

    @staticmethod
    def _unique(values: Sequence[Any], label: str) -> dict[str, Any]:
        indexed = {item.id: item for item in values}
        if len(indexed) != len(values):
            raise CatalogError(f"duplicate {label} id")
        return indexed

    def to_dict(
        self, *, include_physical: bool = False, include_values: bool = False
    ) -> dict[str, Any]:
        """Serialize a public semantic view or the full local mapping."""
        entities = [
            {
                "id": item.id,
                "aliases": list(item.aliases),
                "description": item.description,
                **({"table": item.table, "grain": list(item.grain)} if include_physical else {}),
            }
            for item in sorted(self.entities, key=lambda value: value.id)
        ]
        measures = []
        for item in sorted(self.measures, key=lambda value: value.id):
            serialized: dict[str, Any] = {
                "id": item.id,
                "entity": item.entity,
                "aggregation": item.aggregation,
                "distinct": item.distinct,
                "additive_dimensions": (
                    sorted(item.additive_dimensions)
                    if item.additive_dimensions is not None
                    else None
                ),
                "return_type": item.return_type,
                "aliases": list(item.aliases),
                "description": item.description,
            }
            if include_physical:
                serialized["column"] = item.column
                serialized["source_grain"] = list(item.source_grain)
                serialized["mandatory_filters"] = [
                    {
                        "column": required.column,
                        "operator": required.operator,
                        "value_count": len(required.values),
                        **({"values": list(required.values)} if include_values else {}),
                    }
                    for required in item.mandatory_filters
                ]
            measures.append(serialized)
        dimensions = [
            {
                "id": item.id,
                "entity": item.entity,
                "kind": item.kind,
                "native_time_grain": item.native_time_grain,
                "aliases": list(item.aliases),
                "description": item.description,
                **({"column": item.column} if include_physical else {}),
            }
            for item in sorted(self.dimensions, key=lambda value: value.id)
        ]
        relationships = [
            {
                "id": item.id,
                "from_entity": item.from_entity,
                "to_entity": item.to_entity,
                "cardinality": item.cardinality,
                "allowed_direction": item.allowed_direction,
                "description": item.description,
                **(
                    {
                        "from_columns": list(item.from_columns),
                        "to_columns": list(item.to_columns),
                    }
                    if include_physical
                    else {}
                ),
            }
            for item in sorted(self.relationships, key=lambda value: value.id)
        ]
        return {
            "version": self.version,
            "entities": entities,
            "measures": measures,
            "dimensions": dimensions,
            "relationships": relationships,
            "evidence": [
                {
                    "source": item.source,
                    "version": item.version,
                    "owner": item.owner,
                    "updated_at": item.updated_at,
                }
                for item in sorted(
                    self.evidence,
                    key=lambda value: (
                        value.source,
                        value.version,
                        value.owner or "",
                        value.updated_at or "",
                    ),
                )
            ],
        }


@dataclass(frozen=True)
class _BoundEntity:
    definition: SemanticEntity
    table: Table
    identity: str


class SemanticCatalogIndex:
    """Eagerly validated bridge from semantic IDs to physical catalog objects."""

    def __init__(self, semantic: SemanticCatalog, physical: CatalogIndex) -> None:
        self.catalog = semantic
        self.physical = physical
        self.entities: dict[str, _BoundEntity] = {}
        self.measures = {item.id: item for item in semantic.measures}
        self.dimensions = {item.id: item for item in semantic.dimensions}
        self.relationships = {item.id: item for item in semantic.relationships}
        for entity in semantic.entities:
            schema, separator, name = entity.table.rpartition(".")
            table, _ = physical.resolve(name if separator else entity.table, schema)
            if table is None:
                raise CatalogError(
                    f"semantic entity {entity.id!r} maps to unknown table {entity.table!r}"
                )
            self._require_columns(table, entity.grain, f"entity {entity.id!r} grain")
            self.entities[entity.id] = _BoundEntity(
                entity, table, physical.normalize(table.display_name)
            )
        for dimension in semantic.dimensions:
            bound = self.entities[dimension.entity]
            self._require_columns(bound.table, (dimension.column,), f"dimension {dimension.id!r}")
        for measure in semantic.measures:
            bound = self.entities[measure.entity]
            columns = (() if measure.column is None else (measure.column,)) + tuple(
                measure.source_grain or bound.definition.grain
            )
            columns += tuple(item.column for item in measure.mandatory_filters)
            self._require_columns(bound.table, columns, f"measure {measure.id!r}")
        for relationship in semantic.relationships:
            left = self.entities[relationship.from_entity].table
            right = self.entities[relationship.to_entity].table
            self._require_columns(left, relationship.from_columns, relationship.id)
            self._require_columns(right, relationship.to_columns, relationship.id)
            if not self._declared_fk(
                left, relationship.from_columns, right, relationship.to_columns
            ):
                raise CatalogError(
                    f"relationship {relationship.id!r} is not backed by a declared foreign key"
                )

    @staticmethod
    def _require_columns(table: Table, columns: Sequence[str], label: str) -> None:
        missing = [column for column in columns if table.column(column) is None]
        if missing:
            raise CatalogError(
                f"{label} references missing columns on {table.display_name}: {missing}"
            )

    def _declared_fk(
        self,
        left: Table,
        left_columns: Sequence[str],
        right: Table,
        right_columns: Sequence[str],
    ) -> bool:
        def matches(
            local: Table,
            local_columns: Sequence[str],
            remote: Table,
            remote_columns: Sequence[str],
        ) -> bool:
            for fk in local.foreign_keys:
                schema, separator, name = fk.ref_table.rpartition(".")
                target, _ = self.physical.resolve(name if separator else fk.ref_table, schema)
                if (
                    target is remote
                    and tuple(map(str.lower, fk.columns)) == tuple(map(str.lower, local_columns))
                    and tuple(map(str.lower, fk.ref_columns))
                    == tuple(map(str.lower, remote_columns))
                ):
                    return True
            return False

        return matches(left, left_columns, right, right_columns) or matches(
            right, right_columns, left, left_columns
        )

    def entity_for_table(self, table: exp.Table) -> str | None:
        resolved, _ = self.physical.resolve(table.name, table.db)
        if resolved is None:
            return None
        identity = self.physical.normalize(resolved.display_name)
        for entity_id, bound in self.entities.items():
            if bound.identity == identity:
                return entity_id
        return None

    def relationship_between(self, left: str, right: str) -> list[RelationshipRole]:
        return [
            role
            for role in self.relationships.values()
            if (role.from_entity == left and role.to_entity == right)
            or (
                role.allowed_direction == "both"
                and role.from_entity == right
                and role.to_entity == left
            )
        ]


def _top_level_tables(tree: exp.Expression) -> list[exp.Table]:
    if not isinstance(tree, exp.Select):
        return []
    out: list[exp.Table] = []
    from_ = tree.args.get("from_")
    if isinstance(from_, exp.From) and isinstance(from_.this, exp.Table):
        out.append(from_.this)
    out.extend(
        join.this
        for join in tree.args.get("joins") or []
        if isinstance(join, exp.Join) and isinstance(join.this, exp.Table)
    )
    return out


def check_semantic_catalog(
    tree: exp.Expression,
    intent: QueryIntent,
    semantic: SemanticCatalogIndex,
    params: Mapping[str, Any],
    enforcement: str,
) -> list[Violation]:
    """Validate semantic measure definitions, filters, and direct fan-out."""
    severity = Severity.ERROR if enforcement == "block" else Severity.WARNING
    violations: list[Violation] = []
    source_entities = {
        entity_id
        for table in _top_level_tables(tree)
        if (entity_id := semantic.entity_for_table(table)) is not None
    }
    reported_fanout: set[tuple[str, str]] = set()

    for requested in intent.measures:
        semantic_id = requested.semantic_id
        if semantic_id is None:
            continue
        measure = semantic.measures.get(semantic_id)
        if measure is None:
            violations.append(
                Violation(
                    Code.UNKNOWN_SEMANTIC_OBJECT,
                    severity,
                    f"Unknown semantic measure {semantic_id!r}",
                    hint="Use a measure ID from the bound semantic catalog version.",
                    extra={"semantic_id": semantic_id, "catalog_digest": semantic.catalog.digest},
                )
            )
            continue
        bound = semantic.entities[measure.entity]
        expected_table = semantic.physical.normalize(bound.table.display_name)
        requested_table = (
            semantic.physical.normalize(requested.table) if requested.table is not None else None
        )
        requested_column = (
            semantic.physical.normalize(requested.column) if requested.column is not None else None
        )
        expected_column = (
            semantic.physical.normalize(measure.column) if measure.column is not None else None
        )
        if (
            requested.aggregation != measure.aggregation
            or requested_column != expected_column
            or (requested_table is not None and requested_table != expected_table)
            or (requested.distinct is not None and requested.distinct != measure.distinct)
        ):
            violations.append(
                Violation(
                    Code.METRIC_DEFINITION_MISMATCH,
                    severity,
                    f"QueryIntent does not match governed measure {semantic_id!r}",
                    hint="Resolve the measure from the current semantic catalog snapshot.",
                    extra={"semantic_id": semantic_id, "catalog_digest": semantic.catalog.digest},
                )
            )
            continue

        governed_intent = QueryIntent(
            measures=(
                MeasureIntent(
                    measure.aggregation,
                    measure.column,
                    table=bound.table.display_name,
                    alias=requested.alias,
                    distinct=measure.distinct,
                ),
            )
        )
        governed_result, _ = check_query_intent(
            tree, governed_intent, semantic.physical, params, enforcement
        )
        if not governed_result.matched:
            violations.append(
                Violation(
                    Code.METRIC_DEFINITION_MISMATCH,
                    severity,
                    f"SQL does not implement governed measure {semantic_id!r}",
                    hint="Generate the aggregate from the bound governed definition.",
                    extra={
                        "semantic_id": semantic_id,
                        "catalog_digest": semantic.catalog.digest,
                    },
                )
            )
            continue

        if measure.mandatory_filters:
            required_intent = QueryIntent(
                filters=[
                    FilterIntent(
                        item.column,
                        item.operator,
                        item.values,
                        table=bound.table.display_name,
                    )
                    for item in measure.mandatory_filters
                ]
            )
            result, _ = check_query_intent(
                tree, required_intent, semantic.physical, params, enforcement
            )
            if not result.matched:
                violations.append(
                    Violation(
                        Code.METRIC_DEFINITION_MISMATCH,
                        severity,
                        f"SQL does not apply every governed filter for measure {semantic_id!r}",
                        hint="Apply the metric-owned filters from the semantic catalog.",
                        extra={
                            "semantic_id": semantic_id,
                            "catalog_digest": semantic.catalog.digest,
                            "missing_filter_count": len(result.missing),
                            "conflicting_filter_count": len(result.conflicting),
                        },
                    )
                )

        for joined_entity in source_entities - {measure.entity}:
            roles = semantic.relationship_between(measure.entity, joined_entity)
            if not roles:
                violations.append(
                    Violation(
                        Code.RELATIONSHIP_ROLE_MISMATCH,
                        severity,
                        "SQL joins a governed measure through an undeclared semantic relationship",
                        hint="Declare and select a governed relationship role for this route.",
                        extra={
                            "semantic_id": semantic_id,
                            "from_entity": measure.entity,
                            "to_entity": joined_entity,
                            "catalog_digest": semantic.catalog.digest,
                        },
                    )
                )
                continue
            for role in roles:
                if not role.fans_out_from(measure.entity):
                    continue
                key = (semantic_id, role.id)
                if key in reported_fanout:
                    continue
                reported_fanout.add(key)
                violations.append(
                    Violation(
                        Code.FANOUT_AGGREGATION_RISK,
                        severity,
                        f"Measure {semantic_id!r} may be duplicated by relationship {role.id!r}",
                        hint="Aggregate at the measure source grain before traversing the fan-out relationship.",
                        extra={
                            "semantic_id": semantic_id,
                            "relationship_role": role.id,
                            "cardinality": role.cardinality,
                            "catalog_digest": semantic.catalog.digest,
                        },
                    )
                )
    return violations
