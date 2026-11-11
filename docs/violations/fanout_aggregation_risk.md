# `fanout_aggregation_risk`

A direct one-to-many or many-to-many traversal can duplicate rows at the
measure's source grain before aggregation. Aggregate at the governed source
grain before traversing the relationship, or avoid the fanout. The current
slice does not certify arbitrary pre-aggregation shapes, so those remain a
planned deterministic-lowering feature.
