"""Role policy composition stays additive and cannot weaken the base policy."""

from __future__ import annotations

import pytest

from sqlguard import ColumnRule, Policy, PolicyOverlay, PolicySet, RLSRule, SQLGuard
from sqlguard.errors import PolicyError


def _guard(catalog) -> SQLGuard:
    base = Policy(
        rls=[RLSRule(table="orders", column="customer_id", param="tenant")],
        column_rules=[ColumnRule(table="orders", column="ssn", action="deny")],
        default_limit=None,
        max_bytes_scanned=2 << 30,
        on_missing_stats="ignore",
    )
    return SQLGuard(
        catalog,
        PolicySet(
            base=base,
            roles={
                "analyst": PolicyOverlay(
                    add_column_rules=[
                        ColumnRule(
                            table="customers", column="email", action="mask", mask_with="redact"
                        )
                    ]
                ),
                "support": PolicyOverlay(
                    add_column_rules=[ColumnRule(table="customers", column="email", action="deny")],
                    max_bytes_scanned=1 << 30,
                ),
                "regional": PolicyOverlay(
                    add_rls=[RLSRule(table="customers", column="region", param="region")]
                ),
            },
        ),
    )


def test_one_guard_applies_role_specific_column_visibility(catalog):
    guard = _guard(catalog)

    analyst = guard.validate("SELECT email FROM customers", role="analyst", params={"tenant": 7})
    support = guard.validate("SELECT email FROM customers", role="support", params={"tenant": 7})

    assert analyst.valid
    assert "'***'" in analyst.sql
    assert analyst.stats.role == "analyst"
    assert not support.valid
    assert any(error.column == "email" for error in support.errors)
    assert support.stats.role == "support"


def test_role_adds_its_own_row_filter(catalog):
    guard = _guard(catalog)

    missing = guard.validate("SELECT id FROM customers", role="regional", params={"tenant": 7})
    scoped = guard.validate(
        "SELECT id FROM customers", role="regional", params={"tenant": 7, "region": "APAC"}
    )

    assert not missing.valid
    assert scoped.valid
    assert "customers.region = 'APAC'" in scoped.sql


def test_base_denial_cannot_be_resurrected_by_a_role(catalog):
    guard = SQLGuard(
        catalog,
        PolicySet(
            base=Policy(
                column_rules=[ColumnRule(table="orders", column="ssn", action="deny")],
                default_limit=None,
                on_missing_stats="ignore",
            ),
            roles={
                "support": PolicyOverlay(
                    add_column_rules=[
                        ColumnRule(table="orders", column="ssn", action="mask", mask_with="redact")
                    ]
                )
            },
        ),
    )

    result = guard.validate("SELECT ssn FROM orders", role="support")

    assert not result.valid
    assert any(error.column == "ssn" for error in result.errors)


def test_role_may_only_tighten_resource_limits():
    base = Policy(max_bytes_scanned=1000)
    with pytest.raises(PolicyError, match="would loosen"):
        PolicySet(base=base, roles={"wide": PolicyOverlay(max_bytes_scanned=2000)})

    resolved = PolicySet(
        base=base, roles={"small": PolicyOverlay(max_bytes_scanned=500)}
    ).resolve("small")
    assert resolved.max_bytes_scanned == 500


def test_unknown_role_and_role_without_policy_set_are_rejected(catalog):
    guard = _guard(catalog)
    with pytest.raises(PolicyError, match="unknown role"):
        guard.validate("SELECT id FROM orders", role="missing")

    plain = SQLGuard(catalog, Policy(default_limit=None, on_missing_stats="ignore"))
    with pytest.raises(PolicyError, match="no PolicySet"):
        plain.validate("SELECT id FROM orders", role="analyst")


def test_role_prompt_uses_resolved_policy(catalog):
    guard = _guard(catalog)

    analyst_prompt = guard.policy_prompt(role="analyst")
    support_prompt = guard.policy_prompt(role="support")

    assert "masked automatically" in analyst_prompt
    assert "email" in support_prompt


def test_all_role_config_is_validated_when_the_guard_starts(catalog):
    with pytest.raises(PolicyError, match="not in the catalog"):
        SQLGuard(
            catalog,
            PolicySet(
                base=Policy(default_limit=None),
                roles={
                    "broken": PolicyOverlay(
                        add_rls=[RLSRule(table="missing", column="tenant", param="tenant")]
                    )
                },
            ),
        )
