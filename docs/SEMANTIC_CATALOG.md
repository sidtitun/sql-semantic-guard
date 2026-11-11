# Governed semantic catalog

The optional `SemanticCatalog` binds stable business IDs to the physical
catalog SQLGuard already trusts. It lets an application say that an intent
requests `order_revenue`, while the guard—not the model—owns the definition
`SUM(orders.amount)`, its source grain, required predicates, and allowed
relationships.

## Trust boundary

Create the catalog from reviewed application configuration or imported
governance artifacts. Never accept a semantic catalog, physical mapping, or
relationship role from an end-user prompt or from the SQL-producing model.

`SemanticCatalog.to_dict()` returns a public view without physical table names,
column names, join columns, or metric-filter values. The complete local mapping
is available only through explicit `include_physical=True`; filter values need
the additional `include_values=True`. Applications making a schema-hiding
claim must still audit their complete model payload, including examples and
other prompt sections.

## Define and bind semantics

```python
from sqlguard import (
    MeasureIntent,
    Policy,
    QueryIntent,
    RelationshipRole,
    SQLGuard,
    SemanticCatalog,
    SemanticEntity,
    SemanticEvidence,
    SemanticFilter,
    SemanticMeasure,
)

semantic_catalog = SemanticCatalog(
    version="finance-2026-10",
    entities=[
        SemanticEntity("purchase", table="orders", grain=["id"]),
        SemanticEntity("item", table="line_items", grain=["id"]),
    ],
    measures=[
        SemanticMeasure(
            "order_revenue",
            entity="purchase",
            aggregation="sum",
            column="amount",
            source_grain=["id"],
            mandatory_filters=[SemanticFilter("status", "eq", ["paid"])],
        ),
    ],
    relationships=[
        RelationshipRole(
            "purchase_items",
            from_entity="purchase",
            to_entity="item",
            from_columns=["id"],
            to_columns=["order_id"],
            cardinality="one_to_many",
        ),
    ],
    evidence=[SemanticEvidence("governance://finance", version="7")],
)

guard = SQLGuard(
    catalog,
    Policy(intent_enforcement="block"),
    semantic_catalog=semantic_catalog,
)

intent = QueryIntent(
    measures=[
        MeasureIntent(
            "sum",
            column="amount",
            table="orders",
            alias="revenue",
            semantic_id="order_revenue",
        )
    ]
)
result = guard.validate(generated_sql, intent=intent)
```

Construction binds every entity, dimension, measure, grain column, filter
column, and relationship to the physical `Catalog`. Unknown mappings and
relationships without a declared physical foreign key raise `CatalogError`
immediately, before any query is validated.

## Runtime behavior

Semantic checks use the existing `intent_enforcement` rollout mode:

- `off` (default) skips intent and semantic validation;
- `warn` records warning findings while allowing otherwise valid SQL;
- `block` makes semantic findings execution-blocking errors.

This first delivery slice checks:

- whether a `MeasureIntent.semantic_id` exists in the bound snapshot;
- whether its aggregate, column, table, and distinctness match governance;
- whether every metric-owned filter is present without exposing its value;
- whether directly joined semantic entities have a declared role; and
- whether a direct one-to-many or many-to-many traversal can duplicate a
  measure before aggregation.

The catalog has a canonical SHA-256 `digest`. Input ordering does not affect
it, but semantic definitions, physical mappings, governed filter values,
evidence, or version changes do. The digest is included in `QueryStats` and
semantic violation metadata so audits can identify the exact snapshot used.

## Current boundary

The direct fanout check is intentionally conservative. This slice does not yet
certify user-selected route handles, role-specific join predicates,
pre-aggregation subqueries, multi-hop routes, or general output-grain
compatibility. Those require deterministic lowering and explicit undecidable
results; until then, SQLGuard does not claim that arbitrary rewritten joins are
safe. dbt Semantic Layer/MetricFlow import is also a later adapter—the internal
catalog does not depend on dbt.

Stable findings introduced by this layer are
`unknown_semantic_object`, `metric_definition_mismatch`,
`relationship_role_mismatch`, and `fanout_aggregation_risk`.
