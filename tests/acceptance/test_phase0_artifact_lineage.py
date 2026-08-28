"""Phase 0 exit-criterion acceptance proof (plan Section 4.11 / Task 11).

Against real PostgreSQL and MinIO, this test:
 1. resolves and registers the immutable configuration snapshot;
 2. registers a synthetic source artifact with exact checksum,
    authoritative availability, code/environment/config identity;
 3. executes the pure degC->K transformation (source contract validated
    via ``input_validator`` before the transform runs);
 4. registers the canonical output and successful activity atomically;
 5. retrieves and checksum-verifies the output;
 6. asserts exact coordinates, Kelvin values, dtype, units, grid,
    quality mask, and valid-time invariant;
 7. traces backward to source and forward to output, recovering
    input/output roles, activity version, config snapshot, digests,
    run, availability, and storage URIs;
 8. exports deterministic lineage JSON, reconstructs the graph from
    PERSISTED REPOSITORY STATE ONLY (producer_of()'s persisted
    input/output edges, not manually built in-memory edges), and
    compares;
 9. repeats the identical request and asserts exactly one succeeded
    activity row and exactly one output edge exist by counting all
    persisted rows for the idempotency digest/output artifact;
10. changes ONLY the transformation's configuration identity while
    retaining the exact same selected source input artifact (isolating
    configuration identity as the sole idempotency-key variable) and
    asserts a new snapshot, idempotency key, activity, and output
    record while the original activity/output remain unchanged;
11. requests an input with available_at > information_cutoff and
    asserts run creation fails closed;
12. attempts retrieval with a wrong digest and asserts IntegrityError.

A fake/in-memory-only execution does not satisfy this test -- it is the
Phase 0 exit criterion and runs only under ``-m integration`` against
real PostgreSQL (via pgserver, see tests/integration/conftest.py) and
real MinIO.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
import sqlalchemy as sa
import xarray as xr
from alembic import command
from alembic.config import Config

from mesoforge.application.artifacts import (
    ArtifactService,
    SourceRegistrationRequest,
    TransformationInputRef,
    TransformationRequest,
)
from mesoforge.application.configuration import ConfigurationService
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.catalog.units import convert
from mesoforge.common.errors import IntegrityError
from mesoforge.contracts.artifacts import Availability
from mesoforge.contracts.datasets import validate_canonical_dataset
from mesoforge.provenance.lineage import ActivityEdge, build_lineage_graph
from mesoforge.storage.netcdf import H5NetcdfDatasetSerializer
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from tests.fixtures.synthetic import (
    build_synthetic_dataset,
    build_synthetic_grid,
    build_synthetic_variable_definition,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    bucket = f"mesoforge-acceptance-{uuid.uuid4().hex[:8]}"
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


def _degc_to_kelvin_transform(dataset: xr.Dataset) -> xr.Dataset:
    """Pure test transformation: convert air_temperature_2m from degC to
    K and stamp canonical attributes. Source-contract validation happens
    separately via ``input_validator`` (finding 1, Codex review
    t_9bb13e2b) before this transform ever runs."""
    converted = dataset.copy(deep=True)
    values_k = convert(dataset["air_temperature_2m"].values.astype("float64"), "degC", "K")
    converted["air_temperature_2m"].values[...] = values_k.astype("float32")
    converted["air_temperature_2m"].attrs["unit_id"] = "K"
    return converted


class TestPhase0ArtifactLineageAcceptance:
    def test_full_register_transform_trace_lineage(
        self,
        service: ArtifactService,
        migrated_dsn: str,
        object_store: S3ArtifactObjectStore,
    ) -> None:
        # ---------------------------------------------------------
        # Step 1: resolve and register the immutable configuration snapshot
        # ---------------------------------------------------------
        configuration, _ = load_configuration_source(base_path=REPO_ROOT / "configs" / "base.yaml")
        config_service = ConfigurationService(
            unit_of_work_factory=lambda: PostgresUnitOfWork(migrated_dsn)
        )
        snapshot = config_service.register(configuration)

        code_revision = "a" * 40
        environment_digest = "sha256:" + "b" * 64

        # ---------------------------------------------------------
        # Step 2: register a synthetic source artifact
        # ---------------------------------------------------------
        source_dataset = build_synthetic_dataset(
            configuration_snapshot_id=snapshot.configuration_snapshot_id
        )
        serializer = H5NetcdfDatasetSerializer()
        source_bytes = serializer.serialize(source_dataset)

        source_availability = Availability(
            available_at=datetime(2026, 1, 1, tzinfo=UTC),
            authority="mesoforge.synthetic",
            method="synthetic-ingest.v1",
        )
        source_request = SourceRegistrationRequest(
            source_authority="mesoforge.synthetic",
            source_locator="synthetic://phase0-acceptance/source",
            source_revision="v1",
            artifact_type="canonical-guidance-source",
            artifact_schema_version="canonical-guidance.v1",
            media_type="application/x-netcdf",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            availability=source_availability,
            configuration_snapshot_id=snapshot.configuration_snapshot_id,
            configuration_digest=snapshot.configuration_digest,
            code_revision=code_revision,
            environment_digest=environment_digest,
        )
        source_artifact = service.register_source(source_request, source_bytes)
        assert source_artifact.source_identity is not None
        assert source_artifact.source_identity.authority == "mesoforge.synthetic"
        assert source_artifact.source_identity.locator == "synthetic://phase0-acceptance/source"
        assert source_artifact.source_identity.revision == "v1"

        # ---------------------------------------------------------
        # Step 2b: create a valid run selecting the registered source
        # input, and attach the transformation activity/output to it
        # (Codex review t_f569c45c finding 6: the acceptance proof must
        # actually create/attach a run, not omit run_id entirely).
        # ---------------------------------------------------------
        lockfile_digest = "sha256:" + "c" * 64
        created_run = service.create_run(
            run_id=f"run_{uuid.uuid4()}",
            forecast_issue_time=datetime(2026, 1, 1, tzinfo=UTC),
            information_cutoff=datetime(2026, 1, 1, 1, tzinfo=UTC),
            configuration_snapshot_id=snapshot.configuration_snapshot_id,
            configuration_digest=snapshot.configuration_digest,
            code_revision=code_revision,
            environment_digest=environment_digest,
            lockfile_digest=lockfile_digest,
            random_seed=42,
            selected_input_artifact_ids=(source_artifact.artifact_id,),
        )
        assert created_run.selected_input_artifact_ids == (source_artifact.artifact_id,)

        # ---------------------------------------------------------
        # Steps 3-4: execute the pure degC->K transformation; register
        # the canonical output and successful activity atomically
        # ---------------------------------------------------------
        grid = build_synthetic_grid()
        source_variable = build_synthetic_variable_definition(canonical_unit_id="degC")
        variable = build_synthetic_variable_definition(canonical_unit_id="K")

        def _validate_input(dataset: xr.Dataset) -> None:
            validate_canonical_dataset(
                dataset,
                grids={grid.grid_id: grid},
                variables={source_variable.variable_id: source_variable},
            )

        def _validate_output(dataset: xr.Dataset) -> None:
            validate_canonical_dataset(
                dataset,
                grids={grid.grid_id: grid},
                variables={variable.variable_id: variable},
            )

        transformation_request = TransformationRequest(
            activity_type="unit-conversion.degc-to-kelvin",
            activity_version="1.0.0",
            inputs=(
                TransformationInputRef(role="primary", artifact_id=source_artifact.artifact_id),
            ),
            output_role="primary",
            output_artifact_type="canonical-guidance",
            output_artifact_schema_version="canonical-guidance.v1",
            output_media_type="application/x-netcdf",
            configuration_snapshot_id=snapshot.configuration_snapshot_id,
            configuration_digest=snapshot.configuration_digest,
            code_revision=code_revision,
            environment_digest=environment_digest,
            run_id=created_run.run_id,
        )

        result = service.execute_transformation(
            transformation_request,
            transform=_degc_to_kelvin_transform,
            serializer=serializer,
            input_loader=serializer.deserialize,
            output_validator=_validate_output,
            input_validator=_validate_input,
        )
        assert result.activity.status == "succeeded"
        output_artifact = result.output

        # ---------------------------------------------------------
        # Step 5: retrieve and checksum-verify the output
        # ---------------------------------------------------------
        retrieved_bytes = object_store.get_verified(
            output_artifact.storage_uri, output_artifact.content_digest
        )
        retrieved_dataset = serializer.deserialize(retrieved_bytes)

        # ---------------------------------------------------------
        # Step 6: exact coordinates, Kelvin values, dtype, units, grid,
        # quality mask, valid-time invariant
        # ---------------------------------------------------------
        np.testing.assert_array_equal(retrieved_dataset["x"].values, np.asarray(grid.x_coordinates))
        np.testing.assert_array_equal(retrieved_dataset["y"].values, np.asarray(grid.y_coordinates))
        original_celsius = source_dataset["air_temperature_2m"].values.astype("float64")
        expected_kelvin = original_celsius + 273.15
        np.testing.assert_allclose(
            retrieved_dataset["air_temperature_2m"].values.astype("float64"),
            expected_kelvin,
            rtol=1e-5,
        )
        assert retrieved_dataset["air_temperature_2m"].dtype == np.dtype("float32")
        assert retrieved_dataset["air_temperature_2m"].attrs["unit_id"] == "K"
        assert retrieved_dataset.attrs["grid_id"] == grid.grid_id
        np.testing.assert_array_equal(
            retrieved_dataset["air_temperature_2m_quality_mask"].values,
            source_dataset["air_temperature_2m_quality_mask"].values,
        )
        expected_valid_times = (
            retrieved_dataset["forecast_reference_time"].values
            + retrieved_dataset["lead_time"].values
        )
        np.testing.assert_array_equal(retrieved_dataset["valid_time"].values, expected_valid_times)

        # ---------------------------------------------------------
        # Step 7: trace backward to source and forward to output;
        # recover run identity and assert full persisted state (Codex
        # review t_f569c45c finding 6: this step must actually assert
        # persisted run, config snapshot+digest, authoritative
        # availability, roles, versions, code/environment, and storage
        # URIs -- not merely activity/artifact edges).
        # ---------------------------------------------------------
        with PostgresUnitOfWork(migrated_dsn) as uow:
            producer = uow.activities.producer_of(output_artifact.artifact_id)
            assert producer is not None
            assert producer.activity_id == result.activity.activity_id
            assert producer.activity_version == "1.0.0"
            assert producer.configuration_snapshot_id == snapshot.configuration_snapshot_id
            assert producer.code_revision == code_revision
            assert producer.environment_digest == environment_digest
            assert producer.inputs[0].artifact_id == source_artifact.artifact_id
            assert producer.inputs[0].role == "primary"
            assert producer.outputs[0].artifact_id == output_artifact.artifact_id
            assert producer.outputs[0].role == "primary"
            # the activity is linked to the run created in step 2b
            assert producer.run_id == created_run.run_id

            consumers = uow.activities.consumers_of(source_artifact.artifact_id)
            assert any(c.activity_id == result.activity.activity_id for c in consumers)

            refetched_source = uow.artifacts.get(source_artifact.artifact_id)
            refetched_output = uow.artifacts.get(output_artifact.artifact_id)
            assert refetched_source.storage_uri == source_artifact.storage_uri
            assert refetched_output.storage_uri == output_artifact.storage_uri

            # ---- recover run identity and assert full persisted state ----
            refetched_run = uow.runs.get(created_run.run_id)
            assert refetched_run.run_id == created_run.run_id
            assert refetched_run.selected_input_artifact_ids == (source_artifact.artifact_id,)
            assert refetched_run.configuration_snapshot_id == snapshot.configuration_snapshot_id
            assert refetched_run.configuration_digest == snapshot.configuration_digest
            assert refetched_run.code_revision == code_revision
            assert refetched_run.environment_digest == environment_digest
            assert refetched_run.lockfile_digest == lockfile_digest
            assert refetched_run.random_seed == 42

            # ---- authoritative availability, roles, versions,
            # code/environment, and storage URIs from persisted
            # records (source and derived) ----
            assert refetched_source.availability.available_at == source_availability.available_at
            assert refetched_source.availability.authority == source_availability.authority
            assert refetched_source.availability.method == source_availability.method
            assert refetched_source.configuration_snapshot_id == snapshot.configuration_snapshot_id
            assert refetched_source.configuration_digest == snapshot.configuration_digest
            assert refetched_source.code_revision == code_revision
            assert refetched_source.environment_digest == environment_digest
            assert refetched_source.storage_uri.startswith("s3://")

            assert refetched_output.availability.available_at >= source_availability.available_at
            assert refetched_output.configuration_snapshot_id == snapshot.configuration_snapshot_id
            assert refetched_output.configuration_digest == snapshot.configuration_digest
            assert refetched_output.code_revision == code_revision
            assert refetched_output.environment_digest == environment_digest
            assert refetched_output.storage_uri.startswith("s3://")
            assert refetched_output.source_registration_digest is None
            # the output artifact was produced as part of the created
            # run (transformation_request.run_id); the source artifact
            # itself was registered independently and only later
            # selected into the run via selected_input_artifact_ids.
            assert refetched_output.run_id == created_run.run_id
            assert refetched_source.run_id is None

        # ---------------------------------------------------------
        # Step 8: export deterministic lineage JSON, reconstruct the
        # graph from PERSISTED REPOSITORY STATE ONLY (not manually
        # built edges from in-memory values -- Codex review t_9bb13e2b
        # finding 7) and compare
        # ---------------------------------------------------------
        with PostgresUnitOfWork(migrated_dsn) as uow:
            persisted_producer = uow.activities.producer_of(output_artifact.artifact_id)
        assert persisted_producer is not None
        persisted_edges = tuple(
            ActivityEdge(
                activity_id=persisted_producer.activity_id,
                artifact_id=ref.artifact_id,
                role=ref.role,
                direction="input",
            )
            for ref in persisted_producer.inputs
        ) + tuple(
            ActivityEdge(
                activity_id=persisted_producer.activity_id,
                artifact_id=ref.artifact_id,
                role=ref.role,
                direction="output",
            )
            for ref in persisted_producer.outputs
        )
        graph_a = build_lineage_graph(
            root_artifact_id=output_artifact.artifact_id, edges=persisted_edges
        )
        graph_b = build_lineage_graph(
            root_artifact_id=output_artifact.artifact_id, edges=tuple(reversed(persisted_edges))
        )
        assert graph_a.model_dump_json() == graph_b.model_dump_json()
        assert source_artifact.artifact_id in graph_a.artifact_nodes
        assert output_artifact.artifact_id in graph_a.artifact_nodes
        assert result.activity.activity_id in graph_a.activity_nodes

        # ---------------------------------------------------------
        # Step 9: repeat the identical request; assert exactly one
        # succeeded activity AND exactly one output edge exist by
        # counting ALL persisted rows (not merely that a succeeded
        # match can be found -- Codex review t_9bb13e2b finding 7)
        # ---------------------------------------------------------
        repeat_result = service.execute_transformation(
            transformation_request,
            transform=_degc_to_kelvin_transform,
            serializer=serializer,
            input_loader=serializer.deserialize,
            output_validator=_validate_output,
            input_validator=_validate_input,
        )
        assert repeat_result.output.artifact_id == output_artifact.artifact_id
        with PostgresUnitOfWork(migrated_dsn) as uow:
            succeeded = uow.activities.find_succeeded_by_idempotency(
                result.activity.idempotency_digest
            )
            assert succeeded is not None
            assert succeeded.activity_id == result.activity.activity_id

            engine = sa.create_engine(migrated_dsn, future=True)
            with engine.connect() as connection:
                activity_count = connection.execute(
                    sa.text("SELECT COUNT(*) FROM activities WHERE idempotency_digest = :d"),
                    {"d": result.activity.idempotency_digest},
                ).scalar_one()
                output_edge_count = connection.execute(
                    sa.text("SELECT COUNT(*) FROM activity_outputs WHERE artifact_id = :a"),
                    {"a": str(output_artifact.artifact_id).removeprefix("art_")},
                ).scalar_one()
            engine.dispose()
        assert activity_count == 1, (
            "repeat request must not create a second (e.g. lingering 'started') "
            "activity row for the same idempotency digest"
        )
        assert output_edge_count == 1, (
            "repeat request must not create a second output edge for the same artifact"
        )

        # ---------------------------------------------------------
        # Step 10: change ONLY the transformation's configuration
        # identity while retaining the EXACT SAME selected source
        # input artifact -- isolating configuration identity as the
        # sole idempotency-key variable (Codex review t_9bb13e2b
        # finding 7: the prior version also changed the source
        # artifact/bytes, which did not isolate configuration
        # identity). Assert a new snapshot, idempotency key, activity,
        # and output record while the original activity/output remain
        # byte-for-byte unchanged.
        #
        # Per plan Section 4.4, a changed grid definition requires a new
        # grid_id (an existing grid_id with different coordinates is a
        # rejected Conflict, exercised separately in Task 6/8's tests);
        # so the "one semantic value" change is a *new*, additional grid
        # definition (a distinct grid_id) added to the configuration --
        # still a real, digest-changing configuration edit.
        # ---------------------------------------------------------
        extra_grid = configuration.grids[0].model_copy(
            update={"grid_id": "synthetic-grid.v2", "y_coordinates": (40.0, 41.0)}
        )
        changed_configuration = configuration.model_copy(
            update={"grids": configuration.grids + (extra_grid,)}
        )
        changed_snapshot = config_service.register(changed_configuration)
        assert changed_snapshot.configuration_snapshot_id != snapshot.configuration_snapshot_id

        changed_transformation_request = TransformationRequest(
            activity_type="unit-conversion.degc-to-kelvin",
            activity_version="1.0.0",
            inputs=(
                # exact same selected input as the original request --
                # only the transformation's own configuration identity
                # changes below.
                TransformationInputRef(role="primary", artifact_id=source_artifact.artifact_id),
            ),
            output_role="primary",
            output_artifact_type="canonical-guidance",
            output_artifact_schema_version="canonical-guidance.v1",
            output_media_type="application/x-netcdf",
            configuration_snapshot_id=changed_snapshot.configuration_snapshot_id,
            configuration_digest=changed_snapshot.configuration_digest,
            code_revision=code_revision,
            environment_digest=environment_digest,
        )
        changed_result = service.execute_transformation(
            changed_transformation_request,
            transform=_degc_to_kelvin_transform,
            serializer=serializer,
            input_loader=serializer.deserialize,
            output_validator=_validate_output,
            input_validator=_validate_input,
        )
        assert changed_result.activity.idempotency_digest != result.activity.idempotency_digest
        assert changed_result.activity.activity_id != result.activity.activity_id
        assert changed_result.output.artifact_id != output_artifact.artifact_id

        # the original output/activity remain unchanged
        with PostgresUnitOfWork(migrated_dsn) as uow:
            original_still_there = uow.artifacts.get(output_artifact.artifact_id)
            assert original_still_there.content_digest == output_artifact.content_digest

        # ---------------------------------------------------------
        # Step 11: input with available_at > information_cutoff fails closed
        # ---------------------------------------------------------
        with pytest.raises(ValueError, match="fails closed"):
            service.create_run(
                run_id=f"run_{uuid.uuid4()}",
                forecast_issue_time=datetime(2026, 1, 1, tzinfo=UTC),
                information_cutoff=datetime(2025, 12, 31, tzinfo=UTC),
                configuration_snapshot_id=snapshot.configuration_snapshot_id,
                configuration_digest=snapshot.configuration_digest,
                code_revision=code_revision,
                environment_digest=environment_digest,
                lockfile_digest="sha256:" + "d" * 64,
                random_seed=1,
                selected_input_artifact_ids=(source_artifact.artifact_id,),
            )

        # ---------------------------------------------------------
        # Step 12: wrong digest retrieval raises IntegrityError
        # ---------------------------------------------------------
        wrong_digest = "sha256:" + "0" * 64
        with pytest.raises(IntegrityError):
            object_store.get_verified(output_artifact.storage_uri, wrong_digest)
