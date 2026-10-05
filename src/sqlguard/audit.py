"""Immutable validation decisions; raw SQL is an explicit opt-in."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class AuditRecord:
    """One validation attempt. Parameter values and error messages are never included.

    ``at`` is the UTC start time. ``duration_ms`` excludes sink delivery.
    ``error_type`` identifies configuration/internal errors without their messages.
    Hashes are identifiers, not anonymization of guessable SQL literals.
    """

    at: datetime
    dialect: str
    role: str | None
    sql_sha256: str
    param_keys: tuple[str, ...]
    valid: bool
    would_block: bool
    violation_codes: tuple[str, ...]
    rewrite_kinds: tuple[str, ...]
    tables: tuple[str, ...]
    estimated_bytes: int | None
    duration_ms: float
    error_type: str | None = None
    sql: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible snapshot, with an ISO-8601 UTC timestamp."""
        record = asdict(self)
        record["at"] = self.at.isoformat()
        return record
