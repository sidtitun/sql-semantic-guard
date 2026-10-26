# `intent_constraint_missing`

The final SQL does not contain a measure, dimension, filter, ordering, grain,
or limit required by the supplied `QueryIntent`.

Add the missing facet without weakening row-level security, column policy, or
cost controls. The violation's `extra.intent_constraint` identifies the facet;
filter values are intentionally omitted.
