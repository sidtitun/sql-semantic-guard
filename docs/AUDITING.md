# Audit and observability

Attach a sink to the base policy to receive one immutable `AuditRecord` per
validation attempt, including early rejections, internal errors, and unknown
roles. Role overlays inherit the sink and cannot disable it.

```python
import json
from sqlguard import Policy, SQLGuard

# Supply an application-owned, thread-safe destination.
def audit(record):
    print(json.dumps(record.to_dict()))

guard = SQLGuard(catalog, Policy(audit_sink=audit))
result = guard.validate(sql_from_llm, params=trusted_context)
```

Records contain the UTC start time (`at`), dialect, selected role, SHA-256 of
the original SQL, sorted parameter names, `valid` and `would_block`, distinct
violation codes and rewrite kinds, resolved tables, estimated bytes, and
validation duration in milliseconds. Cost/table metadata may be incomplete
when a query exits early. `error_type` reports configuration or internal error
class names; messages and parameter values are excluded. `to_dict()` produces
a JSON-compatible snapshot. Records and their tuple fields are immutable.

`valid` and `would_block` mirror the validation result: ordinary rejections
have `valid=False`; shadow-mode passes have both flags true. Unknown-role
configuration errors still raise `PolicyError`, with `valid=False` and
`error_type="PolicyError"` in the record. Auditing does not authenticate callers:
the application must derive roles and tenant parameters from trusted context.

Raw SQL is omitted by default. `Policy(audit_include_sql=True)` includes the
original input in `record.sql`; this can contain credentials or personal data.
Hashes permit correlation but do not anonymize guessable SQL. Table, role, and
parameter names can also be sensitive: control access and retention at the sink.

Sinks run synchronously after validation; their delivery time is excluded from
`duration_ms`. Use a bounded application queue for slow destinations, with your
own backpressure and retention policy. Shared guards require thread-safe sinks.
Sink exceptions are swallowed and a generic warning is logged without exception
contents. Delivery is best effort, not a durable/compliance delivery guarantee.
A failed sink does not prevent other observers from receiving the record.

## OpenTelemetry (optional)

```sh
pip install 'sql-semantic-guard[otel]'
```

Configure your application's OpenTelemetry SDK providers and exporters, then:

```python
from sqlguard.otel import instrument

uninstall = instrument(guard)  # once, during startup
# validate() and validate_or_raise() now emit telemetry
# uninstall() removes only this instrumentation; safe to call repeatedly
```

The extra installs only the API; the application owns the SDK and exporter.
Without SDK configuration the API performs no export. Optional
`tracer_provider=` and `meter_provider=` arguments support isolated providers.
See the [OpenTelemetry Python instrumentation guide](https://opentelemetry.io/docs/languages/python/instrumentation/).

Each completed attempt emits a `sqlguard.validate` span timed over validation,
under the caller's active span. These completion spans do not become current
inside the validation pipeline. Attributes mirror audit metadata under
`sqlguard.*`; **raw SQL is never exported**, even if an audit sink opts into it.

Counters use bounded dimensions:

| Metric | Attribute | Values |
| --- | --- | --- |
| `sqlguard.validations` | `verdict` | `allowed`, `shadow`, `blocked` |
| `sqlguard.violations` | `code` | One increment per distinct violation code per attempt |

Roles, SQL hashes, parameter names, and tables are span attributes only, never
metric dimensions. Install/uninstall instrumentation before serving requests;
validation records themselves remain isolated across concurrent calls.
