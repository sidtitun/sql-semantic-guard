"""Exercise real OTel SDK exporters, with no network or global providers."""
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from sqlguard import Policy, PolicyOverlay, PolicySet, SQLGuard
from sqlguard.otel import instrument


def test_exports_private_spans_and_bounded_metrics(catalog):
    exporter = InMemorySpanExporter()
    tracer = TracerProvider(sampler=ALWAYS_ON)
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader])
    records = []
    guard = SQLGuard(catalog, PolicySet(
        Policy(audit_sink=records.append, audit_include_sql=True, enforcement="log_only"),
        {"reader": PolicyOverlay()},
    ))
    uninstall = instrument(guard, tracer_provider=tracer, meter_provider=meter)
    with tracer.get_tracer("test").start_as_current_span("request") as parent:
        guard.validate("SELECT id FROM orders WHERE id = 192387", {"tenant": "SECRET"}, role="reader")
        guard.validate("SELECT missing FROM orders")
        guard.validate("DELETE FROM orders")
    spans = [s for s in exporter.get_finished_spans() if s.name == "sqlguard.validate"]
    assert len(spans) == 3
    assert all(s.parent.span_id == parent.get_span_context().span_id for s in spans)
    assert all(s.end_time > s.start_time for s in spans)
    assert spans[0].attributes["sqlguard.role"] == "reader"
    assert "sqlguard.estimated_bytes" in spans[0].attributes
    assert spans[1].attributes["sqlguard.would_block"]
    assert "SECRET" not in repr(spans[0].attributes)
    assert "192387" not in repr(spans[0].attributes)
    metrics = {m.name: m for r in reader.get_metrics_data().resource_metrics for s in r.scope_metrics for m in s.metrics}
    counts = {p.attributes["verdict"]: p.value for p in metrics["sqlguard.validations"].data.data_points}
    assert counts == {"allowed": 1, "shadow": 1, "blocked": 1}
    assert all(set(p.attributes) == {"code"} for p in metrics["sqlguard.violations"].data.data_points)
    uninstall()
    uninstall()
    guard.validate("SELECT 1")
    assert len(exporter.get_finished_spans()) == 4  # three validations and parent
    assert len(records) == 4
    tracer.shutdown()
    meter.shutdown()


def test_instrumentation_without_policy_sink_and_failure_isolation(catalog, monkeypatch):
    exporter = InMemorySpanExporter()
    tracer = TracerProvider(sampler=ALWAYS_ON)
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    guard = SQLGuard(catalog)
    instrument(guard, tracer_provider=tracer)

    def broken(*args):
        raise RuntimeError("PRIVATE")

    monkeypatch.setattr(guard, "_validate", broken)
    assert not guard.validate("SELECT 1").valid
    assert exporter.get_finished_spans()[0].attributes["sqlguard.error_type"] == "RuntimeError"
    tracer.shutdown()
