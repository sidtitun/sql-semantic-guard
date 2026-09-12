"""Schema catalog: the ground truth the guard validates SQL against.

A :class:`Catalog` is a plain, serializable description of tables, columns,
types, and (optionally) statistics — it never talks to a database itself.
Build one by hand, from a dict/JSON document, or with the live reflectors in
:mod:`sqlguard.reflect` (SQLAlchemy) and :mod:`sqlguard.athena` (AWS Glue).
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.schema import MappingSchema

from sqlguard.errors import CatalogError

# Table-spec keys with reserved meaning in Catalog.from_dict rich form.
_TABLE_META_KEYS = {
    "columns",
    "row_count",
    "total_bytes",
    "partition_columns",
    "columnar",
    "tags",
    "comment",
}


@dataclass
class Column:
    """One column of a table.

    ``type`` is a SQL type string in the catalog's dialect (e.g. ``BIGINT``,
    ``VARCHAR(120)``, ``STRUCT<name STRING>``). Unknown/unparseable types are
    tolerated — type-dependent checks simply skip those columns.
    """

    name: str
    type: str = "unknown"
    nullable: bool = True
    tags: frozenset[str] = frozenset()
    comment: str | None = None
    avg_width: int | None = None  # average serialized width in bytes

    def __post_init__(self) -> None:
        if not self.name:
            raise CatalogError("Column name must be non-empty")
        if not isinstance(self.tags, frozenset):
            self.tags = frozenset(self.tags)


@dataclass
class Table:
    """One table (or view) with optional scan statistics."""

    name: str
    columns: list[Column] = field(default_factory=list)
    schema: str | None = None
    row_count: int | None = None
    total_bytes: int | None = None
    partition_columns: tuple[str, ...] = ()
    columnar: bool | None = None  # None = decide by dialect (Athena => True)
    tags: frozenset[str] = frozenset()
    comment: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise CatalogError("Table name must be non-empty")
        if not isinstance(self.tags, frozenset):
            self.tags = frozenset(self.tags)
        self.partition_columns = tuple(self.partition_columns)
        norm_cols = {c.name.lower() for c in self.columns}
        for p in self.partition_columns:
            if p.lower() not in norm_cols:
                raise CatalogError(
                    f"Partition column {p!r} of table {self.name!r} is not in its column list"
                )
        self._by_name: dict[str, Column] = {c.name.lower(): c for c in self.columns}
        if len(self._by_name) != len(self.columns):
            raise CatalogError(f"Table {self.name!r} has duplicate column names")

    @property
    def display_name(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    def column(self, name: str) -> Column | None:
        """Case-insensitive column lookup."""
        return self._by_name.get(name.lower())

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


def _parse_column_spec(name: str, spec: str | Mapping[str, Any]) -> Column:
    if isinstance(spec, str):
        return Column(name=name, type=spec)
    if isinstance(spec, Mapping):
        return Column(
            name=name,
            type=str(spec.get("type", "unknown")),
            nullable=bool(spec.get("nullable", True)),
            tags=frozenset(spec.get("tags", ())),
            comment=spec.get("comment"),
            avg_width=spec.get("avg_width"),
        )
    raise CatalogError(f"Invalid column spec for {name!r}: {spec!r}")


def _parse_table_spec(name: str, spec: Mapping[str, Any], schema: str | None) -> Table:
    if not isinstance(spec, Mapping):
        raise CatalogError(f"Invalid table spec for {name!r}: {spec!r}")
    if "columns" in spec:
        unknown = set(spec) - _TABLE_META_KEYS
        if unknown:
            raise CatalogError(f"Unknown keys in table spec {name!r}: {sorted(unknown)}")
        columns = [_parse_column_spec(c, s) for c, s in spec["columns"].items()]
        return Table(
            name=name,
            columns=columns,
            schema=schema,
            row_count=spec.get("row_count"),
            total_bytes=spec.get("total_bytes"),
            partition_columns=tuple(spec.get("partition_columns", ())),
            columnar=spec.get("columnar"),
            tags=frozenset(spec.get("tags", ())),
            comment=spec.get("comment"),
        )
    # simple form: {column: type_or_spec}
    columns = [_parse_column_spec(c, s) for c, s in spec.items()]
    return Table(name=name, columns=columns, schema=schema)


# Keys a rich column spec may contain (besides "type").
_COLUMN_META_KEYS = {"type", "nullable", "tags", "comment", "avg_width"}


def _is_column_spec(v: Any) -> bool:
    """Is ``v`` a column spec (``"int"`` or ``{"type": ..., ...}``) rather than
    a nested table? A bare string is a type; a Mapping is a column spec only
    when it declares ``type`` or contains solely recognized column keys —
    otherwise ``{"id": "int", "amount": "decimal"}`` would masquerade as one."""
    if isinstance(v, str):
        return True
    if isinstance(v, Mapping):
        if "type" in v:
            return True
        return bool(v) and set(v).issubset(_COLUMN_META_KEYS)
    return False


def _looks_like_table_spec(spec: Any) -> bool:
    if not isinstance(spec, Mapping):
        return False
    if "columns" in spec:
        return True
    return bool(spec) and all(_is_column_spec(v) for v in spec.values())


@dataclass
class Catalog:
    """A collection of tables the LLM is allowed to query.

    Anything *not* in the catalog is treated as nonexistent — including system
    tables like ``information_schema`` — which is deliberate: fail closed.
    """

    tables: list[Table] = field(default_factory=list)
    default_schema: str | None = None

    def __post_init__(self) -> None:
        has_schema = [t for t in self.tables if t.schema]
        lacks_schema = [t for t in self.tables if not t.schema]
        if has_schema and lacks_schema:
            if not self.default_schema:
                names = ", ".join(t.name for t in lacks_schema[:5])
                raise CatalogError(
                    "Catalog mixes schema-qualified and bare tables "
                    f"({names}, ...); set default_schema= to resolve the bare ones"
                )
            for t in lacks_schema:
                t.schema = self.default_schema
        seen = set()
        for t in self.tables:
            key = ((t.schema or "").lower(), t.name.lower())
            if key in seen:
                raise CatalogError(f"Duplicate table in catalog: {t.display_name!r}")
            seen.add(key)

    @property
    def has_schemas(self) -> bool:
        return any(t.schema for t in self.tables)

    # -- constructors ------------------------------------------------------

    @classmethod
    def from_dict(
        cls,
        mapping: Mapping[str, Any],
        default_schema: str | None = None,
        nested: bool | None = None,
    ) -> Catalog:
        """Build a catalog from a dict.

        Two-level form (no schemas)::

            {"orders": {"id": "bigint", "amount": "decimal(10,2)"}}

        Three-level form (schema -> table -> columns)::

            {"public": {"orders": {"id": "bigint"}}}

        Rich table form (works at either depth)::

            {"orders": {"columns": {...}, "row_count": 10_000_000,
                        "total_bytes": 2 << 30, "partition_columns": ["dt"]}}

        ``nested`` forces the interpretation when auto-detection is ambiguous.
        """
        if nested is None:
            specs = list(mapping.values())
            if not specs:
                nested = False
            elif all(_looks_like_table_spec(s) for s in specs):
                nested = False
            elif all(
                isinstance(s, Mapping)
                and "columns" not in s
                and all(_looks_like_table_spec(v) for v in s.values())
                for s in specs
            ):
                nested = True
            else:
                raise CatalogError(
                    "Could not auto-detect catalog dict shape; pass nested=True "
                    "for {schema: {table: columns}} or nested=False for {table: columns}"
                )
        tables: list[Table] = []
        if nested:
            for schema_name, tbls in mapping.items():
                for tname, tspec in tbls.items():
                    tables.append(_parse_table_spec(tname, tspec, schema_name))
        else:
            for tname, tspec in mapping.items():
                tables.append(_parse_table_spec(tname, tspec, None))
        return cls(tables=tables, default_schema=default_schema)

    @classmethod
    def from_json(cls, document: str, **kwargs: Any) -> Catalog:
        """Build a catalog from a JSON string (same shapes as :meth:`from_dict`)."""
        return cls.from_dict(json.loads(document), **kwargs)

    # -- export ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        def table_spec(t: Table) -> dict[str, Any]:
            spec: dict[str, Any] = {
                "columns": {
                    c.name: {
                        "type": c.type,
                        "nullable": c.nullable,
                        **({"tags": sorted(c.tags)} if c.tags else {}),
                        **({"avg_width": c.avg_width} if c.avg_width else {}),
                    }
                    for c in t.columns
                }
            }
            if t.row_count is not None:
                spec["row_count"] = t.row_count
            if t.total_bytes is not None:
                spec["total_bytes"] = t.total_bytes
            if t.partition_columns:
                spec["partition_columns"] = list(t.partition_columns)
            if t.columnar is not None:
                spec["columnar"] = t.columnar
            return spec

        if self.has_schemas:
            out: dict[str, Any] = {}
            for t in self.tables:
                out.setdefault(t.schema or "", {})[t.name] = table_spec(t)
            return out
        return {t.name: table_spec(t) for t in self.tables}


class CatalogIndex:
    """Dialect-aware lookup layer over a :class:`Catalog`.

    Normalizes identifiers with the dialect's case-folding rules (both
    Postgres and Athena fold unquoted identifiers to lowercase) and exposes
    the sqlglot ``MappingSchema`` used for star expansion and type inference.
    """

    def __init__(self, catalog: Catalog, dialect: str) -> None:
        self.catalog = catalog
        self.dialect = dialect
        self._sqlglot_dialect = Dialect.get_or_raise(dialect)
        self.by_key: dict[tuple[str | None, str], Table] = {}
        self.by_name: dict[str, list[Table]] = {}
        for t in catalog.tables:
            schema_norm = self.normalize(t.schema) if t.schema else None
            name_norm = self.normalize(t.name)
            self.by_key[(schema_norm, name_norm)] = t
            self.by_name.setdefault(name_norm, []).append(t)
        self.default_schema = (
            self.normalize(catalog.default_schema) if catalog.default_schema else None
        )
        self.has_schemas = catalog.has_schemas
        self._mapping_schema: MappingSchema | None = None

    def normalize(self, identifier: str) -> str:
        ident = self._sqlglot_dialect.normalize_identifier(exp.to_identifier(identifier))
        return ident.name

    # -- resolution --------------------------------------------------------

    def resolve(self, name: str, db: str = "") -> tuple[Table | None, str | None]:
        """Resolve a (possibly schema-qualified) table reference.

        Returns ``(table, hint)`` — ``table`` is None when unresolved, and
        ``hint`` may carry a did-you-mean or ambiguity explanation.
        """
        name_norm = self.normalize(name)
        db_norm = self.normalize(db) if db else None

        if db_norm:
            t = self.by_key.get((db_norm, name_norm))
            if t:
                return t, None
            return None, self._suggest_hint(name_norm)

        if self.has_schemas:
            if self.default_schema:
                t = self.by_key.get((self.default_schema, name_norm))
                if t:
                    return t, None
            candidates = self.by_name.get(name_norm, [])
            if len(candidates) == 1:
                return candidates[0], None
            if len(candidates) > 1:
                names = ", ".join(sorted(c.display_name for c in candidates))
                return None, f"Ambiguous table {name!r}; qualify it: {names}"
            return None, self._suggest_hint(name_norm)

        t = self.by_key.get((None, name_norm))
        if t:
            return t, None
        return None, self._suggest_hint(name_norm)

    def _suggest_hint(self, name_norm: str) -> str | None:
        matches = difflib.get_close_matches(name_norm, list(self.by_name), n=3, cutoff=0.6)
        if matches:
            display = ", ".join(
                sorted({t.display_name for m in matches for t in self.by_name[m]})
            )
            return f"Did you mean: {display}?"
        return None

    @staticmethod
    def suggest_columns(table: Table, name: str) -> str | None:
        matches = difflib.get_close_matches(
            name.lower(), [c.name.lower() for c in table.columns], n=3, cutoff=0.5
        )
        if matches:
            return "Did you mean: " + ", ".join(matches) + "?"
        return None

    # -- sqlglot bridge ----------------------------------------------------

    def mapping_schema(self) -> MappingSchema:
        """Build (once) the sqlglot MappingSchema for qualification."""
        if self._mapping_schema is None:
            nested: dict[str, Any] = {}
            for t in self.catalog.tables:
                cols = {c.name: self._safe_type(c.type) for c in t.columns}
                if self.has_schemas:
                    nested.setdefault(t.schema or "", {})[t.name] = cols
                else:
                    nested[t.name] = cols
            self._mapping_schema = MappingSchema(nested, dialect=self.dialect)
        return self._mapping_schema

    def _safe_type(self, type_str: str) -> str:
        try:
            exp.DataType.build(type_str, dialect=self.dialect)
            return type_str
        except Exception:
            return "UNKNOWN"

    def data_type(self, column: Column) -> exp.DataType | None:
        try:
            dt = exp.DataType.build(column.type, dialect=self.dialect)
            if dt.this == exp.DataType.Type.UNKNOWN:
                return None
            return dt
        except Exception:
            return None

    def all_column_names(self, tables: Iterable[Table]) -> list[str]:
        out: list[str] = []
        for t in tables:
            out.extend(c.name for c in t.columns)
        return out


def match_table(pattern: str, table: Table, index: CatalogIndex, schema_pattern: str | None = None) -> bool:
    """fnmatch-style table matching used by policy rules.

    ``pattern`` matches either the bare table name or the schema-qualified
    name; ``schema_pattern`` (when given) must additionally match the schema.
    """
    import fnmatch

    name = index.normalize(table.name)
    qualified = f"{index.normalize(table.schema)}.{name}" if table.schema else name
    pat = pattern.lower()
    if schema_pattern is not None:
        if not table.schema or not fnmatch.fnmatch(index.normalize(table.schema), schema_pattern.lower()):
            return False
    return fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(qualified, pat)
