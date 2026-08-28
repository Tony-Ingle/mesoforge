"""Integration tests: ArtifactService.create_run against real PostgreSQL
(plan Section 4.8; Codex review t_f569c45c finding 5).

Verifies ordered round-trip through the relational
``run_selected_inputs`` join table, FK rejection of a dangling
artifact reference, that a run with no selected inputs round-trips as
an empty tuple (never an unconstrained-JSON fallback -- the legacy
``runs.selected_inputs`` column no longer exists after migration
0003_drop_legacy_selection_json), and configuration/cutoff/source
checks enforced through ``ArtifactService.create_run`` itself.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
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

_TEST_CONFIGURATION_SNAPSHOT_ID = "cfg_sha256_" + "a" * 64
_TEST_CONFIGURATION_DIGEST = "sha256:" + "a" * 64
_CODE_REVISION = "a" * 40
_ENVIRONMENT_DIGEST = "sha256:" + "b" * 64


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
    bucket = f"mesoforge-run-creation-test-{uuid.uuid4().hex[:8]}"
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


class _FakeSnapshot:
    def __init__(self, snapshot_id: str, digest: str) -> None:
        self.configuration_snapshot_id = snapshot_id
        self.configuration_digest = digest
        self.canonical_json: dict[str, object] = {}
        self.source_references: tuple[dict[str, object], ...] = ()


@pytest.fixture()
def registered_configuration(migrated_dsn: str) -> str:
    with PostgresUnitOfWork(migrated_dsn) as uow:
        uow.configurations.add_if_absent(
            _FakeSnapshot(_TEST_CONFIGURATION_SNAPSHOT_ID, _TEST_CONFIGURATION_DIGEST)
        )
        uow.commit()
    return _TEST_CONFIGURATION_SNAPSHOT_ID


def _source_request(**overrides: object) -> SourceRegistrationRequest:
    kwargs: dict[str, object] = dict(
        source_authority="noaa.synthetic",
        source_locator="synthetic://run-creation/source",
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
        configuration_snapshot_id=_TEST_CONFIGURATION_SNAPSHOT_ID,
        configuration_digest=_TEST_CONFIGURATION_DIGEST,
        code_revision=_CODE_REVISION,
        environment_digest=_ENVIRONMENT_DIGEST,
    )
    kwargs.update(overrides)
    return SourceRegistrationRequest(**kwargs)


def _run_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = dict(
        run_id=f"run_{uuid.uuid4()}",
        forecast_issue_time=datetime(2026, 1, 1, tzinfo=UTC),
        information_cutoff=datetime(2026, 1, 1, 6, tzinfo=UTC),
        configuration_snapshot_id=_TEST_CONFIGURATION_SNAPSHOT_ID,
        configuration_digest=_TEST_CONFIGURATION_DIGEST,
        code_revision=_CODE_REVISION,
        environment_digest=_ENVIRONMENT_DIGEST,
        lockfile_digest="sha256:" + "d" * 64,
        random_seed=1,
        selected_input_artifact_ids=(),
    )
    kwargs.update(overrides)
    return kwargs


class TestCreateRunOrderedRoundTrip:
    def test_ordered_selection_round_trips_through_relational_table(
        self, service: ArtifactService, registered_configuration: str, migrated_dsn: str
    ) -> None:
        source_a = service.register_source(_source_request(source_locator="loc-a"), b"a-bytes")
        source_b = service.register_source(_source_request(source_locator="loc-b"), b"b-bytes")
        source_c = service.register_source(_source_request(source_locator="loc-c"), b"c-bytes")

        # deliberately non-alphabetical/non-registration order, to prove
        # the persisted ordinal -- not insertion or ID order -- drives
        # the round-tripped tuple order.
        created = service.create_run(
            **_run_kwargs(
                selected_input_artifact_ids=(
                    source_c.artifact_id,
                    source_a.artifact_id,
                    source_b.artifact_id,
                )
            )
        )
        assert created.selected_input_artifact_ids == (
            source_c.artifact_id,
            source_a.artifact_id,
            source_b.artifact_id,
        )

        with PostgresUnitOfWork(migrated_dsn) as uow:
            refetched = uow.runs.get(created.run_id)
        assert refetched.selected_input_artifact_ids == (
            source_c.artifact_id,
            source_a.artifact_id,
            source_b.artifact_id,
        )

        # the relational rows exist with the correct ordinals -- not a
        # JSON fallback (the legacy column no longer exists at all).
        engine = sa.create_engine(migrated_dsn, future=True)
        with engine.connect() as connection:
            rows = connection.execute(
                sa.text(
                    "SELECT ordinal, artifact_id FROM run_selected_inputs "
                    "WHERE run_id = :run_id ORDER BY ordinal"
                ),
                {"run_id": str(created.run_id).removeprefix("run_")},
            ).fetchall()
        engine.dispose()
        assert [str(r.artifact_id) for r in rows] == [
            str(source_c.artifact_id).removeprefix("art_"),
            str(source_a.artifact_id).removeprefix("art_"),
            str(source_b.artifact_id).removeprefix("art_"),
        ]

    def test_run_with_no_selected_inputs_round_trips_empty_tuple(
        self, service: ArtifactService, registered_configuration: str, migrated_dsn: str
    ) -> None:
        """A run legitimately selecting zero inputs must round-trip as
        an empty tuple -- not raise, and not fall back to any JSON."""
        created = service.create_run(**_run_kwargs(selected_input_artifact_ids=()))
        assert created.selected_input_artifact_ids == ()

        with PostgresUnitOfWork(migrated_dsn) as uow:
            refetched = uow.runs.get(created.run_id)
        assert refetched.selected_input_artifact_ids == ()


class TestCreateRunFailClosedAgainstRealPostgres:
    def test_missing_fk_artifact_rejected_and_creates_no_run(
        self, service: ArtifactService, registered_configuration: str, migrated_dsn: str
    ) -> None:
        from mesoforge.common.errors import NotFound

        dangling_artifact_id = f"art_{uuid.uuid4()}"
        run_id = f"run_{uuid.uuid4()}"
        with pytest.raises(NotFound):
            service.create_run(
                **_run_kwargs(run_id=run_id, selected_input_artifact_ids=(dangling_artifact_id,))
            )

        engine = sa.create_engine(migrated_dsn, future=True)
        with engine.connect() as connection:
            run_count = connection.execute(
                sa.text("SELECT COUNT(*) FROM runs WHERE id = :run_id"),
                {"run_id": run_id.removeprefix("run_")},
            ).scalar_one()
        engine.dispose()
        assert run_count == 0

    def test_direct_dangling_fk_insert_rejected_by_database(
        self, registered_configuration: str, migrated_dsn: str
    ) -> None:
        """Independent of the service layer, the database schema itself
        must reject a run_selected_inputs row referencing a nonexistent
        artifact -- proving the FK constraint, not just application-level
        validation, is what makes the relation fail closed."""
        run_row_id = uuid.uuid4()
        dangling_artifact_uuid = uuid.uuid4()
        engine = sa.create_engine(migrated_dsn, future=True)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO runs (id, schema_version, forecast_issue_time, "
                    "information_cutoff, configuration_snapshot_id, configuration_digest, "
                    "code_revision, environment_digest, lockfile_digest, random_seed) "
                    "VALUES (:id, 'run-manifest.v1', :ts, :ts, :cfg_id, :cfg_digest, "
                    ":code_rev, :env_digest, :lockfile_digest, 1)"
                ),
                {
                    "id": run_row_id,
                    "ts": datetime(2026, 1, 1, tzinfo=UTC),
                    "cfg_id": _TEST_CONFIGURATION_SNAPSHOT_ID,
                    "cfg_digest": _TEST_CONFIGURATION_DIGEST,
                    "code_rev": _CODE_REVISION,
                    "env_digest": _ENVIRONMENT_DIGEST,
                    "lockfile_digest": "sha256:" + "d" * 64,
                },
            )
        with (
            engine.begin() as connection,
            pytest.raises(sa.exc.IntegrityError),
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO run_selected_inputs (run_id, ordinal, artifact_id) "
                    "VALUES (:run_id, 0, :artifact_id)"
                ),
                {"run_id": run_row_id, "artifact_id": dangling_artifact_uuid},
            )
        engine.dispose()

    def test_missing_relational_rows_yields_empty_selection_not_error(
        self, registered_configuration: str, migrated_dsn: str
    ) -> None:
        """A run row that (for any reason) has no run_selected_inputs
        children must report an empty selection through the repository
        -- never an error, and never any JSON-derived value (there is no
        JSON column to read from any more)."""
        run_row_id = uuid.uuid4()
        engine = sa.create_engine(migrated_dsn, future=True)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO runs (id, schema_version, forecast_issue_time, "
                    "information_cutoff, configuration_snapshot_id, configuration_digest, "
                    "code_revision, environment_digest, lockfile_digest, random_seed) "
                    "VALUES (:id, 'run-manifest.v1', :ts, :ts, :cfg_id, :cfg_digest, "
                    ":code_rev, :env_digest, :lockfile_digest, 1)"
                ),
                {
                    "id": run_row_id,
                    "ts": datetime(2026, 1, 1, tzinfo=UTC),
                    "cfg_id": _TEST_CONFIGURATION_SNAPSHOT_ID,
                    "cfg_digest": _TEST_CONFIGURATION_DIGEST,
                    "code_rev": _CODE_REVISION,
                    "env_digest": _ENVIRONMENT_DIGEST,
                    "lockfile_digest": "sha256:" + "d" * 64,
                },
            )
        engine.dispose()

        with PostgresUnitOfWork(migrated_dsn) as uow:
            fetched = uow.runs.get(f"run_{run_row_id}")
        assert fetched.selected_input_artifact_ids == ()

    def test_tampered_configuration_digest_rejected_and_creates_no_run(
        self, service: ArtifactService, registered_configuration: str, migrated_dsn: str
    ) -> None:
        run_id = f"run_{uuid.uuid4()}"
        with pytest.raises(ValueError, match="does not match"):
            service.create_run(
                **_run_kwargs(run_id=run_id, configuration_digest="sha256:" + "f" * 64)
            )

        engine = sa.create_engine(migrated_dsn, future=True)
        with engine.connect() as connection:
            run_count = connection.execute(
                sa.text("SELECT COUNT(*) FROM runs WHERE id = :run_id"),
                {"run_id": run_id.removeprefix("run_")},
            ).scalar_one()
        engine.dispose()
        assert run_count == 0

    def test_cutoff_violation_rejected_and_creates_no_run(
        self, service: ArtifactService, registered_configuration: str, migrated_dsn: str
    ) -> None:
        far_future = datetime(2030, 1, 1, tzinfo=UTC)
        source = service.register_source(
            _source_request(
                availability=Availability(
                    available_at=far_future, authority="mesoforge.synthetic", method="synthetic.v1"
                )
            ),
            b"future-bytes",
        )
        run_id = f"run_{uuid.uuid4()}"
        with pytest.raises(ValueError, match="fails closed"):
            service.create_run(
                **_run_kwargs(
                    run_id=run_id,
                    information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
                    selected_input_artifact_ids=(source.artifact_id,),
                )
            )

        engine = sa.create_engine(migrated_dsn, future=True)
        with engine.connect() as connection:
            run_count = connection.execute(
                sa.text("SELECT COUNT(*) FROM runs WHERE id = :run_id"),
                {"run_id": run_id.removeprefix("run_")},
            ).scalar_one()
        engine.dispose()
        assert run_count == 0

    def test_derived_source_rejected_and_creates_no_run(
        self, service: ArtifactService, registered_configuration: str, migrated_dsn: str
    ) -> None:
        source = service.register_source(_source_request(), b"10.0")

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        transformation_request = TransformationRequest(
            activity_type="unit-conversion",
            activity_version="1.0.0",
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),),
            output_role="primary",
            output_artifact_type="synthetic-derived",
            output_artifact_schema_version="synthetic-derived.v1",
            output_media_type="application/octet-stream",
            configuration_snapshot_id=_TEST_CONFIGURATION_SNAPSHOT_ID,
            configuration_digest=_TEST_CONFIGURATION_DIGEST,
            code_revision=_CODE_REVISION,
            environment_digest=_ENVIRONMENT_DIGEST,
        )
        derived = service.execute_raw_transformation(
            transformation_request,
            transform=lambda data: data,
            serializer=_Serializer(),
            input_loader=lambda payload: payload,
            output_validator=lambda _output: None,
        )

        run_id = f"run_{uuid.uuid4()}"
        with pytest.raises(ValueError, match="not a source/root artifact"):
            service.create_run(
                **_run_kwargs(
                    run_id=run_id,
                    selected_input_artifact_ids=(derived.output.artifact_id,),
                )
            )

        engine = sa.create_engine(migrated_dsn, future=True)
        with engine.connect() as connection:
            run_count = connection.execute(
                sa.text("SELECT COUNT(*) FROM runs WHERE id = :run_id"),
                {"run_id": run_id.removeprefix("run_")},
            ).scalar_one()
        engine.dispose()
        assert run_count == 0
