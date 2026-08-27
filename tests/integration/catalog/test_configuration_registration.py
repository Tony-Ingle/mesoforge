"""Integration test for ConfigurationService against real PostgreSQL
(plan Section 4.7, Task 6 verify command).

Completes the placeholder introduced in the Task 6 commit now that
Task 8's PostgreSQL repositories (storage.postgres.repositories) exist.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from mesoforge.application.configuration import ConfigurationService
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.common.errors import Conflict
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def migrated_dsn(clean_postgres_dsn: str) -> str:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    os.environ["MESOFORGE_ALEMBIC_DSN"] = clean_postgres_dsn
    command.upgrade(config, "head")
    return clean_postgres_dsn


def _load_config():
    config, _ = load_configuration_source(base_path=REPO_ROOT / "configs" / "base.yaml")
    return config


def test_registers_grid_and_snapshot_atomically(migrated_dsn: str) -> None:
    configuration = _load_config()
    service = ConfigurationService(unit_of_work_factory=lambda: PostgresUnitOfWork(migrated_dsn))

    service.register(configuration)

    with PostgresUnitOfWork(migrated_dsn) as uow:
        for grid in configuration.grids:
            fetched = uow.grids.get(grid.grid_id)
            assert fetched.grid_id == grid.grid_id


def test_idempotent_identical_registration_returns_same_snapshot(migrated_dsn: str) -> None:
    configuration = _load_config()
    service = ConfigurationService(unit_of_work_factory=lambda: PostgresUnitOfWork(migrated_dsn))

    first = service.register(configuration)
    second = service.register(configuration)

    assert first.configuration_snapshot_id == second.configuration_snapshot_id


def test_grid_id_reuse_with_changed_definition_aborts(migrated_dsn: str) -> None:
    configuration = _load_config()
    service = ConfigurationService(unit_of_work_factory=lambda: PostgresUnitOfWork(migrated_dsn))
    service.register(configuration)

    changed_grid = configuration.grids[0].model_copy(update={"y_coordinates": (1.0, 2.0)})
    conflicting_configuration = configuration.model_copy(
        update={"grids": (changed_grid,) + configuration.grids[1:]}
    )

    with pytest.raises(Conflict):
        service.register(conflicting_configuration)

    with PostgresUnitOfWork(migrated_dsn) as uow:
        original = uow.grids.get(configuration.grids[0].grid_id)
        assert original.definition_digest == str(configuration.grids[0].definition_digest)
