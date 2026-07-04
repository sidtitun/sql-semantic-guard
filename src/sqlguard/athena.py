"""Reflect an AWS Glue Data Catalog database into a sqlguard Catalog.

Glue is Athena's system of record, so this gives the guard exactly what
Athena itself sees: columns, partition keys, and (when table parameters are
populated by crawlers/ETL) row counts and byte sizes for cost estimation.

Requires the ``athena`` extra: ``pip install 'sql-semantic-guard[athena]'``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlguard.catalog import Catalog, Column, Table

logger = logging.getLogger("sqlguard")

_COLUMNAR_FORMATS = ("parquet", "orc", "columnar")

# Glue/Hive table parameter keys that carry statistics, in preference order.
_ROW_KEYS = ("numRows", "recordCount", "numrows")
_BYTE_KEYS = ("totalSize", "rawDataSize", "sizeKey")


def _int_param(params: dict, keys: tuple) -> int | None:
    for key in keys:
        raw = params.get(key)
        if raw is None:
            continue
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return None


def catalog_from_glue(
    database: str,
    client: Any = None,
    region_name: str | None = None,
    catalog_id: str | None = None,
    include_stats: bool = True,
) -> Catalog:
    """Build a catalog from an AWS Glue database.

    Args:
        database: Glue database name (becomes the schema of every table).
        client: an existing ``boto3`` Glue client; created if omitted.
        region_name: region for the auto-created client.
        catalog_id: AWS account id of the catalog, for cross-account setups.
        include_stats: read row/byte statistics from table parameters.
    """
    if client is None:
        try:
            import boto3
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "catalog_from_glue requires boto3: "
                "pip install 'sql-semantic-guard[athena]'"
            ) from e
        client = boto3.client("glue", region_name=region_name)

    kwargs = {"DatabaseName": database}
    if catalog_id:
        kwargs["CatalogId"] = catalog_id

    tables: list[Table] = []
    paginator = client.get_paginator("get_tables")
    for page in paginator.paginate(**kwargs):
        for entry in page.get("TableList", []):
            descriptor = entry.get("StorageDescriptor") or {}
            columns: list[Column] = []
            for col in descriptor.get("Columns", []):
                columns.append(
                    Column(
                        name=col["Name"],
                        type=col.get("Type", "unknown"),
                        comment=col.get("Comment"),
                    )
                )
            partition_cols: list[str] = []
            for pkey in entry.get("PartitionKeys", []) or []:
                partition_cols.append(pkey["Name"])
                columns.append(
                    Column(
                        name=pkey["Name"],
                        type=pkey.get("Type", "string"),
                        comment=pkey.get("Comment"),
                    )
                )
            params = entry.get("Parameters") or {}
            input_format = (descriptor.get("InputFormat") or "").lower()
            columnar = any(fmt in input_format for fmt in _COLUMNAR_FORMATS) or None
            tables.append(
                Table(
                    name=entry["Name"],
                    columns=columns,
                    schema=database,
                    row_count=_int_param(params, _ROW_KEYS) if include_stats else None,
                    total_bytes=_int_param(params, _BYTE_KEYS) if include_stats else None,
                    partition_columns=tuple(partition_cols),
                    columnar=columnar,
                    comment=entry.get("Description"),
                )
            )
    return Catalog(tables=tables, default_schema=database)
