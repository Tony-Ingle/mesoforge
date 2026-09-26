"""Integration test: run the Phase 0 Alembic migration up/down/up against
a real PostgreSQL instance (plan Section 4.10 verify command).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]


def _alembic_config(dsn: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    os.environ["MESOFORGE_ALEMBIC_DSN"] = dsn
    return config


def test_upgrade_downgrade_upgrade_on_empty_database(clean_postgres_dsn: str) -> None:
    config = _alembic_config(clean_postgres_dsn)

    command.upgrade(config, "head")
    engine = sa.create_engine(clean_postgres_dsn, future=True)
    inspector = sa.inspect(engine)
    tables_after_upgrade = set(inspector.get_table_names())
    expected_tables = {
        "configuration_snapshots",
        "grids",
        "stored_objects",
        "runs",
        "artifacts",
        "activities",
        "activity_inputs",
        "activity_outputs",
        "run_selected_inputs",
        "issued_forecasts",
        "governance_events",
    }
    assert expected_tables.issubset(tables_after_upgrade)

    command.downgrade(config, "base")
    inspector = sa.inspect(engine)
    tables_after_downgrade = set(inspector.get_table_names())
    assert not (expected_tables & tables_after_downgrade)

    command.upgrade(config, "head")
    inspector = sa.inspect(engine)
    tables_after_second_upgrade = set(inspector.get_table_names())
    assert expected_tables.issubset(tables_after_second_upgrade)

    engine.dispose()


def test_stored_objects_and_configuration_snapshots_have_no_payload_columns(
    clean_postgres_dsn: str,
) -> None:
    """Schema-inspection guard: Section 4.10 requires that scientific
    array bytes never live in PostgreSQL. No table may contain a
    bytea/large-binary payload column."""
    config = _alembic_config(clean_postgres_dsn)
    command.upgrade(config, "head")

    engine = sa.create_engine(clean_postgres_dsn, future=True)
    inspector = sa.inspect(engine)
    for table_name in inspector.get_table_names():
        for column in inspector.get_columns(table_name):
            column_type = str(column["type"]).upper()
            assert "BYTEA" not in column_type, (
                f"{table_name}.{column['name']} has a BYTEA payload column; "
                "scientific bytes must live in the object store, not PostgreSQL"
            )
    engine.dispose()


def test_runs_table_has_no_legacy_selected_inputs_json_column(
    clean_postgres_dsn: str,
) -> None:
    """Codex review t_f569c45c finding 5: migration
    0003_drop_legacy_selection_json must remove the unconstrained
    legacy runs.selected_inputs JSONB column -- run_selected_inputs is
    the only place selected-input references may live."""
    config = _alembic_config(clean_postgres_dsn)
    command.upgrade(config, "head")

    engine = sa.create_engine(clean_postgres_dsn, future=True)
    inspector = sa.inspect(engine)
    column_names = {column["name"] for column in inspector.get_columns("runs")}
    assert "selected_inputs" not in column_names
    engine.dispose()


def test_check_constraints_are_enforced(clean_postgres_dsn: str) -> None:
    config = _alembic_config(clean_postgres_dsn)
    command.upgrade(config, "head")

    engine = sa.create_engine(clean_postgres_dsn, future=True)
    with engine.begin() as connection, pytest.raises(sa.exc.IntegrityError):
        connection.execute(
            sa.text(
                "INSERT INTO stored_objects "
                "(content_digest, storage_uri, media_type, byte_size) "
                "VALUES ('sha256:x', 's3://bucket/key', 'application/x-netcdf', -1)"
            )
        )
    engine.dispose()
