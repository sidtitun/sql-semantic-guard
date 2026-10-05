"""Optional OpenTelemetry export; no SDK or global provider is configured here."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlguard.audit import AuditRecord

if TYPE_CHECKING:
    from sqlguard.guard import SQLGuard


def instrument(
    guard: SQLGuard, *, tracer_provider: Any = None, meter_provider: Any = None
) -> Callable[[], None]:
    """Export completed validations as spans/counters; return an uninstall callback.

    Call once at startup. Configure SDK providers/exporters in the application.
    Spans describe the validation interval but are not current during validation.
    Raw SQL is never exported, even when the audit sink opts into it.
    """
    from opentelemetry import metrics, trace

    tracer = trace.get_tracer("sqlguard", tracer_provider=tracer_provider)
    meter = metrics.get_meter("sqlguard", meter_provider=meter_provider)
    validations = meter.create_counter("sqlguard.validations", unit="1")
    violations = meter.create_counter("sqlguard.violations", unit="1")

    def observe(record: AuditRecord) -> None:
        verdict = "blocked" if not record.valid else "shadow" if record.would_block else "allowed"
        attributes: dict[str, Any] = {
            "sqlguard.dialect": record.dialect,
            "sqlguard.sql_sha256": record.sql_sha256,
            "sqlguard.param_keys": record.param_keys,
            "sqlguard.valid": record.valid,
            "sqlguard.would_block": record.would_block,
            "sqlguard.violation_codes": record.violation_codes,
            "sqlguard.rewrite_kinds": record.rewrite_kinds,
            "sqlguard.tables": record.tables,
            "sqlguard.duration_ms": record.duration_ms,
        }
        if record.role is not None:
            attributes["sqlguard.role"] = record.role
        if record.estimated_bytes is not None:
            attributes["sqlguard.estimated_bytes"] = record.estimated_bytes
        if record.error_type is not None:
            attributes["sqlguard.error_type"] = record.error_type
        start = int(record.at.timestamp() * 1_000_000_000)
        span = tracer.start_span("sqlguard.validate", attributes=attributes, start_time=start)
        span.end(end_time=start + int(record.duration_ms * 1_000_000))
        # Keep metric dimensions bounded: no SQL hashes, table names or roles.
        validations.add(1, {"verdict": verdict})
        for code in record.violation_codes:
            violations.add(1, {"code": code})

    guard._audit_observers.append(observe)

    def uninstall() -> None:
        if observe in guard._audit_observers:
            guard._audit_observers.remove(observe)

    return uninstall
