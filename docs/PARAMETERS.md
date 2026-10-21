# Driver parameters

Set `Policy(paramstyle=...)` to receive rewritten SQL together with the exact
bindings in `ValidationResult.parameters`. Supply named input placeholders
(`:name`, or dialect-native named placeholders) and a mapping of trusted values.
Driver output style and SQL dialect are independent settings.

```python
from sqlguard import Catalog, Policy, RLSRule, SQLGuard

guard = SQLGuard(
    Catalog.from_dict({'orders': {'id': 'int', 'tenant_id': 'int'}}),
    Policy(
        paramstyle='qmark',
        rls=[RLSRule(table='orders', column='tenant_id', param='tenant')],
        on_missing_stats='ignore',
    ),
    dialect='sqlite',
)
result = guard.validate(
    'SELECT id FROM orders WHERE id > :minimum',
    params={'minimum': 0, 'tenant': 7},
)
if result.valid:
    if result.parameters is None:
        cursor.execute(result.sql)
    else:
        cursor.execute(result.sql, result.parameters)
```

| Policy style | Output markers | Parameters |
| --- | --- | --- |
| `named` | `:p1`, `:p2` | dictionary |
| `pyformat` | `%(p1)s`, `%(p2)s` | dictionary |
| `qmark` | `?`, `?` | list in emitted SQL order |
| `numeric` | `:1`, `:2` | list in emitted SQL order |
| `None` (default) | existing dialect rendering | `None` (legacy behavior) |

Names are generated per occurrence. Repeated input names repeat their values;
CTEs and nested expressions follow emitted SQL order rather than AST walk order.
Unused supplied values are excluded. A query with no bindings returns `None`.
Placeholder-looking text inside quoted strings remains literal text. In pyformat
output, non-placeholder percent signs are doubled for driver interpolation.
Never interpolate the returned bindings into SQL yourself.

Use `qmark` or `named` with sqlite3 and `pyformat` with psycopg's DB-API cursor.
`numeric` follows [PEP 249](https://peps.python.org/pep-0249/): it uses `:1`,
**not asyncpg's `$1`**. Native positional input and asyncpg output are not
supported in this iteration. Check the driver's supported parameter style;
SQLite numeric markers do not provide portable sequence-binding behavior.

Explicit styles also bind injected RLS values, including each member of an IN
list. Role policies inherit the style. RLS values are still used for validation
and conflict detection before output binding. Missing or invalid RLS values
block even in shadow mode in explicit driver mode. Missing SQL bind values or
unsupported positional inputs produce `parameter_error`, with no executable SQL
or parameters, even in shadow mode.

Leaving `paramstyle=None` preserves the previous API: supplied RLS values are
rendered as literals; `rls_parameterize=True` can emit unbound RLS placeholders
when values are absent. That legacy mode does not build a driver parameter object.

## Privacy and estimation

`result.parameters` contains sensitive values. Default `to_dict()` and audit
records exclude it; use `to_dict(include_parameters=True)` only for a trusted
execution boundary. Existing SQL input and RLS rewrite diagnostics can still
contain literal values: driver binding is not a general-purpose log scrubber.
Parameter values must be types accepted by your driver; the guard does not
perform driver-specific adaptation. General SQL list parameters are not expanded;
RLS IN-list expansion is supported.

The final read-only audit uses dialect SQL before the controlled placeholder
rendering step. Estimators still receive the canonical dialect SQL and AST;
this change does not add parameter passing to the estimator protocol. Live
EXPLAIN adapters may be unable to estimate queries with input placeholders and
will report their existing estimation warning. Driver binding itself does not
execute SQL.
