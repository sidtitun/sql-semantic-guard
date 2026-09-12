"""Catalog construction, dialect handling, and policy validation."""

from __future__ import annotations

import json

import pytest

from sqlguard import Catalog, Column, Policy, SQLGuard, Table
from sqlguard.errors import CatalogError, PolicyError


def test_simple_dict_catalog():
    cat = Catalog.from_dict({"t": {"a": "int", "b": "text"}})
    assert cat.tables[0].name == "t"
    assert cat.tables[0].column("a").type == "int"


def test_nested_dict_catalog():
    cat = Catalog.from_dict({"public": {"t": {"a": "int"}}}, nested=True)
    assert cat.tables[0].schema == "public"
    assert cat.has_schemas


def test_auto_detect_nested():
    cat = Catalog.from_dict({"public": {"orders": {"id": "int", "amount": "decimal"}}})
    assert cat.has_schemas


def test_auto_detect_flat():
    cat = Catalog.from_dict({"orders": {"id": "int", "amount": "decimal"}})
    assert not cat.has_schemas


def test_rich_table_spec():
    cat = Catalog.from_dict(
        {
            "orders": {
                "columns": {"id": "int", "dt": "string"},
                "row_count": 1000,
                "partition_columns": ["dt"],
            }
        }
    )
    t = cat.tables[0]
    assert t.row_count == 1000
    assert t.partition_columns == ("dt",)


def test_column_tags():
    cat = Catalog.from_dict({"t": {"ssn": {"type": "text", "tags": ["pii"]}}})
    assert "pii" in cat.tables[0].column("ssn").tags


def test_partition_column_must_exist():
    with pytest.raises(CatalogError):
        Table(name="t", columns=[Column("id", "int")], partition_columns=("missing",))


def test_duplicate_columns_rejected():
    with pytest.raises(CatalogError):
        Table(name="t", columns=[Column("id", "int"), Column("id", "text")])


def test_duplicate_tables_rejected():
    with pytest.raises(CatalogError):
        Catalog(tables=[Table("t", [Column("a")]), Table("t", [Column("b")])])


def test_mixed_schema_requires_default():
    with pytest.raises(CatalogError):
        Catalog(
            tables=[
                Table("a", [Column("x")], schema="public"),
                Table("b", [Column("y")]),
            ]
        )


def test_mixed_schema_with_default_ok():
    cat = Catalog(
        tables=[
            Table("a", [Column("x")], schema="public"),
            Table("b", [Column("y")]),
        ],
        default_schema="public",
    )
    assert all(t.schema == "public" for t in cat.tables)


def test_json_roundtrip():
    original = Catalog.from_dict(
        {
            "orders": {
                "columns": {"id": "bigint", "dt": "string"},
                "row_count": 5,
                "partition_columns": ["dt"],
            }
        }
    )
    restored = Catalog.from_json(json.dumps(original.to_dict()))
    assert restored.tables[0].row_count == 5
    assert restored.tables[0].partition_columns == ("dt",)


def test_unknown_dialect_rejected():
    cat = Catalog.from_dict({"t": {"a": "int"}})
    with pytest.raises(PolicyError):
        SQLGuard(cat, dialect="not_a_real_dialect")


def test_dialect_aliases():
    cat = Catalog.from_dict({"t": {"a": "int"}})
    assert SQLGuard(cat, dialect="postgresql").dialect == "postgres"
    assert SQLGuard(cat, dialect="pg").dialect == "postgres"


def test_policy_default_limit_exceeds_max_rejected():
    with pytest.raises(PolicyError):
        Policy(default_limit=100, max_limit=10)


def test_policy_negative_limit_rejected():
    with pytest.raises(PolicyError):
        Policy(default_limit=-5)


def test_policy_bad_rls_strategy_rejected():
    with pytest.raises(PolicyError):
        Policy(rls_strategy="teleport")


def test_policy_write_statement_rejected():
    with pytest.raises(PolicyError):
        Policy(allowed_statements=("select", "insert"))


def test_function_denylist_default_postgres(catalog):
    guard = SQLGuard(catalog, Policy(on_missing_stats="ignore"), dialect="postgres")
    result = guard.validate("SELECT id FROM orders WHERE pg_sleep(10) IS NULL")
    assert not result.valid
    assert any(v.code.value == "forbidden_function" for v in result.errors)


def test_function_allowlist_mode(catalog):
    guard = SQLGuard(
        catalog,
        Policy(
            function_allowlist=frozenset({"count", "sum"}),
            on_missing_stats="ignore",
        ),
    )
    ok = guard.validate("SELECT count(*) FROM orders")
    assert ok.valid
    bad = guard.validate("SELECT md5(status) FROM orders")
    assert not bad.valid


def test_extra_function_denylist(catalog):
    guard = SQLGuard(
        catalog,
        Policy(extra_function_denylist=frozenset({"md5"}), on_missing_stats="ignore"),
    )
    result = guard.validate("SELECT md5(status) FROM orders")
    assert not result.valid


def test_policy_prompt_includes_schema_and_rules(guard):
    prompt = guard.policy_prompt()
    assert "orders" in prompt
    assert "customer_id" in prompt
    assert "read-only" in prompt.lower()


def test_policy_prompt_describes_tag_rule_not_glob(guard):
    """A tag-based deny rule must read as English, not a meaningless '*.*'."""
    prompt = guard.policy_prompt()
    assert "*.*" not in prompt
    assert "tagged pii" in prompt


def test_policy_prompt_lists_named_column_rule(catalog):
    from sqlguard import ColumnRule

    g = SQLGuard(
        catalog,
        Policy(
            column_rules=[ColumnRule(table="orders", column="notes", action="deny")],
            on_missing_stats="ignore",
        ),
    )
    assert "notes" in g.policy_prompt()


def test_result_serializes_to_json(guard):
    result = guard.validate("SELECT id FROM orders", params={"customer_id": 1})
    payload = json.dumps(result.to_dict())
    assert '"valid"' in payload


def test_validate_or_raise(guard):
    from sqlguard.errors import ValidationFailed

    with pytest.raises(ValidationFailed) as exc:
        guard.validate_or_raise("DELETE FROM orders", params={"customer_id": 1})
    assert exc.value.result.errors

    valid = guard.validate_or_raise("SELECT id FROM orders", params={"customer_id": 1})
    assert valid.valid
