# `metric_definition_mismatch`

The intent's aggregate, physical binding, or distinctness differs from the
governed measure, or the SQL omits/conflicts with a metric-owned filter.
Resolve the measure from the current semantic snapshot and generate SQL from
that definition. Diagnostics report counts and IDs without predicate values.
