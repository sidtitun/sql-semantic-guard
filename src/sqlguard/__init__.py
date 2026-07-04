"""sql-semantic-guard: semantic firewall for LLM-generated SQL.

Validates generated SQL against a live schema catalog, enforces row-level
security, blocks writes/DDL, bounds scan cost, applies safe rewrites
(``SELECT *`` expansion, LIMIT caps), and returns structured violation
reports an LLM can use to self-repair — all before anything touches the
database.

Quickstart::

    from sqlguard import SQLGuard, Catalog, Policy, RLSRule

    catalog = Catalog.from_dict({
        "orders": {"id": "bigint", "customer_id": "bigint", "amount": "decimal(10,2)"},
    })
    guard = SQLGuard(
        catalog,
        Policy(rls=[RLSRule(table="orders", column="customer_id", param="customer_id")]),
        dialect="postgres",
    )
    result = guard.validate("SELECT * FROM orders", params={"customer_id": 42})
    if result.valid:
        run(result.sql)           # rewritten, tenant-scoped, LIMITed
    else:
        llm_repair(result.feedback())
"""

from sqlguard.catalog import Catalog, Column, Table
from sqlguard.cost import CostEstimator, EstimateInputs, HeuristicCostEstimator
from sqlguard.errors import CatalogError, PolicyError, SQLGuardError, ValidationFailed
from sqlguard.guard import SQLGuard
from sqlguard.policy import ColumnRule, Policy, RLSRule
from sqlguard.violations import (
    Code,
    CostEstimate,
    QueryStats,
    Rewrite,
    RewriteKind,
    Severity,
    TableScanEstimate,
    ValidationResult,
    Violation,
)

__version__ = "0.1.0"

__all__ = [
    "SQLGuard",
    "Catalog",
    "Table",
    "Column",
    "Policy",
    "RLSRule",
    "ColumnRule",
    "ValidationResult",
    "Violation",
    "Rewrite",
    "RewriteKind",
    "Severity",
    "Code",
    "QueryStats",
    "CostEstimate",
    "TableScanEstimate",
    "CostEstimator",
    "EstimateInputs",
    "HeuristicCostEstimator",
    "SQLGuardError",
    "CatalogError",
    "PolicyError",
    "ValidationFailed",
    "__version__",
]
