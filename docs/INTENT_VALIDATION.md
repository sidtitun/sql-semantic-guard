# Query intent validation

SQL Semantic Guard can validate that the final SQL matches an independently
produced result contract. This closes a gap that schema, type, policy, and cost
checks cannot close: a query can be valid and safe but still answer the wrong
question.

## Trust boundary

`QueryIntent` is structured input from the application. Produce it from a
trusted UI workflow, a governed semantic planner, or a separately validated
question-understanding step. Do not derive it from the SQL under review; that
would only prove that the SQL agrees with itself.

Intent checking is deterministic and does not call an LLM or a database. It
runs after SQLGuard's safe rewrites, so the contract describes the exact query
that would be returned for execution.

## Contract

```python
from sqlguard import (
    DimensionIntent,
    FilterIntent,
    MeasureIntent,
    OrderIntent,
    QueryIntent,
)

intent = QueryIntent(
    measures=[
        MeasureIntent("sum", column="amount", alias="revenue"),
        MeasureIntent("count", column="customer_id", distinct=True, alias="buyers"),
    ],
    dimensions=[
        DimensionIntent("created_at", time_grain="month", alias="month"),
    ],
    filters=[
        FilterIntent("status", "eq", ("paid",)),
        FilterIntent("amount", "between", (10, 500)),
    ],
    ordering=[OrderIntent("revenue", "desc")],
    distinct=False,
    limit=10,
    expected_grain=["month"],
)
```

Supported aggregates are `avg`, `count`, `max`, `min`, and `sum`. Supported
time grains are year through hour. Filters support equality/inequality,
comparisons, `IN`, `BETWEEN`, and null tests. Placeholder values are resolved
from the `params` passed to `validate()`.

All declared constraints are required. The contract is deliberately partial:
omit a facet when the application does not know it. An empty `QueryIntent`
therefore imposes no semantic requirements.

## Enforcement and rollout

```python
policy = Policy(intent_enforcement="warn")  # off | warn | block
result = SQLGuard(catalog, policy).validate(sql, params, intent=intent)
```

| Mode | Behavior |
|---|---|
| `off` | Skip intent checking. This is the backward-compatible default. |
| `warn` | Record warning violations but allow otherwise valid SQL. |
| `block` | Treat mismatches and undecidable query shapes as errors. |

Start with `warn`, measure mismatch categories, adjudicate false positives,
then enable `block` for the intent facets your planner produces reliably. Role
overlays may tighten enforcement (`off` → `warn` → `block`) but cannot weaken
the base policy.

## Results and privacy

`result.stats.intent` is an `IntentCheckResult` with `confirmed`, `missing`,
`conflicting`, and `undecidable` constraint identifiers. `matched` is true only
when the latter three lists are empty. `observed` exposes counts and structural
properties, not filter values.

Filter values are compared internally but excluded from intent serialization,
violations, and `feedback()` by default:

```python
intent.to_dict()                       # value_count only
intent.to_dict(include_values=True)    # explicit opt-in
```

The stable violation classes are:

- `intent_constraint_missing`: a required facet is absent.
- `intent_constraint_conflict`: the field exists with the wrong operation,
  grain, direction, value, distinctness, or limit.
- `intent_undecidable`: the checker cannot prove the contract for the outer
  query shape. In block mode this fails closed.

## Current boundary

The first release validates one outer `SELECT`. Set-operation roots such as
`UNION` are reported as undecidable rather than silently accepted. This API
does not infer intent from natural language, validate business metric
definitions, or prove result equivalence. Governed metrics and relationship
roles are the next layer planned on top of this contract.
