"""ConfigurationService: composes grid and configuration repositories in
one transactional unit of work (plan Section 4.7).

The concrete unit-of-work / repository protocols this depends on live in
``storage.interfaces`` (implemented in Task 8/9); this module depends
only on structural typing (Protocol), matching the plan's dependency
direction: application -> catalog, contracts, storage.interfaces, common.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from mesoforge.catalog.configuration import (
    MesoForgeConfiguration,
    compute_configuration_digest,
    compute_configuration_snapshot_id,
)


class _GridRepositoryLike(Protocol):
    def add_if_absent(
        self, grid_id: str, definition_digest: str, canonical_json: dict[str, Any]
    ) -> Any: ...


class _ConfigurationRepositoryLike(Protocol):
    def add_if_absent(self, snapshot: Any) -> Any: ...


class _UnitOfWorkLike(Protocol):
    grids: _GridRepositoryLike
    configurations: _ConfigurationRepositoryLike

    def __enter__(self) -> _UnitOfWorkLike: ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> object | None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class _ConfigurationSnapshotRecord:
    """Minimal snapshot value object passed to ConfigurationRepository.
    Matches the fields of contracts.provenance.ConfigurationSnapshot
    (Task 7) without importing it, to avoid a premature cross-task
    dependency; application/artifacts.py or Task 7 may replace this with
    the real contract type once it exists."""

    def __init__(
        self,
        *,
        configuration_snapshot_id: str,
        configuration_digest: str,
        canonical_json: dict[str, Any],
        created_at: datetime,
    ) -> None:
        self.configuration_snapshot_id = configuration_snapshot_id
        self.configuration_digest = configuration_digest
        self.canonical_json = canonical_json
        self.created_at = created_at


class ConfigurationService:
    """``register(snapshot)`` opens one unit of work, calls
    ``GridRepository.add_if_absent`` for every grid, then
    ``ConfigurationRepository.add_if_absent`` for the snapshot, and
    commits atomically. A reused grid_id with a different definition
    aborts the entire registration (the unit of work rolls back)."""

    def __init__(self, unit_of_work_factory: Callable[[], _UnitOfWorkLike]) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def register(self, configuration: MesoForgeConfiguration) -> _ConfigurationSnapshotRecord:
        digest = compute_configuration_digest(configuration)
        snapshot_id = compute_configuration_snapshot_id(configuration)
        canonical_json = configuration.model_dump(mode="json")

        with self._unit_of_work_factory() as unit_of_work:
            for grid in configuration.grids:
                unit_of_work.grids.add_if_absent(
                    grid.grid_id,
                    str(grid.definition_digest),
                    grid.model_dump(mode="json"),
                )

            record = _ConfigurationSnapshotRecord(
                configuration_snapshot_id=str(snapshot_id),
                configuration_digest=str(digest),
                canonical_json=canonical_json,
                created_at=datetime.now(UTC),
            )
            stored = unit_of_work.configurations.add_if_absent(record)
            unit_of_work.commit()
            return stored  # type: ignore[no-any-return]
