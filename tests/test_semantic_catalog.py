"""Governed semantic catalog binding and validation behavior."""

from __future__ import annotations

import json

import pytest

from sqlguard import (
    Catalog,
    CatalogError,
    Code,
    MeasureIntent,
    Policy,
    QueryIntent,
    RelationshipRole,
    SemanticCatalog,
    SemanticDimension,
    SemanticEntity,
    SemanticEvidence,
    SemanticFilter,
    SemanticMeasure,
    Severity,
    SQLGuard,
)


def _physical_catalog() -> Catalog:
    return Catalog.from_dict(
        {
            "orders": {
                "columns": {
                    "id": "bigint",
                    "customer_id": "bigint",
                    "amount": "decimal(10,2)",
                    "status": "text",
                    "created_at": "timestamp",
                },
                "primary_key": ["id"],
            },
            "line_items": {
                "columns": {
                    "id": "bigint",
                    "order_id": "bigint",
                    "quantity": "integer",
                },
                "primary_key": ["id"],
                "foreign_keys": [
                    {
                        "columns": ["order_id"],
                        "ref_table": "orders",
                        "ref_columns": ["id"],
                    }
                ],
            },
            "customers": {
                "columns": {"id": "bigint", "name": "text"},
                "primary_key": ["id"],
            },
        }
    )


def _semantic_catalog(*, mandatory_filter: bool = False) -> SemanticCatalog:
    return SemanticCatalog(
        version="2026-10-26",
        entities=(
            SemanticEntity("purchase", "orders", ("id",)),
            SemanticEntity("item", "line_items", ("id",)),
            SemanticEntity("buyer", "customers", ("id",)),
        ),
        measures=(
            SemanticMeasure(
                "order_revenue",
                "purchase",
                "sum",
                "amount",
                source_grain=("id",),
                mandatory_filters=(
                    (SemanticFilter("status", "eq", ("paid",)),) if mandatory_filter else ()
                ),
            ),
            SemanticMeasure("item_quantity", "item", "sum", "quantity", source_grain=("id",)),
        ),
        dimensions=(
            SemanticDimension("purchase_status", "purchase", "status"),
            SemanticDimension("purchase_time", "purchase", "created_at", kind="time"),
        ),
        relationships=(
            RelationshipRole(
                "purchase_items",
                "purchase",
                "item",
                ("id",),
                ("order_id",),
                "one_to_many",
            ),
        ),
        evidence=(SemanticEvidence("governance://finance", "7", "finance-data"),),
    )


def _guard(semantic: SemanticCatalog | None = None, *, enforcement: str = "block") -> SQLGuard:
    return SQLGuard(
        _physical_catalog(),
        Policy(
            intent_enforcement=enforcement,
            on_missing_stats="ignore",
            default_limit=None,
        ),
        semantic_catalog=semantic or _semantic_catalog(),
    )


def _intent(*, semantic_id: str = "order_revenue", aggregation: str = "sum") -> QueryIntent:
    return QueryIntent(
        measures=(
            MeasureIntent(
                aggregation,
                "amount",
                table="orders",
                alias="revenue",
                semantic_id=semantic_id,
            ),
        )
    )


def _codes(result) -> set[Code]:
    return {violation.code for violation in result.violations}


def test_catalog_digest_is_canonical_but_changes_with_governance() -> None:
    original = _semantic_catalog(mandatory_filter=True)
    reordered = SemanticCatalog(
        version=original.version,
        entities=tuple(reversed(original.entities)),
        measures=tuple(reversed(original.measures)),
        dimensions=tuple(reversed(original.dimensions)),
        relationships=original.relationships,
        evidence=original.evidence,
    )
    changed_filter = SemanticCatalog(
        version=original.version,
        entities=original.entities,
        measures=(
            SemanticMeasure(
                "order_revenue",
                "purchase",
                "sum",
                "amount",
                source_grain=("id",),
                mandatory_filters=(SemanticFilter("status", "eq", ("settled",)),),
            ),
            original.measures[1],
        ),
        dimensions=original.dimensions,
        relationships=original.relationships,
        evidence=original.evidence,
    )

    assert reordered.digest == original.digest
    assert changed_filter.digest != original.digest
    assert len(original.digest) == 64


def test_public_view_omits_physical_mapping_and_filter_values() -> None:
    catalog = _semantic_catalog(mandatory_filter=True)
    public = catalog.to_dict()
    public_json = json.dumps(public)

    assert "orders" not in public_json
    assert "line_items" not in public_json
    assert "amount" not in public_json
    assert "paid" not in public_json
    assert "from_columns" not in public_json
    assert "to_columns" not in public_json

    local = catalog.to_dict(include_physical=True)
    assert local["entities"][2]["table"] == "orders"
    assert local["measures"][1]["mandatory_filters"][0]["value_count"] == 1
    assert "values" not in local["measures"][1]["mandatory_filters"][0]
    assert catalog.to_dict(include_physical=True, include_values=True)["measures"][1][
        "mandatory_filters"
    ][0]["values"] == ["paid"]


@pytest.mark.parametrize(
    "semantic",
    [
        SemanticCatalog("v1", (SemanticEntity("purchase", "missing", ("id",)),)),
        SemanticCatalog("v1", (SemanticEntity("purchase", "orders", ("missing",)),)),
        SemanticCatalog(
            "v1",
            (
                SemanticEntity("purchase", "orders", ("id",)),
                SemanticEntity("buyer", "customers", ("id",)),
            ),
            relationships=(
                RelationshipRole(
                    "invented",
                    "purchase",
                    "buyer",
                    ("customer_id",),
                    ("id",),
                    "many_to_one",
                ),
            ),
        ),
    ],
)
def test_physical_bindings_fail_eagerly(semantic: SemanticCatalog) -> None:
    with pytest.raises(CatalogError):
        _guard(semantic)


def test_catalog_rejects_duplicate_and_unknown_semantic_ids() -> None:
    with pytest.raises(CatalogError, match="duplicate entity"):
        SemanticCatalog(
            "v1",
            (
                SemanticEntity("purchase", "orders", ("id",)),
                SemanticEntity("purchase", "orders", ("id",)),
            ),
        )
    with pytest.raises(CatalogError, match="unknown entity"):
        SemanticCatalog(
            "v1",
            (SemanticEntity("purchase", "orders", ("id",)),),
            measures=(SemanticMeasure("revenue", "missing", "sum", "amount"),),
        )
    with pytest.raises(CatalogError, match="scalar JSON literals"):
        SemanticFilter("status", "eq", ({"mutable": "value"},))


def test_unknown_measure_blocks_or_warns_by_policy() -> None:
    sql = "SELECT SUM(amount) AS revenue FROM orders"
    blocked = _guard().validate(sql, intent=_intent(semantic_id="invented"))
    warned = _guard(enforcement="warn").validate(sql, intent=_intent(semantic_id="invented"))

    assert not blocked.valid
    assert Code.UNKNOWN_SEMANTIC_OBJECT in _codes(blocked)
    assert warned.valid
    finding = next(item for item in warned.violations if item.code == Code.UNKNOWN_SEMANTIC_OBJECT)
    assert finding.severity == Severity.WARNING


def test_governed_metric_definition_must_match_intent() -> None:
    result = _guard().validate(
        "SELECT AVG(amount) AS revenue FROM orders",
        intent=_intent(aggregation="avg"),
    )

    assert not result.valid
    assert Code.METRIC_DEFINITION_MISMATCH in _codes(result)


def test_sql_must_implement_governed_distinctness_even_if_intent_omits_it() -> None:
    semantic = SemanticCatalog(
        "v1",
        (SemanticEntity("purchase", "orders", ("id",)),),
        measures=(
            SemanticMeasure("unique_buyers", "purchase", "count", "customer_id", distinct=True),
        ),
    )
    intent = QueryIntent(
        measures=(
            MeasureIntent("count", "customer_id", alias="buyers", semantic_id="unique_buyers"),
        )
    )
    result = _guard(semantic).validate(
        "SELECT COUNT(customer_id) AS buyers FROM orders", intent=intent
    )

    assert Code.METRIC_DEFINITION_MISMATCH in _codes(result)


def test_metric_owned_filter_is_required_without_leaking_value() -> None:
    guard = _guard(_semantic_catalog(mandatory_filter=True))
    missing = guard.validate("SELECT SUM(amount) AS revenue FROM orders", intent=_intent())
    conflicting = guard.validate(
        "SELECT SUM(amount) AS revenue FROM orders WHERE status = 'refunded'",
        intent=_intent(),
    )
    accepted = guard.validate(
        "SELECT SUM(amount) AS revenue FROM orders WHERE status = 'paid'",
        intent=_intent(),
    )

    assert Code.METRIC_DEFINITION_MISMATCH in _codes(missing)
    assert Code.METRIC_DEFINITION_MISMATCH in _codes(conflicting)
    assert "paid" not in json.dumps([item.to_dict() for item in missing.violations])
    assert accepted.valid


def test_direct_one_to_many_fanout_blocks_order_grain_measure() -> None:
    result = _guard().validate(
        """SELECT SUM(o.amount) AS revenue
        FROM orders AS o
        JOIN line_items AS li ON li.order_id = o.id""",
        intent=_intent(),
    )

    assert not result.valid
    assert Code.FANOUT_AGGREGATION_RISK in _codes(result)


def test_many_to_one_traversal_does_not_fan_out_item_measure() -> None:
    intent = QueryIntent(
        measures=(
            MeasureIntent(
                "sum",
                "quantity",
                table="line_items",
                alias="quantity",
                semantic_id="item_quantity",
            ),
        )
    )
    result = _guard().validate(
        """SELECT SUM(li.quantity) AS quantity
        FROM line_items AS li
        JOIN orders AS o ON li.order_id = o.id""",
        intent=intent,
    )

    assert result.valid
    assert Code.FANOUT_AGGREGATION_RISK not in _codes(result)


def test_join_without_semantic_relationship_is_rejected() -> None:
    result = _guard().validate(
        """SELECT SUM(o.amount) AS revenue
        FROM orders AS o
        JOIN customers AS c ON c.id = o.customer_id""",
        intent=_intent(),
    )

    assert not result.valid
    assert Code.RELATIONSHIP_ROLE_MISMATCH in _codes(result)


def test_semantic_check_is_optional_and_digest_is_auditable() -> None:
    semantic = _semantic_catalog()
    result = _guard(semantic, enforcement="off").validate(
        "SELECT SUM(amount) AS revenue FROM orders", intent=_intent(semantic_id="missing")
    )

    assert result.valid
    assert "semantic_catalog" in result.stats.checks_skipped
    assert result.stats.semantic_catalog_digest == semantic.digest
    assert result.to_dict()["stats"]["semantic_catalog_digest"] == semantic.digest
