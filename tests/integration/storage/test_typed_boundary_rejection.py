"""Integration tests: concrete repository/object-store/lock adapters
reject malformed IDs/digests at the call boundary, against real
PostgreSQL and real MinIO (plan Section 4.9/4.10; final re-review HIGH
finding 6/t_1ecb8414: ``tests/unit/application/test_typed_boundaries.py``
only proved request-model/``create_run`` rejection -- this file proves
the concrete adapters themselves fail closed with ``InvalidIdentifier``
before a malformed value reaches SQL/S3, rather than becoming a query
miss or reaching the backend unchecked).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from mesoforge.common.errors import InvalidIdentifier
from mesoforge.contracts.provenance import ActivityError
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]

_VALID_DIGEST = "sha256:" + "a" * 64
_VALID_CONFIG_SNAPSHOT_ID = "cfg_sha256_" + "a" * 64


@pytest.fixture()
def migrated_dsn(clean_postgres_dsn: str) -> str:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    os.environ["MESOFORGE_ALEMBIC_DSN"] = clean_postgres_dsn
    command.upgrade(config, "head")
    return clean_postgres_dsn


@pytest.fixture()
def object_store() -> S3ArtifactObjectStore:
    endpoint = os.environ.get("MESOFORGE_TEST_S3_ENDPOINT", "http://127.0.0.1:19100")
    access_key = os.environ.get("MESOFORGE_TEST_S3_ACCESS_KEY", "mesoforge_test")
    secret_key = os.environ.get("MESOFORGE_TEST_S3_SECRET_KEY", "mesoforge_test_password")
    bucket = f"mesoforge-typed-boundary-{uuid.uuid4().hex[:8]}"
    return S3ArtifactObjectStore(
        bucket=bucket, endpoint_url=endpoint, access_key=access_key, secret_key=secret_key
    )


class TestPostgresGridRepositoryRejectsMalformedValues:
    def test_add_if_absent_rejects_malformed_grid_id(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.grids.add_if_absent("Not A Grid!", _VALID_DIGEST, {})

    def test_add_if_absent_rejects_malformed_digest(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.grids.add_if_absent("valid-grid.v1", "md5:bad", {})

    def test_get_rejects_malformed_grid_id(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.grids.get("Not A Grid!")


class TestPostgresConfigurationRepositoryRejectsMalformedValues:
    def test_get_rejects_malformed_snapshot_id(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.configurations.get("not-a-snapshot-id")


class TestPostgresStoredObjectRepositoryRejectsMalformedValues:
    def test_get_rejects_malformed_digest(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.stored_objects.get("not-a-digest")


class TestPostgresArtifactRepositoryRejectsMalformedValues:
    def test_get_rejects_malformed_artifact_id(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.artifacts.get("not-prefixed")

    def test_get_many_rejects_malformed_artifact_id(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.artifacts.get_many(("not-prefixed",))

    def test_find_by_source_registration_digest_rejects_malformed_digest(
        self, migrated_dsn: str
    ) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.artifacts.find_by_source_registration_digest("not-a-digest")

    def test_add_derived_rejects_malformed_artifact_id(self, migrated_dsn: str) -> None:
        completed_at = datetime(2026, 1, 1, tzinfo=UTC)
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.artifacts.add_derived(
                artifact_id="not-prefixed",
                artifact_type="x",
                artifact_schema_version="x.v1",
                content_digest=_VALID_DIGEST,
                created_at=completed_at,
                availability_authority="a",
                availability_method="m",
                parent_available_ats=(),
                activity_completed_at=completed_at,
                run_id=None,
                configuration_snapshot_id=_VALID_CONFIG_SNAPSHOT_ID,
                configuration_digest=_VALID_DIGEST,
                code_revision="a" * 40,
                environment_digest=_VALID_DIGEST,
                quality_state="valid",
                attributes=None,
            )

    def test_add_derived_rejects_malformed_content_digest(self, migrated_dsn: str) -> None:
        completed_at = datetime(2026, 1, 1, tzinfo=UTC)
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.artifacts.add_derived(
                artifact_id=f"art_{uuid.uuid4()}",
                artifact_type="x",
                artifact_schema_version="x.v1",
                content_digest="not-a-digest",
                created_at=completed_at,
                availability_authority="a",
                availability_method="m",
                parent_available_ats=(),
                activity_completed_at=completed_at,
                run_id=None,
                configuration_snapshot_id=_VALID_CONFIG_SNAPSHOT_ID,
                configuration_digest=_VALID_DIGEST,
                code_revision="a" * 40,
                environment_digest=_VALID_DIGEST,
                quality_state="valid",
                attributes=None,
            )


class TestPostgresActivityRepositoryRejectsMalformedValues:
    def test_finish_succeeded_rejects_malformed_activity_id(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.activities.finish_succeeded("not-prefixed", (), datetime(2026, 1, 1, tzinfo=UTC))

    def test_finish_failed_rejects_malformed_activity_id(self, migrated_dsn: str) -> None:
        error = ActivityError(error_type="X", message_digest=_VALID_DIGEST, retryable=True)
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.activities.finish_failed("not-prefixed", error, datetime(2026, 1, 1, tzinfo=UTC))

    def test_find_succeeded_by_idempotency_rejects_malformed_digest(
        self, migrated_dsn: str
    ) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.activities.find_succeeded_by_idempotency("not-a-digest")

    def test_producer_of_rejects_malformed_artifact_id(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.activities.producer_of("not-prefixed")

    def test_consumers_of_rejects_malformed_artifact_id(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.activities.consumers_of("not-prefixed")


class TestPostgresRunRepositoryRejectsMalformedValues:
    def test_get_rejects_malformed_run_id(self, migrated_dsn: str) -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow, pytest.raises(InvalidIdentifier):
            uow.runs.get("not-prefixed")


class TestPostgresIdempotencyLockRejectsMalformedValues:
    def test_acquire_rejects_malformed_digest(self, migrated_dsn: str) -> None:
        lock = PostgresIdempotencyLock(migrated_dsn)
        with pytest.raises(InvalidIdentifier):
            with lock.acquire("not-a-digest"):
                pass  # pragma: no cover - never reached


class TestS3ArtifactObjectStoreRejectsMalformedValues:
    def test_put_if_absent_rejects_malformed_digest(
        self, object_store: S3ArtifactObjectStore
    ) -> None:
        with pytest.raises(InvalidIdentifier):
            object_store.put_if_absent("not-a-digest", b"data", "application/octet-stream")

    def test_get_verified_rejects_malformed_digest(
        self, object_store: S3ArtifactObjectStore
    ) -> None:
        with pytest.raises(InvalidIdentifier):
            object_store.get_verified("s3://bucket/key", "not-a-digest")
