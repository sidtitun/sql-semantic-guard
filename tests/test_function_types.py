from unittest.mock import patch

import pytest

from sqlguard import Catalog, Code, Policy, SQLGuard, semantics
from sqlguard.functions import signature_registry


def validate(expression, **options):
    return SQLGuard(
        Catalog.from_dict({"t": {"n": "integer", "s": "text", "ts": "timestamp"}}),
        Policy(default_limit=None, on_missing_stats="ignore", **options),
        dialect="postgres",
    ).validate(f"SELECT {expression} FROM t")


@pytest.mark.parametrize("expression", ["lower(n)", "abs(s)", "round(s)", "split_part(s, s, s)"])
def test_known_conflicts_warn_not_block(expression):
    result = validate(expression)
    assert result.valid
    assert any(
        v.code == Code.FUNCTION_MISUSE and v.extra["failure"] == "argument_type"
        for v in result.warnings
    )


@pytest.mark.parametrize(
    "expression",
    [
        "lower(s)",
        "abs(n)",
        "date_trunc('day', ts)",
        "substring(s, n, n)",
        "substring(s, s)",
        "abs(NULL)",
        "abs('12')",
        "company_function(s)",
        "count(*)",
        "lower(CAST(n AS text))",
    ],
)
def test_valid_or_uncertain_types_are_tolerated(expression):
    result = validate(expression)
    assert not any(v.code == Code.FUNCTION_MISUSE for v in result.violations)


def test_policy_flags_are_independent():
    assert validate("lower(n)", check_types=False).warnings
    assert not validate("lower(n)", check_function_signatures=False).warnings


def test_single_annotation_pass():
    with patch.object(semantics, "annotate_types", wraps=semantics.annotate_types) as annotate:
        validate("lower(n)")
        assert annotate.call_count == 1


def test_annotation_failure_is_visible():
    with patch.object(semantics, "annotate_types", side_effect=ValueError):
        result = validate("lower(n)")
    assert "function_type_checks" in result.stats.checks_skipped
    assert "type_checks" in result.stats.checks_skipped


def test_cached_registry_is_read_only():
    with pytest.raises(TypeError):
        signature_registry("postgres")["abs"] = None
