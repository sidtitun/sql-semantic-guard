"""Exception types raised by sqlguard.

The validation pipeline itself never raises for problems found *in the SQL* —
those are reported as :class:`~sqlguard.violations.Violation` entries on the
:class:`~sqlguard.violations.ValidationResult`. Exceptions are reserved for
configuration mistakes (bad policy, bad catalog) and for the opt-in
``validate_or_raise`` convenience.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from sqlguard.violations import ValidationResult


class SQLGuardError(Exception):
    """Base class for all sqlguard exceptions."""


class CatalogError(SQLGuardError):
    """The schema catalog is malformed (duplicate tables, mixed schemas, ...)."""


class PolicyError(SQLGuardError):
    """The guard policy is misconfigured (unknown dialect, bad RLS rule, ...)."""


class ValidationFailed(SQLGuardError):
    """Raised by ``SQLGuard.validate_or_raise`` when validation fails.

    Carries the full :class:`ValidationResult` so callers can still access the
    structured violation report (e.g. to feed back to an LLM).
    """

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        errors = [v for v in result.violations if v.is_error]
        summary = "; ".join(v.message for v in errors[:3])
        if len(errors) > 3:
            summary += f" (+{len(errors) - 3} more)"
        super().__init__(f"SQL failed validation with {len(errors)} error(s): {summary}")
