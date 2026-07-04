"""Type checking: catch comparisons the engine will reject or silently break."""

from __future__ import annotations

from sqlguard import Code, Severity


def test_string_vs_numeric_column(plain_guard):
    result = plain_guard.validate("SELECT id FROM orders WHERE amount = 'expensive'")
    assert any(v.code == Code.TYPE_MISMATCH for v in result.errors)


def test_numeric_literal_vs_text_column(plain_guard):
    result = plain_guard.validate("SELECT id FROM orders WHERE status = 42")
    assert any(v.code == Code.TYPE_MISMATCH for v in result.errors)


def test_bad_date_literal(plain_guard):
    result = plain_guard.validate(
        "SELECT id FROM orders WHERE created_at > 'last tuesday'"
    )
    assert any(v.code == Code.TYPE_MISMATCH for v in result.errors)


def test_iso_date_string_ok(plain_guard):
    result = plain_guard.validate(
        "SELECT id FROM orders WHERE created_at > '2026-01-01'"
    )
    assert not any(v.code == Code.TYPE_MISMATCH for v in result.violations)


def test_iso_timestamp_string_ok(plain_guard):
    result = plain_guard.validate(
        "SELECT id FROM orders WHERE created_at > '2026-01-01 12:30:00'"
    )
    assert not any(v.code == Code.TYPE_MISMATCH for v in result.violations)


def test_numeric_string_against_numeric_ok(plain_guard):
    # '100' is coercible to numeric; don't cry wolf
    result = plain_guard.validate("SELECT id FROM orders WHERE amount > '100'")
    assert not any(
        v.code == Code.TYPE_MISMATCH and v.severity == Severity.ERROR
        for v in result.violations
    )


def test_matching_types_ok(plain_guard):
    result = plain_guard.validate(
        "SELECT id FROM orders WHERE amount > 100 AND status = 'shipped'"
    )
    assert not any(v.code == Code.TYPE_MISMATCH for v in result.violations)


def test_between_type_check(plain_guard):
    result = plain_guard.validate(
        "SELECT id FROM orders WHERE amount BETWEEN 'a' AND 'z'"
    )
    assert any(v.code == Code.TYPE_MISMATCH for v in result.errors)


def test_in_list_type_check(plain_guard):
    result = plain_guard.validate(
        "SELECT id FROM orders WHERE amount IN ('cheap', 'pricey')"
    )
    assert any(v.code == Code.TYPE_MISMATCH for v in result.errors)


def test_column_to_column_same_family_ok(plain_guard):
    result = plain_guard.validate(
        "SELECT o.id FROM orders o JOIN line_items li ON li.order_id = o.id"
    )
    assert not any(v.code == Code.TYPE_MISMATCH for v in result.violations)


def test_types_disabled(catalog):
    from sqlguard import Policy, SQLGuard

    guard = SQLGuard(catalog, Policy(check_types=False, on_missing_stats="ignore"))
    result = guard.validate("SELECT id FROM orders WHERE amount = 'expensive'")
    assert not any(v.code == Code.TYPE_MISMATCH for v in result.violations)
    assert "type_checks" in result.stats.checks_skipped
