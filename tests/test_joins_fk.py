"""Foreign-key-aware checks catch executable joins that silently return wrong data."""

import pytest

from sqlguard import Catalog, ForeignKey, Policy, SQLGuard
from sqlguard.catalog import CatalogIndex
from sqlguard.errors import CatalogError
from sqlguard.violations import Code, Severity


@pytest.fixture
def relationship_catalog() -> Catalog:
    return Catalog.from_dict(
        {
            "orders": {
                "columns": {
                    "id": "bigint",
                    "tenant_id": "bigint",
                    "customer_id": "bigint",
                },
                "primary_key": ["id", "tenant_id"],
                "foreign_keys": [
                    {
                        "columns": ["customer_id"],
                        "ref_table": "customers",
                        "ref_columns": ["id"],
                    }
                ],
            },
            "customers": {
                "columns": {"id": "bigint", "tenant_id": "bigint", "name": "text"},
                "primary_key": ["id"],
            },
            "line_items": {
                "columns": {"order_id": "bigint", "tenant_id": "bigint", "sku": "text"},
                "foreign_keys": [
                    {
                        "columns": ["order_id", "tenant_id"],
                        "ref_table": "orders",
                        "ref_columns": ["id", "tenant_id"],
                    }
                ],
            },
        }
    )


def _guard(catalog: Catalog, **policy_kwargs: object) -> SQLGuard:
    return SQLGuard(
        catalog,
        Policy(on_missing_stats="ignore", default_limit=None, **policy_kwargs),
        dialect="postgres",
    )


def test_correct_declared_join_is_clean(relationship_catalog: Catalog) -> None:
    result = _guard(relationship_catalog).validate(
        "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id"
    )

    assert result.valid
    assert not any(v.code in {Code.INVALID_JOIN_PATH, Code.UNDECLARED_JOIN} for v in result.violations)


def test_wrong_key_reports_declared_relationship(relationship_catalog: Catalog) -> None:
    result = _guard(relationship_catalog).validate(
        "SELECT o.id, c.name FROM orders o JOIN customers c ON o.id = c.id"
    )

    violation = next(v for v in result.violations if v.code is Code.INVALID_JOIN_PATH)
    assert violation.severity is Severity.WARNING
    assert violation.hint is not None
    assert "orders.customer_id = customers.id" in violation.hint


def test_strict_mode_blocks_wrong_join(relationship_catalog: Catalog) -> None:
    result = _guard(relationship_catalog, strict_joins=True).validate(
        "SELECT o.id FROM orders o JOIN customers c ON o.id = c.id"
    )

    assert not result.valid
    assert result.sql is None
    assert any(v.code is Code.INVALID_JOIN_PATH and v.is_error for v in result.violations)


def test_composite_foreign_key_requires_every_pair(relationship_catalog: Catalog) -> None:
    partial = _guard(relationship_catalog).validate(
        "SELECT li.sku FROM line_items li JOIN orders o ON li.order_id = o.id"
    )
    complete = _guard(relationship_catalog).validate(
        "SELECT li.sku FROM line_items li JOIN orders o "
        "ON li.order_id = o.id AND li.tenant_id = o.tenant_id"
    )

    assert any(v.code is Code.INVALID_JOIN_PATH for v in partial.violations)
    assert not any(v.code is Code.INVALID_JOIN_PATH for v in complete.violations)


def test_undeclared_join_is_opt_in(relationship_catalog: Catalog) -> None:
    sql = "SELECT li.sku FROM line_items li JOIN customers c ON li.tenant_id = c.tenant_id"

    default = _guard(relationship_catalog).validate(sql)
    curated = _guard(relationship_catalog, require_declared_join_paths=True).validate(sql)

    assert not any(v.code is Code.UNDECLARED_JOIN for v in default.violations)
    assert any(v.code is Code.UNDECLARED_JOIN for v in curated.violations)


def test_ordinary_self_join_is_exempt(relationship_catalog: Catalog) -> None:
    result = _guard(relationship_catalog, require_declared_join_paths=True).validate(
        "SELECT a.id FROM orders a JOIN orders b ON a.tenant_id = b.tenant_id"
    )
    assert not any(
        v.code in {Code.INVALID_JOIN_PATH, Code.UNDECLARED_JOIN} for v in result.violations
    )


def test_cte_lineage_resolves_to_physical_relationship(relationship_catalog: Catalog) -> None:
    result = _guard(relationship_catalog).validate(
        "WITH source AS (SELECT customer_id FROM orders) "
        "SELECT s.customer_id FROM source s JOIN customers c "
        "ON s.customer_id = c.tenant_id"
    )

    assert any(v.code is Code.INVALID_JOIN_PATH for v in result.violations)


def test_catalog_relationships_round_trip_and_bidirectional_index(
    relationship_catalog: Catalog,
) -> None:
    round_tripped = Catalog.from_dict(relationship_catalog.to_dict())
    edges = CatalogIndex(round_tripped, "postgres").fk_edges()

    assert len(edges) == 4
    assert round_tripped.tables[0].primary_key == ("id", "tenant_id")
    assert round_tripped.tables[0].foreign_keys == (
        ForeignKey(("customer_id",), "customers", ("id",)),
    )


@pytest.mark.parametrize(
    "foreign_key",
    [
        {"columns": [], "ref_table": "customers", "ref_columns": []},
        {"columns": ["missing"], "ref_table": "customers", "ref_columns": ["id"]},
        {"columns": ["customer_id"], "ref_table": "missing", "ref_columns": ["id"]},
        {"columns": ["customer_id"], "ref_table": "customers", "ref_columns": ["missing"]},
        {
            "columns": ["customer_id", "id"],
            "ref_table": "customers",
            "ref_columns": ["id"],
        },
    ],
)
def test_invalid_relationship_metadata_fails_closed(foreign_key: dict[str, object]) -> None:
    with pytest.raises(CatalogError):
        Catalog.from_dict(
            {
                "orders": {
                    "columns": {"id": "bigint", "customer_id": "bigint"},
                    "foreign_keys": [foreign_key],
                },
                "customers": {"columns": {"id": "bigint"}},
            }
        )
