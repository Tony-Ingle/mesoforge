"""Integration test: run the Phase 0 Alembic migration up/down/up against
a real PostgreSQL instance (plan Section 4.10 verify command).
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from mesoforge.storage.postgres.repositories import PostgresIssuedForecastRepository

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


def test_issuance_horizon_upgrade_preserves_history_and_rejects_lossy_downgrade(
    clean_postgres_dsn: str,
) -> None:
    config = _alembic_config(clean_postgres_dsn)
    command.upgrade(config, "0005_policy_governance")
    engine = sa.create_engine(clean_postgres_dsn, future=True)
    identifier, batch = uuid4(), uuid4()
    digest = "sha256:" + "a" * 64
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO stored_objects (content_digest, storage_uri, media_type, byte_size) "
                "VALUES (:digest, 's3://fixture/unchanged', 'application/json', 100)"
            ),
            {"digest": digest},
        )
        connection.execute(
            sa.text(
                "INSERT INTO issued_forecasts (issued_forecast_id, schema_version, batch_run_id, "
                "location_index, latitude, longitude, issued_at, target_reference_time, "
                "content_digest) VALUES (:id, 'issued-forecast.v1', :batch, 0, 45, -93, "
                "'2026-10-08T12:00:00Z', '2026-10-08T12:00:00Z', :digest)"
            ),
            {"id": identifier, "batch": batch, "digest": digest},
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT forecast_horizon_hours, content_digest, forecast_payload_digest "
                "FROM issued_forecasts"
            )
        ).one()
        assert tuple(row) == (36, digest, None)
    # No UPDATE is needed to backfill the immutable historical issuance.
    with engine.begin() as connection, pytest.raises(sa.exc.DBAPIError, match="immutable"):
        connection.execute(sa.text("UPDATE issued_forecasts SET forecast_horizon_hours = 120"))
    insert = sa.text(
        "INSERT INTO issued_forecasts (issued_forecast_id, schema_version, batch_run_id, "
        "location_index, latitude, longitude, issued_at, target_reference_time, content_digest, "
        "forecast_horizon_hours, forecast_payload_digest) VALUES "
        "(:id, 'issued-forecast.v1', :batch, 0, 45, -93, "
        "'2026-10-08T12:00:00Z', '2026-10-08T12:00:00Z', :digest, :duration, :logical_digest)"
    )
    with engine.begin() as connection, pytest.raises(sa.exc.IntegrityError):
        connection.execute(
            insert,
            {
                "id": uuid4(),
                "batch": uuid4(),
                "digest": digest,
                "duration": 121,
                "logical_digest": None,
            },
        )
    long_identifier = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert,
            {
                "id": long_identifier,
                "batch": uuid4(),
                "digest": digest,
                "duration": 120,
                "logical_digest": "sha256:" + "b" * 64,
            },
        )
    with Session(engine) as session:
        repository = PostgresIssuedForecastRepository(session)
        assert repository.get(identifier).forecast_horizon_hours == 36
        assert repository.get(identifier).payload_digest == digest
        assert repository.get(long_identifier).forecast_horizon_hours == 120
        assert repository.get(long_identifier).content_digest == digest
        assert repository.get(long_identifier).payload_digest == "sha256:" + "b" * 64
    with pytest.raises(sa.exc.DBAPIError, match="Cannot remove horizon metadata"):
        command.downgrade(config, "0005_policy_governance")
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM issued_forecasts")) == 2
    engine.dispose()
