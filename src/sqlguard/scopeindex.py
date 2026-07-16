"""A shared, invalidatable scope index for the validation pipeline.

Scope traversal is the pipeline's dominant cost: v0.1 rebuilt the scope maps
in five separate stages. The key observation making one shared index safe:
after qualification, the *source topology* (alias -> table/CTE mapping per
scope) only changes when RLS wraps a table reference in a filtered subquery.
Predicate appends (WHERE/ON), LIMIT changes, and projection-item drops never
alter ``selected_sources``. So stages share one lazily-built index, and the
RLS injector invalidates it on the rare wrap.

Debugging aid: set ``SQLGUARD_PARANOID=1`` to disable caching entirely (every
access rebuilds from the live tree — behaviorally identical to v0.1). CI runs
the suite once in this mode to catch stale-index bugs.
"""

from __future__ import annotations

import os

from sqlglot import exp

from sqlguard.catalog import CatalogIndex
from sqlguard.semantics import ScopeInfo, build_scope_maps


class ScopeIndex:
    """Lazily-built scope maps over one tree, shared across pipeline stages."""

    def __init__(self, tree: exp.Expression, catalog_index: CatalogIndex) -> None:
        self._tree = tree
        self._catalog_index = catalog_index
        self._infos: list[ScopeInfo] | None = None
        self._by_expr: dict[int, ScopeInfo] | None = None
        self._paranoid = bool(os.environ.get("SQLGUARD_PARANOID"))

    @property
    def tree(self) -> exp.Expression:
        return self._tree

    @property
    def infos(self) -> list[ScopeInfo]:
        if self._infos is None or self._paranoid:
            self._infos, _, _ = build_scope_maps(self._tree, self._catalog_index)
            self._by_expr = {id(i.expression): i for i in self._infos}
        return self._infos

    @property
    def by_expression(self) -> dict[int, ScopeInfo]:
        _ = self.infos  # ensure built
        assert self._by_expr is not None
        return self._by_expr

    def invalidate(self) -> None:
        """Call after any mutation that changes source topology (subquery wraps)."""
        self._infos = None
        self._by_expr = None
