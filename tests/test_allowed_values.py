"""Catalog domains catch plausible-looking but invalid business values."""

import pytest

from sqlguard import Catalog, Policy, SQLGuard
from sqlguard.reflect import profile_allowed_values
from sqlguard.violations import Code

CATALOG = Catalog.from_dict(
    {
        "orders": {
            "columns": {
                "id": "bigint",
                "status": {
                    "type": "text",
                    "allowed_values": ["pending", "shipped", "cancelled"],
                },
                "notes": "text",
                "amount": "decimal(10,2)",
            }
        }
    }
)


def _validate(sql: str, *, case_insensitive: bool = False):
    return SQLGuard(
        CATALOG,
        Policy(
            on_missing_stats="ignore",
            default_limit=None,
            case_insensitive_enums=case_insensitive,
        ),
        dialect="postgres",
    ).validate(sql)


def test_typo_is_blocked_with_suggestion_and_domain() -> None:
    result = _validate("SELECT id FROM orders WHERE status = 'shiped'")

    violation = next(v for v in result.errors if v.code is Code.UNKNOWN_VALUE)
    assert "Did you mean 'shipped'?" in violation.message
    assert violation.hint == "Allowed values: 'pending', 'shipped', 'cancelled'"


def test_valid_value_and_reversed_comparison_are_clean() -> None:
    assert _validate("SELECT id FROM orders WHERE status = 'shipped'").valid
    assert _validate("SELECT id FROM orders WHERE 'pending' != status").valid


def test_in_list_reports_each_unknown_value_once() -> None:
    result = _validate(
        "SELECT id FROM orders WHERE status IN ('pending', 'shiped', 'lost', 'lost')"
    )
    unknowns = [v for v in result.errors if v.code is Code.UNKNOWN_VALUE]

    assert {v.extra["value"] for v in unknowns} == {"shiped", "lost"}


def test_case_insensitive_mode_is_explicit() -> None:
    assert not _validate("SELECT id FROM orders WHERE status = 'SHIPPED'").valid
    assert _validate(
        "SELECT id FROM orders WHERE status = 'SHIPPED'", case_insensitive=True
    ).valid


def test_non_domain_and_range_predicates_are_exempt() -> None:
    assert _validate("SELECT id FROM orders WHERE notes = 'anything'").valid
    assert _validate("SELECT id FROM orders WHERE status > 'zzz'").valid
    assert _validate("SELECT id FROM orders WHERE status LIKE 'ship%'").valid


def test_allowed_values_round_trip() -> None:
    round_tripped = Catalog.from_dict(CATALOG.to_dict())
    status = round_tripped.tables[0].column("status")
    assert status is not None
    assert status.allowed_values == ("pending", "shipped", "cancelled")


def test_profile_allowed_values_with_sqlalchemy() -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE states (value TEXT)")
        connection.exec_driver_sql(
            "INSERT INTO states(value) VALUES ('pending'), ('shipped'), ('pending'), (NULL)"
        )

    assert profile_allowed_values(engine, "states", "value") == ("pending", "shipped")
    with pytest.raises(ValueError, match="more than 1 distinct"):
        profile_allowed_values(engine, "states", "value", max_distinct=1)
    with pytest.raises(ValueError, match="must be positive"):
        profile_allowed_values(engine, "states", "value", max_distinct=0)
