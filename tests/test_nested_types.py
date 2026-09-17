"""Nested catalog types are validated instead of treated as opaque aliases."""

from __future__ import annotations

import pytest

from sqlguard import Catalog, Code, Policy, SQLGuard


@pytest.fixture
def nested_guard() -> SQLGuard:
    catalog = Catalog.from_dict(
        {
            "events": {
                "payload": "struct<referrer:string,device:struct<os:string,version:int>>",
                "attributes": "map<string,string>",
                "items": "array<struct<sku:string,quantity:int>>",
                "opaque": "not a real type",
            }
        }
    )
    return SQLGuard(
        catalog,
        Policy(
            default_limit=None,
            require_partition_filter=False,
            on_missing_stats="ignore",
        ),
        dialect="athena",
    )


def test_valid_struct_member_passes(nested_guard: SQLGuard) -> None:
    result = nested_guard.validate("SELECT payload.referrer FROM events")
    assert result.valid, [str(v) for v in result.errors]


def test_unknown_struct_member_is_blocked_with_suggestion(nested_guard: SQLGuard) -> None:
    result = nested_guard.validate("SELECT payload.refferer FROM events")

    violation = next(v for v in result.errors if v.code is Code.UNKNOWN_COLUMN)
    assert "refferer" in violation.message
    assert violation.hint is not None and "referrer" in violation.hint


def test_two_level_struct_path_is_validated(nested_guard: SQLGuard) -> None:
    valid = nested_guard.validate("SELECT payload.device.os FROM events")
    invalid = nested_guard.validate("SELECT payload.device.oss FROM events")

    assert valid.valid, [str(v) for v in valid.errors]
    violation = next(v for v in invalid.errors if v.code is Code.UNKNOWN_COLUMN)
    assert violation.hint is not None and "os" in violation.hint


def test_array_element_struct_member_is_validated(nested_guard: SQLGuard) -> None:
    valid = nested_guard.validate("SELECT items[1].sku FROM events")
    invalid = nested_guard.validate("SELECT items[1].skuu FROM events")

    assert valid.valid, [str(v) for v in valid.errors]
    assert any(v.code is Code.UNKNOWN_COLUMN for v in invalid.errors)


def test_map_keys_and_unparseable_types_remain_tolerant(nested_guard: SQLGuard) -> None:
    map_result = nested_guard.validate("SELECT attributes.any_key FROM events")
    opaque_result = nested_guard.validate("SELECT opaque.any_key FROM events")

    assert map_result.valid, [str(v) for v in map_result.errors]
    assert opaque_result.valid, [str(v) for v in opaque_result.errors]


def test_nested_member_type_flows_to_type_checks(nested_guard: SQLGuard) -> None:
    result = nested_guard.validate("SELECT 1 FROM events WHERE payload.device.version = 'old'")

    assert any(v.code is Code.TYPE_MISMATCH and v.is_error for v in result.violations)

