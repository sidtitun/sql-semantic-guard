"""Reflect a live database schema into a :class:`~sqlguard.catalog.Catalog`.

Uses SQLAlchemy inspection, so it works against Postgres, Redshift, MySQL,
Snowflake, Trino — anything with a SQLAlchemy dialect. On Postgres it also
pulls cheap planner statistics (``pg_class.reltuples`` and relation sizes) so
the heuristic cost estimator has something to work with.

Requires the ``postgres`` extra: ``pip install 'sql-semantic-guard[postgres]'``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlguard.catalog import Catalog, Column, ForeignKey, Table

logger = logging.getLogger("sqlguard")


def profile_allowed_values(
    engine: Any,
    table: str,
    column: str,
    max_distinct: int = 50,
) -> tuple[str, ...]:
    """Explicitly profile a low-cardinality column for enum metadata.

    Identifiers are quoted through the connected SQLAlchemy dialect. The
    extra row in LIMIT makes high-cardinality columns fail closed instead of
    silently recording an incomplete domain.
    """
    if max_distinct <= 0:
        raise ValueError("max_distinct must be positive")
    preparer = engine.dialect.identifier_preparer
    quoted_table = ".".join(preparer.quote(part) for part in table.split("."))
    quoted_column = preparer.quote(column)
    statement = (
        f"SELECT DISTINCT {quoted_column} FROM {quoted_table} "
        f"WHERE {quoted_column} IS NOT NULL ORDER BY {quoted_column} "
        f"LIMIT {max_distinct + 1}"
    )
    with engine.connect() as connection:
        values = [str(row[0]) for row in connection.exec_driver_sql(statement)]
    if len(values) > max_distinct:
        raise ValueError(
            f"{table}.{column} has more than {max_distinct} distinct values; "
            "refusing to treat it as an enum"
        )
    return tuple(values)


def _pg_stats(engine: Any, schemas: Sequence[str]) -> dict[tuple[str, str], tuple[int | None, int | None]]:
    """(schema, table) -> (row_count, total_bytes) from pg_class estimates."""
    query = """
        SELECT n.nspname, c.relname,
               GREATEST(c.reltuples, 0)::bigint,
               pg_total_relation_size(c.oid)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'm') AND n.nspname = ANY(%(schemas)s)
    """
    stats: dict[tuple[str, str], tuple[int | None, int | None]] = {}
    try:
        with engine.connect() as conn:
            result = conn.exec_driver_sql(query, {"schemas": list(schemas)})
            for schema, name, rows, size in result:
                stats[(schema, name)] = (int(rows) if rows is not None else None, size)
    except Exception:
        logger.warning("Could not read pg_class statistics; catalog will lack stats", exc_info=True)
    return stats


def catalog_from_sqlalchemy(
    engine: Any,
    schemas: Sequence[str] | None = None,
    include_views: bool = True,
    include_stats: bool = True,
) -> Catalog:
    """Build a catalog by inspecting a live database via SQLAlchemy.

    Args:
        engine: a SQLAlchemy ``Engine`` (or anything ``sqlalchemy.inspect``
            accepts).
        schemas: schemas to reflect; defaults to the connection's default
            schema (``public`` on Postgres).
        include_views: also reflect views (recommended — views are often the
            sanctioned query surface).
        include_stats: on Postgres, fetch row-count/size estimates from
            ``pg_class`` for the cost estimator.
    """
    try:
        from sqlalchemy import inspect
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "catalog_from_sqlalchemy requires SQLAlchemy: "
            "pip install 'sql-semantic-guard[postgres]'"
        ) from e

    inspector = inspect(engine)
    if schemas is None:
        default = inspector.default_schema_name or "public"
        schemas = [default]

    is_postgres = getattr(getattr(engine, "dialect", None), "name", "") == "postgresql"
    stats = _pg_stats(engine, schemas) if (include_stats and is_postgres) else {}

    tables: list[Table] = []
    for schema in schemas:
        names = list(inspector.get_table_names(schema=schema))
        if include_views:
            names.extend(inspector.get_view_names(schema=schema))
            try:
                names.extend(inspector.get_materialized_view_names(schema=schema))
            except Exception:  # pragma: no cover - not all dialects
                pass
        for name in sorted(set(names)):
            columns: list[Column] = []
            for col in inspector.get_columns(name, schema=schema):
                try:
                    type_str = str(col["type"])
                except Exception:  # pragma: no cover
                    type_str = "unknown"
                columns.append(
                    Column(
                        name=col["name"],
                        type=type_str,
                        nullable=bool(col.get("nullable", True)),
                        comment=col.get("comment"),
                    )
                )
            row_count, total_bytes = stats.get((schema, name), (None, None))
            try:
                pk_data = inspector.get_pk_constraint(name, schema=schema) or {}
                primary_key = tuple(pk_data.get("constrained_columns") or ())
            except Exception:
                primary_key = ()
            foreign_keys: list[ForeignKey] = []
            try:
                reflected_fks = inspector.get_foreign_keys(name, schema=schema) or []
            except Exception:
                reflected_fks = []
            for fk in reflected_fks:
                ref_name = fk.get("referred_table")
                local_columns = tuple(fk.get("constrained_columns") or ())
                ref_columns = tuple(fk.get("referred_columns") or ())
                if not ref_name or not local_columns or not ref_columns:
                    continue
                ref_schema = fk.get("referred_schema") or schema
                foreign_keys.append(
                    ForeignKey(
                        columns=local_columns,
                        ref_table=f"{ref_schema}.{ref_name}" if ref_schema else ref_name,
                        ref_columns=ref_columns,
                    )
                )
            tables.append(
                Table(
                    name=name,
                    columns=columns,
                    schema=schema,
                    row_count=row_count,
                    total_bytes=total_bytes,
                    columnar=False,
                    primary_key=primary_key,
                    foreign_keys=tuple(foreign_keys),
                )
            )
    return Catalog(tables=tables, default_schema=schemas[0] if schemas else None)
