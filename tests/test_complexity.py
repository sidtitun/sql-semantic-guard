"""Pre-semantics query-complexity budgets (roadmap 2.4)."""

from __future__ import annotations

import pytest

from sqlguard import Catalog, Code, Policy, SQLGuard
from sqlguard.errors import PolicyError

CATALOG = Catalog.from_dict(
    {f"t{i}": {"id": "bigint", "parent_id": "bigint"} for i in range(12)}
)


def _validate(sql: str, **limits: object):
    return SQLGuard(
        CATALOG,
        Policy(default_limit=None, on_missing_stats="ignore", **limits),
    ).validate(sql)


def _complexity_error(result, metric: str):
    return next(
        v
        for v in result.errors
        if v.code is Code.COMPLEXITY_EXCEEDED and v.extra["metric"] == metric
    )


def test_join_limit_allows_n_and_rejects_n_plus_one() -> None:
    two = "SELECT t0.id FROM t0 JOIN t1 ON t0.id = t1.id JOIN t2 ON t1.id = t2.id"
    assert _validate(two, max_joins=2).valid

    three = two + " JOIN t3 ON t2.id = t3.id"
    result = _validate(three, max_joins=2)
    error = _complexity_error(result, "joins")
    assert error.extra == {"metric": "joins", "actual": 3, "limit": 2}
    assert result.sql is None
    assert "name_binding" in result.stats.checks_skipped


def test_generated_forty_join_monster_is_rejected_before_binding() -> None:
    joins = " ".join(
        f"JOIN t0 a{i} ON a{i - 1}.id = a{i}.id" for i in range(1, 41)
    )
    result = _validate(f"SELECT a0.id FROM t0 a0 {joins}", max_joins=8)

    assert _complexity_error(result, "joins").extra["actual"] == 40
    assert result.stats.checks_run == ["parse", "statement_gate", "complexity_checks"]


def test_nested_subquery_depth_limit() -> None:
    result = _validate(
        "SELECT x.id FROM (SELECT y.id FROM (SELECT id FROM t0) y) x",
        max_subquery_depth=1,
    )
    assert _complexity_error(result, "subquery_depth").extra["actual"] == 2


def test_cte_limit() -> None:
    result = _validate(
        "WITH a AS (SELECT id FROM t0), b AS (SELECT id FROM t1) SELECT id FROM a",
        max_ctes=1,
    )
    assert _complexity_error(result, "ctes").extra["actual"] == 2


def test_union_branch_limit_counts_a_chain() -> None:
    result = _validate(
        "SELECT id FROM t0 UNION ALL SELECT id FROM t1 UNION SELECT id FROM t2",
        max_union_branches=2,
    )
    assert _complexity_error(result, "union_branches").extra["actual"] == 3


def test_expression_node_limit_and_stats_serialization() -> None:
    baseline = _validate("SELECT id FROM t0", max_expression_nodes=10_000)
    nodes = baseline.stats.complexity["expression_nodes"]
    result = _validate("SELECT id FROM t0", max_expression_nodes=nodes - 1)

    assert _complexity_error(result, "expression_nodes").extra["actual"] == nodes
    assert result.to_dict()["stats"]["complexity"]["joins"] == 0


def test_no_ast_walk_when_every_complexity_limit_is_off(monkeypatch) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("complexity traversal should remain off by default")

    monkeypatch.setattr("sqlguard.analyzer.check_complexity", fail_if_called)
    result = _validate("SELECT id FROM t0")

    assert result.valid
    assert result.stats.complexity == {}
    assert "complexity_checks" in result.stats.checks_skipped


def test_multiple_exceeded_limits_report_each_metric() -> None:
    result = _validate(
        "WITH a AS (SELECT id FROM t0) SELECT a.id FROM a JOIN t1 ON a.id = t1.id",
        max_ctes=1,
        max_joins=1,
        max_expression_nodes=1,
    )
    assert [v.extra["metric"] for v in result.errors] == ["expression_nodes"]


def test_shadow_mode_measures_but_completes_pipeline() -> None:
    guard = SQLGuard(
        CATALOG,
        Policy(
            enforcement="log_only",
            max_joins=1,
            default_limit=10,
            on_missing_stats="ignore",
        ),
    )
    result = guard.validate(
        "SELECT t0.id FROM t0 JOIN t1 ON t0.id = t1.id JOIN t2 ON t1.id = t2.id"
    )
    assert result.valid and result.would_block
    assert result.sql is not None and "LIMIT 10" in result.sql
    assert _complexity_error(result, "joins")
    assert "output_audit" in result.stats.checks_run


@pytest.mark.parametrize(
    "field",
    [
        "max_joins",
        "max_subquery_depth",
        "max_ctes",
        "max_union_branches",
        "max_expression_nodes",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_complexity_limits_must_be_positive(field: str, value: int) -> None:
    with pytest.raises(PolicyError, match=field):
        Policy(**{field: value})
