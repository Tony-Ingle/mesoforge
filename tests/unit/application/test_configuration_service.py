"""Unit tests for application.configuration.ConfigurationService (Task 6,
plan Section 4.7, using in-memory repository test doubles).

RED: written before src/mesoforge/application/configuration.py and
storage/interfaces.py exist.

Real PostgreSQL integration coverage is deferred to Task 8/10, when the
PostgreSQL-backed GridRepository/ConfigurationRepository this service
depends on (storage.interfaces protocols, plan Section 4.9) actually
exist; see tests/integration/catalog/test_configuration_registration.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from mesoforge.application.configuration import ConfigurationService
from mesoforge.catalog.configuration import (
    load_configuration_source,
)
from mesoforge.common.errors import Conflict
from tests.fixtures.synthetic import build_synthetic_grid


@dataclass
class _FakeGridRepository:
    _by_id: dict[str, tuple[str, dict]] = field(default_factory=dict)

    def add_if_absent(self, grid_id: str, definition_digest: str, canonical_json: dict) -> None:
        existing = self._by_id.get(grid_id)
        if existing is not None:
            if existing[0] != definition_digest:
                raise Conflict(
                    f"grid_id {grid_id!r} already registered with a different definition"
                )
            return
        self._by_id[grid_id] = (definition_digest, canonical_json)


@dataclass
class _FakeConfigurationRepository:
    _by_digest: dict[str, object] = field(default_factory=dict)
    add_calls: int = 0

    def add_if_absent(self, snapshot) -> object:
        self.add_calls += 1
        existing = self._by_digest.get(snapshot.configuration_digest)
        if existing is not None:
            return existing
        self._by_digest[snapshot.configuration_digest] = snapshot
        return snapshot


@dataclass
class _FakeUnitOfWork:
    grids: _FakeGridRepository = field(default_factory=_FakeGridRepository)
    configurations: _FakeConfigurationRepository = field(
        default_factory=_FakeConfigurationRepository
    )
    committed: bool = False
    rolled_back: bool = False

    def __enter__(self) -> _FakeUnitOfWork:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.rollback()

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


REPO_ROOT_CONFIG = "configs/base.yaml"


def _load_config():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    config, _ = load_configuration_source(base_path=repo_root / REPO_ROOT_CONFIG)
    return config


class TestConfigurationServiceRegister:
    def test_registers_grids_then_snapshot_atomically(self) -> None:
        config = _load_config()
        uow = _FakeUnitOfWork()
        service = ConfigurationService(unit_of_work_factory=lambda: uow)

        service.register(config)

        assert uow.committed is True
        for grid in config.grids:
            assert grid.grid_id in uow.grids._by_id

    def test_idempotent_identical_registration(self) -> None:
        config = _load_config()
        uow = _FakeUnitOfWork()
        service = ConfigurationService(unit_of_work_factory=lambda: uow)

        service.register(config)
        service.register(config)

        assert uow.configurations.add_calls == 2
        assert len(uow.configurations._by_digest) == 1

    def test_grid_id_reuse_with_changed_definition_aborts(self) -> None:
        config = _load_config()
        uow = _FakeUnitOfWork()
        service = ConfigurationService(unit_of_work_factory=lambda: uow)
        service.register(config)

        changed_grid = build_synthetic_grid().model_copy(update={"y_coordinates": (1.0, 2.0)})
        conflicting_config = config.model_copy(update={"grids": (changed_grid,) + config.grids[1:]})

        with pytest.raises(Conflict):
            service.register(conflicting_config)
