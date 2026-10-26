# `intent_constraint_conflict`

The final SQL contains the requested field but uses semantics that conflict
with `QueryIntent`, such as a different aggregation, time grain, filter value,
order direction, distinctness, or exact limit.

Regenerate the SQL from the trusted contract. Diagnostics never include the
compared filter values.
