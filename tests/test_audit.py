"""Audit delivery is private by default and independent of enforcement."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from sqlguard import AuditRecord, Policy, PolicyOverlay, PolicySet, SQLGuard
from sqlguard.errors import PolicyError, ValidationFailed


def test_record_matches_result_and_excludes_secrets(catalog):
    records = []
    guard = SQLGuard(catalog, Policy(audit_sink=records.append))
    sql = "SELECT id FROM orders WHERE id = 92341"
    result = guard.validate(sql, {"tenant": "SECRET"})
    assert result.valid
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, AuditRecord)
    assert record.sql_sha256 == hashlib.sha256(sql.encode()).hexdigest()
    assert record.param_keys == ("tenant",)
    assert record.valid == result.valid
    assert record.would_block == result.would_block
    assert record.violation_codes == tuple(dict.fromkeys(v.code.value for v in result.violations))
    assert record.rewrite_kinds == tuple(dict.fromkeys(r.kind.value for r in result.rewrites))
    assert record.tables == tuple(result.stats.tables)
    assert record.estimated_bytes == result.stats.cost.bytes_scanned
    assert record.duration_ms > 0
    assert record.at.utcoffset().total_seconds() == 0
    serialized = json.dumps(record.to_dict())
    assert "SECRET" not in serialized and "92341" not in serialized
    assert record.sql is None
    with pytest.raises(FrozenInstanceError):
        record.valid = False


@pytest.mark.parametrize("sql", ["DELETE FROM orders", "SELECT FROM", "SELECT missing FROM orders"])
def test_rejections_are_audited_once(catalog, sql):
    records = []
    guard = SQLGuard(catalog, Policy(audit_sink=records.append))
    with pytest.raises(ValidationFailed):
        guard.validate_or_raise(sql)
    assert len(records) == 1
    assert not records[0].valid
    assert records[0].violation_codes


def test_shadow_mode_and_role_inherit_sink(catalog):
    records = []
    guard = SQLGuard(catalog, PolicySet(
        Policy(audit_sink=records.append, enforcement="log_only"),
        {"reader": PolicyOverlay()},
    ))
    result = guard.validate("SELECT missing FROM orders", role="reader")
    assert result.valid and result.would_block
    assert records[0].valid and records[0].would_block
    assert records[0].role == "reader"


def test_raw_sql_is_explicit_opt_in(catalog):
    records = []
    SQLGuard(catalog, Policy(audit_sink=records.append, audit_include_sql=True)).validate("SELECT 1")
    assert records[0].sql == "SELECT 1"


def test_internal_error_is_audited_without_exception_text(catalog, monkeypatch, caplog):
    records = []
    guard = SQLGuard(catalog, Policy(audit_sink=records.append))

    def broken(*args):
        raise RuntimeError("PRIVATE QUERY CONTENT")

    monkeypatch.setattr(guard, "_validate", broken)
    result = guard.validate("SELECT 1")
    assert not result.valid and result.sql is None
    assert records[0].violation_codes == ("internal_error",)
    assert records[0].error_type == "RuntimeError"
    assert "PRIVATE QUERY CONTENT" not in caplog.text


def test_sink_failure_does_not_change_result_or_leak_exception(catalog, caplog):
    def broken(record):
        raise RuntimeError("SECRET VALUE")

    guard = SQLGuard(catalog, Policy(audit_sink=broken))
    assert guard.validate("SELECT 1").valid
    assert not guard.validate("DELETE FROM orders").valid
    assert "audit sink failed" in caplog.text
    assert "SECRET VALUE" not in caplog.text


def test_unknown_role_is_audited_and_still_raises(catalog):
    records = []
    guard = SQLGuard(catalog, Policy(audit_sink=records.append))
    with pytest.raises(PolicyError):
        guard.validate("SELECT 1", role="unknown")
    assert len(records) == 1
    assert records[0].role == "unknown"
    assert records[0].error_type == "PolicyError"
    assert not records[0].valid


def test_concurrent_roles_keep_records_separate(catalog):
    records = []
    guard = SQLGuard(catalog, PolicySet(
        Policy(audit_sink=records.append),
        {"one": PolicyOverlay(), "two": PolicyOverlay()},
    ))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda role: guard.validate("SELECT 1", role=role), ["one", "two"] * 10))
    assert all(r.valid for r in results)
    assert len(records) == 20
    assert sum(r.role == "one" for r in records) == 10


def test_bad_sink_is_configuration_error():
    with pytest.raises(PolicyError, match="audit_sink"):
        Policy(audit_sink=42)


def test_failed_sink_does_not_prevent_other_observers(catalog):
    records = []

    def broken(record):
        raise RuntimeError("SECRET")

    guard = SQLGuard(catalog, Policy(audit_sink=broken))
    guard._audit_observers.append(records.append)
    result = guard.validate("DELETE FROM orders")
    assert len(records) == 1
    assert records[0].valid == result.valid
    assert records[0].would_block == result.would_block
    assert records[0].violation_codes == tuple(v.code.value for v in result.violations)


def test_audit_does_not_change_validation_output(catalog):
    records = []
    plain = SQLGuard(catalog)
    audited = SQLGuard(catalog, Policy(audit_sink=records.append))
    for sql in ("SELECT * FROM orders", "SELECT missing FROM orders", "DELETE FROM orders"):
        assert plain.validate(sql).to_dict() == audited.validate(sql).to_dict()
