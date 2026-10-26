# `intent_undecidable`

SQL Semantic Guard cannot deterministically compare the outer query shape with
the supplied `QueryIntent`. In `block` mode the query fails closed; in `warn`
mode it is allowed with a warning.

Rewrite the result as one outer `SELECT`, or run in warning mode while the
application adjudicates unsupported shapes.
