"""Integration test: ArtifactService against real PostgreSQL and MinIO
(plan Section 4.9/5, Task 10 verify command).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from mesoforge.application.artifacts import (
    ArtifactService,
    SourceRegistrationRequest,
    TransformationInputRef,
    TransformationRequest,
)
from mesoforge.contracts.artifacts import Availability
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def migrated_dsn(clean_postgres_dsn: str) -> str:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    os.environ["MESOFORGE_ALEMBIC_DSN"] = clean_postgres_dsn
    command.upgrade(config, "head")
    return clean_postgres_dsn


@pytest.fixture()
def object_store() -> S3ArtifactObjectStore:
    import uuid

    endpoint = os.environ.get("MESOFORGE_TEST_S3_ENDPOINT", "http://127.0.0.1:19100")
    access_key = os.environ.get("MESOFORGE_TEST_S3_ACCESS_KEY", "mesoforge_test")
    secret_key = os.environ.get("MESOFORGE_TEST_S3_SECRET_KEY", "mesoforge_test_password")
    bucket = f"mesoforge-artifact-service-test-{uuid.uuid4().hex[:8]}"
    return S3ArtifactObjectStore(
        bucket=bucket, endpoint_url=endpoint, access_key=access_key, secret_key=secret_key
    )


@pytest.fixture()
def service(migrated_dsn: str, object_store: S3ArtifactObjectStore) -> ArtifactService:
    return ArtifactService(
        unit_of_work_factory=lambda: PostgresUnitOfWork(migrated_dsn),
        object_store=object_store,
        idempotency_lock=PostgresIdempotencyLock(migrated_dsn),
    )


_TEST_CONFIGURATION_SNAPSHOT_ID = "cfg_sha256_" + "a" * 64
_TEST_CONFIGURATION_DIGEST = "sha256:" + "a" * 64


@pytest.fixture()
def registered_configuration(migrated_dsn: str) -> str:
    """Insert the configuration_snapshots row the synthetic requests
    reference, satisfying the artifacts table's foreign key -- exactly
    as ConfigurationService.register() would in real use (Task 6/8)."""
    with PostgresUnitOfWork(migrated_dsn) as uow:
        uow.configurations.add_if_absent(
            _FakeSnapshot(_TEST_CONFIGURATION_SNAPSHOT_ID, _TEST_CONFIGURATION_DIGEST)
        )
        uow.commit()
    return _TEST_CONFIGURATION_SNAPSHOT_ID


class _FakeSnapshot:
    def __init__(self, snapshot_id: str, digest: str) -> None:
        self.configuration_snapshot_id = snapshot_id
        self.configuration_digest = digest
        self.canonical_json: dict[str, object] = {}
        self.source_references: tuple[dict[str, object], ...] = ()


def _source_request(**overrides: object) -> SourceRegistrationRequest:
    kwargs: dict[str, object] = dict(
        source_authority="noaa.synthetic",
        source_locator="synthetic://source/1",
        source_revision="v1",
        artifact_type="synthetic-source",
        artifact_schema_version="synthetic-source.v1",
        media_type="application/octet-stream",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        availability=Availability(
            available_at=datetime(2026, 1, 1, tzinfo=UTC),
            authority="mesoforge.synthetic",
            method="synthetic.v1",
        ),
        configuration_snapshot_id="cfg_sha256_" + "a" * 64,
        configuration_digest="sha256:" + "a" * 64,
        code_revision="a" * 40,
        environment_digest="sha256:" + "b" * 64,
    )
    kwargs.update(overrides)
    return SourceRegistrationRequest(**kwargs)


def test_register_source_against_real_postgres_and_minio(
    service: ArtifactService, registered_configuration: str
) -> None:
    request = _source_request()
    manifest = service.register_source(request, b"synthetic-real-bytes")

    assert manifest.source_identity is not None
    assert manifest.storage_uri.startswith("s3://")


def test_repeat_registration_is_idempotent_against_real_services(
    service: ArtifactService, registered_configuration: str
) -> None:
    request = _source_request()
    first = service.register_source(request, b"synthetic-real-bytes-2")
    second = service.register_source(request, b"synthetic-real-bytes-2")
    assert first.artifact_id == second.artifact_id


def test_full_register_transform_trace_round_trip(
    service: ArtifactService, registered_configuration: str
) -> None:
    source = service.register_source(_source_request(), b"10.0")

    class _Serializer:
        def serialize(self, value: bytes) -> bytes:
            return value

    request = TransformationRequest(
        activity_type="unit-conversion",
        activity_version="1.0.0",
        inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),),
        output_role="primary",
        output_artifact_type="synthetic-derived",
        output_artifact_schema_version="synthetic-derived.v1",
        output_media_type="application/octet-stream",
        configuration_snapshot_id="cfg_sha256_" + "a" * 64,
        configuration_digest="sha256:" + "a" * 64,
        code_revision="a" * 40,
        environment_digest="sha256:" + "b" * 64,
    )

    result = service.execute_raw_transformation(
        request,
        transform=lambda data: str(float(data) + 273.15).encode(),
        serializer=_Serializer(),
        input_loader=lambda payload: payload,
        output_validator=lambda _output: None,
    )

    assert result.activity.status == "succeeded"
    assert result.output.availability.available_at >= source.availability.available_at
