"""Integration tests: PostgreSQL-backed repositories against a real
PostgreSQL instance (plan Section 4.9/4.10, Task 8 verify command).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from mesoforge.common.errors import Conflict, NotFound
from mesoforge.contracts.artifacts import ArtifactManifest, Availability
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def migrated_dsn(clean_postgres_dsn: str) -> str:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    import os

    os.environ["MESOFORGE_ALEMBIC_DSN"] = clean_postgres_dsn
    command.upgrade(config, "head")
    return clean_postgres_dsn


def _artifact_manifest(**overrides: object) -> ArtifactManifest:
    kwargs: dict[str, object] = dict(
        artifact_id=f"art_{uuid.uuid4()}",
        artifact_type="synthetic-dataset",
        artifact_schema_version="canonical-guidance.v1",
        media_type="application/x-netcdf",
        byte_size=1024,
        content_digest="sha256:" + "a" * 64,
        storage_uri="s3://bucket/objects/sha256/aa/" + "a" * 62,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        registered_at=datetime(2026, 1, 1, tzinfo=UTC),
        availability=Availability(
            available_at=datetime(2026, 1, 1, tzinfo=UTC),
            authority="mesoforge.synthetic",
            method="synthetic.v1",
        ),
        configuration_snapshot_id="cfg_sha256_" + "a" * 64,
        configuration_digest="sha256:" + "a" * 64,
        code_revision="a" * 40,
        environment_digest="sha256:" + "b" * 64,
        quality_state="valid",
    )
    kwargs.update(overrides)
    return ArtifactManifest(**kwargs)


class TestGridRepository:
    def test_add_if_absent_then_get(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow:
            uow.grids.add_if_absent("g.v1", "sha256:" + "a" * 64, {"grid_id": "g.v1"})
            uow.commit()

        with PostgresUnitOfWork(migrated_dsn) as uow:
            fetched = uow.grids.get("g.v1")
            assert fetched.grid_id == "g.v1"

    def test_idempotent_identical_registration(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow:
            uow.grids.add_if_absent("g.v1", "sha256:" + "a" * 64, {"grid_id": "g.v1"})
            uow.grids.add_if_absent("g.v1", "sha256:" + "a" * 64, {"grid_id": "g.v1"})
            uow.commit()

    def test_changed_definition_raises_conflict(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow:
            uow.grids.add_if_absent("g.v1", "sha256:" + "a" * 64, {"grid_id": "g.v1"})
            uow.commit()

        with PostgresUnitOfWork(migrated_dsn) as uow:
            with pytest.raises(Conflict):
                uow.grids.add_if_absent(
                    "g.v1", "sha256:" + "b" * 64, {"grid_id": "g.v1", "changed": True}
                )
            uow.rollback()

        with PostgresUnitOfWork(migrated_dsn) as uow:
            fetched = uow.grids.get("g.v1")
            assert fetched.definition_digest == "sha256:" + "a" * 64

    def test_get_missing_raises_not_found(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(NotFound):
            uow.grids.get("does-not-exist")


class TestArtifactRepository:
    def test_add_requires_existing_stored_object(self, migrated_dsn: str) -> None:
        manifest = _artifact_manifest()
        with PostgresUnitOfWork(migrated_dsn) as uow:
            uow.configurations.add_if_absent(
                _FakeSnapshot(manifest.configuration_snapshot_id, manifest.configuration_digest)
            )
            with pytest.raises(sa.exc.IntegrityError):
                uow.artifacts.add(manifest)
            uow.rollback()

    def test_add_and_get_round_trip(self, migrated_dsn: str) -> None:
        manifest = _artifact_manifest()
        with PostgresUnitOfWork(migrated_dsn) as uow:
            uow.configurations.add_if_absent(
                _FakeSnapshot(manifest.configuration_snapshot_id, manifest.configuration_digest)
            )
            uow.stored_objects.add_if_absent(
                _FakeStoredObject(
                    manifest.content_digest,
                    manifest.storage_uri,
                    manifest.media_type,
                    manifest.byte_size,
                )
            )
            uow.artifacts.add(manifest)
            uow.commit()

        with PostgresUnitOfWork(migrated_dsn) as uow:
            fetched = uow.artifacts.get(manifest.artifact_id)
            assert fetched.artifact_id == manifest.artifact_id
            assert fetched.content_digest == manifest.content_digest
            assert fetched.storage_uri == manifest.storage_uri


class TestArtifactRepositoryAddDerivedTransactionTimestamp:
    """Finding 3 test gap (Codex review t_f569c45c): the required
    committed DB-backed integration assertion that add_derived's
    registered_at equals the exact PostgreSQL transaction_timestamp()
    for the insert's own transaction, and available_at >= registered_at
    with the max-parent-availability rule honored (Codex review
    t_9bb13e2b finding 3)."""

    def test_registered_at_equals_transaction_timestamp_and_available_at_honors_max_parent(
        self, migrated_dsn: str
    ) -> None:
        parent_manifest = _artifact_manifest()
        far_future_parent_available_at = datetime(2030, 6, 15, 12, tzinfo=UTC)
        parent_manifest = parent_manifest.model_copy(
            update={
                "availability": Availability(
                    available_at=far_future_parent_available_at,
                    authority="mesoforge.synthetic",
                    method="synthetic.v1",
                )
            }
        )

        with PostgresUnitOfWork(migrated_dsn) as uow:
            uow.configurations.add_if_absent(
                _FakeSnapshot(
                    parent_manifest.configuration_snapshot_id,
                    parent_manifest.configuration_digest,
                )
            )
            uow.stored_objects.add_if_absent(
                _FakeStoredObject(
                    parent_manifest.content_digest,
                    parent_manifest.storage_uri,
                    parent_manifest.media_type,
                    parent_manifest.byte_size,
                )
            )
            uow.artifacts.add(parent_manifest)
            uow.commit()

        derived_content_digest = "sha256:" + "e" * 64
        derived_storage_uri = "s3://bucket/objects/sha256/ee/" + "e" * 62
        activity_completed_at = datetime(2026, 1, 1, tzinfo=UTC)  # deliberately in the past

        with PostgresUnitOfWork(migrated_dsn) as uow:
            uow.stored_objects.add_if_absent(
                _FakeStoredObject(
                    derived_content_digest,
                    derived_storage_uri,
                    "application/x-netcdf",
                    2048,
                )
            )
            created = uow.artifacts.add_derived(
                artifact_id=f"art_{uuid.uuid4()}",
                artifact_type="canonical-guidance",
                artifact_schema_version="canonical-guidance.v1",
                content_digest=derived_content_digest,
                created_at=activity_completed_at,
                availability_authority="mesoforge.derived",
                availability_method="unit-conversion.1.0.0",
                parent_available_ats=(far_future_parent_available_at,),
                activity_completed_at=activity_completed_at,
                run_id=None,
                configuration_snapshot_id=parent_manifest.configuration_snapshot_id,
                configuration_digest=parent_manifest.configuration_digest,
                code_revision=parent_manifest.code_revision,
                environment_digest=parent_manifest.environment_digest,
                quality_state="valid",
                attributes=None,
            )

            # Required committed integration assertion: registered_at
            # equals the EXACT transaction_timestamp() PostgreSQL used
            # for this insert's own transaction, queried inside the
            # same still-open transaction/session so it observes the
            # identical value the insert itself computed.
            observed_transaction_timestamp = uow._session.execute(  # type: ignore[attr-defined]
                sa.text("SELECT transaction_timestamp()")
            ).scalar_one()
            uow.commit()

        assert created.registered_at == observed_transaction_timestamp
        # available_at >= registered_at always holds by construction.
        assert created.availability.available_at >= created.registered_at
        # the max-parent rule is honored: the far-future parent
        # available_at strictly dominates both activity_completed_at
        # (in the past) and transaction_timestamp() (now), so
        # available_at must equal it exactly.
        assert created.availability.available_at == far_future_parent_available_at

        with PostgresUnitOfWork(migrated_dsn) as uow:
            refetched = uow.artifacts.get(created.artifact_id)
        assert refetched.registered_at == created.registered_at
        assert refetched.availability.available_at == far_future_parent_available_at


class _FakeSnapshot:
    def __init__(self, snapshot_id: str, digest: str) -> None:
        self.configuration_snapshot_id = snapshot_id
        self.configuration_digest = digest
        self.canonical_json: dict[str, object] = {}
        self.source_references: tuple[dict[str, object], ...] = ()


class _FakeStoredObject:
    def __init__(
        self, content_digest: str, storage_uri: str, media_type: str, byte_size: int
    ) -> None:
        self.content_digest = content_digest
        self.storage_uri = storage_uri
        self.media_type = media_type
        self.byte_size = byte_size
