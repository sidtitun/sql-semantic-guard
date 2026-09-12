"""Catalog boundary cases are security-relevant because binding trusts them."""

import pytest

from sqlguard import Catalog, Column, Table
from sqlguard.catalog import CatalogIndex, _is_column_spec, _looks_like_table_spec, match_table
from sqlguard.errors import CatalogError


def test_catalog_metadata_round_trip_and_normalization() -> None:
    column = Column("ID", "BIGINT", tags={"key"})  # type: ignore[arg-type]
    table = Table(
        "Orders",
        [column, Column("dt", "DATE")],
        schema="analytics",
        total_bytes=123,
        partition_columns=["dt"],  # type: ignore[arg-type]
        columnar=True,
        tags={"fact"},  # type: ignore[arg-type]
    )
    catalog = Catalog([table], default_schema="analytics")

    assert column.tags == frozenset({"key"})
    assert table.tags == frozenset({"fact"})
    assert table.column_names() == ["ID", "dt"]
    assert table.column("id") is column
    assert catalog.to_dict() == {
        "analytics": {
            "Orders": {
                "columns": {
                    "ID": {"type": "BIGINT", "nullable": True, "tags": ["key"]},
                    "dt": {"type": "DATE", "nullable": True},
                },
                "total_bytes": 123,
                "partition_columns": ["dt"],
                "columnar": True,
            }
        }
    }


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Column(""),
        lambda: Table(""),
        lambda: Catalog.from_dict({"t": {"columns": {"id": object()}}}),
        lambda: Catalog.from_dict({"t": {"columns": {"id": "int"}, "bogus": True}}),
        lambda: Catalog.from_dict({"t": "not-a-table"}, nested=False),
        lambda: Catalog.from_dict({"schema": {"table": {"id": "int"}}, "bad": 3}),
    ],
)
def test_invalid_catalog_boundaries_fail_at_construction(factory) -> None:
    with pytest.raises(CatalogError):
        factory()


def test_empty_catalog_and_json_constructor() -> None:
    assert Catalog.from_dict({}).tables == []
    catalog = Catalog.from_json('{"t": {"id": "int"}}')
    assert catalog.tables[0].column("id") is not None
    assert not _is_column_spec(3)
    assert not _looks_like_table_spec(3)


def test_schema_resolution_ambiguity_suggestions_and_type_fallbacks() -> None:
    catalog = Catalog(
        [
            Table("orders", [Column("identifier", "BIGINT")], schema="a"),
            Table("orders", [Column("identifier", "NOT A REAL TYPE")], schema="b"),
        ]
    )
    index = CatalogIndex(catalog, "postgres")

    table, hint = index.resolve("orders")
    assert table is None
    assert hint == "Ambiguous table 'orders'; qualify it: a.orders, b.orders"
    assert index.resolve("ordres", "a")[1] == "Did you mean: a.orders, b.orders?"
    assert CatalogIndex.suggest_columns(catalog.tables[0], "identifer")
    assert CatalogIndex.suggest_columns(catalog.tables[0], "unrelated") is None
    assert index.data_type(catalog.tables[0].columns[0]) is not None
    assert index.data_type(catalog.tables[1].columns[0]) is None
    assert index.all_column_names(catalog.tables) == ["identifier", "identifier"]
    assert index.mapping_schema() is index.mapping_schema()


def test_match_table_honors_schema_pattern() -> None:
    table = Table("orders", [Column("id")], schema="analytics")
    index = CatalogIndex(Catalog([table]), "postgres")

    assert match_table("orders", table, index, "analytics")
    assert not match_table("orders", table, index, "private")
    assert not match_table("customers", table, index)


def test_schema_index_resolves_unique_and_missing_names() -> None:
    table = Table("orders", [Column("id")], schema="analytics")
    index = CatalogIndex(Catalog([table]), "postgres")

    assert index.resolve("orders") == (table, None)
    assert index.resolve("missing") == (None, None)
    assert index.data_type(Column("x", "UNKNOWN")) is None
