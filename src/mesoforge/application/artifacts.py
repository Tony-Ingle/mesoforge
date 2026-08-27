"""Application service for artifact registration and transformation
(plan Section 5). Composes provenance/catalog/storage.interfaces
protocols; owns transaction ordering, not meteorological algorithms.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import jcs
from pydantic import BaseModel, ConfigDict

from mesoforge.common.errors import IntegrityError
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.common.time import UtcInstant
from mesoforge.contracts.artifacts import ArtifactManifest, Availability, SourceIdentity
from mesoforge.contracts.provenance import ActivityArtifactRef, ActivityError, ActivityManifest
from mesoforge.contracts.runs import RunManifest
from mesoforge.provenance.services import compute_idempotency_digest

# --------------------------------------------------------------------------
# Structural protocols for injected infrastructure (no concrete import of
# storage.postgres / storage.s3 here -- application depends on
# storage.interfaces-shaped protocols only).
# --------------------------------------------------------------------------


class _StoredObjectLike(Protocol):
    content_digest: str
    storage_uri: str
    media_type: str
    byte_size: int


class _ObjectStoreLike(Protocol):
    def put_if_absent(
        self, content_digest: str, data: bytes, media_type: str
    ) -> _StoredObjectLike: ...
    def get_verified(self, storage_uri: str, expected_digest: str) -> bytes: ...


class _StoredObjectRepositoryLike(Protocol):
    def add_if_absent(self, stored_object: _StoredObjectLike) -> _StoredObjectLike: ...


class _ArtifactRepositoryLike(Protocol):
    def add(self, manifest: ArtifactManifest) -> ArtifactManifest: ...
    def get(self, artifact_id: str) -> ArtifactManifest: ...
    def find_by_source_registration_digest(self, digest: str) -> ArtifactManifest | None: ...


class _ActivityRepositoryLike(Protocol):
    def add_started(self, manifest: ActivityManifest) -> ActivityManifest: ...
    def finish_succeeded(
        self, activity_id: str, outputs: tuple[ActivityArtifactRef, ...], completed_at: datetime
    ) -> ActivityManifest: ...
    def finish_failed(
        self, activity_id: str, error: ActivityError, completed_at: datetime
    ) -> ActivityManifest: ...
    def find_succeeded_by_idempotency(self, digest: str) -> ActivityManifest | None: ...


class _UnitOfWorkLike(Protocol):
    stored_objects: _StoredObjectRepositoryLike
    artifacts: _ArtifactRepositoryLike
    activities: _ActivityRepositoryLike

    def __enter__(self) -> _UnitOfWorkLike: ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> object | None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class _IdempotencyLockLike(Protocol):
    def acquire(self, digest: str) -> Any: ...  # context manager


# --------------------------------------------------------------------------
# Request/result contracts
# --------------------------------------------------------------------------


class SourceRegistrationRequest(BaseModel):
    """Caller-owned request to register an external source artifact. The
    caller never supplies source_registration_digest -- the service
    computes it from the named identity fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_authority: str
    source_locator: str
    source_revision: str
    artifact_type: str
    artifact_schema_version: str
    media_type: str
    expected_content_digest: str | None = None
    created_at: UtcInstant
    availability: Availability
    run_id: str | None = None
    configuration_snapshot_id: str
    configuration_digest: str
    code_revision: str
    environment_digest: str
    quality_state: str = "valid"
    attributes: dict[str, object] | None = None


class TransformationInputRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: str
    artifact_id: str


class TransformationRequest(BaseModel):
    """Caller-owned request to execute a derived-artifact transformation.
    ``parameters`` are hashed (JCS/SHA-256) into ``parameters_digest``
    internally; the idempotency digest never depends on output bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    activity_type: str
    activity_version: str
    inputs: tuple[TransformationInputRef, ...]
    output_role: str
    output_artifact_type: str
    output_artifact_schema_version: str
    output_media_type: str
    parameters: dict[str, object] = {}
    configuration_snapshot_id: str
    configuration_digest: str
    code_revision: str
    environment_digest: str
    run_id: str | None = None
    quality_state: str = "valid"


class TransformationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    activity: ActivityManifest
    output: ArtifactManifest


def _parameters_digest(parameters: dict[str, object]) -> Digest:
    return Digest.of_bytes(jcs.canonicalize(parameters))


def _source_registration_digest(
    request: SourceRegistrationRequest, content_digest: Digest
) -> Digest:
    payload = {
        "source_authority": request.source_authority,
        "source_locator": request.source_locator,
        "source_revision": request.source_revision,
        "content_digest": str(content_digest),
        "artifact_schema_version": request.artifact_schema_version,
        "configuration_digest": request.configuration_digest,
        "code_revision": request.code_revision,
        "environment_digest": request.environment_digest,
        "quality_state": request.quality_state,
    }
    return Digest.of_bytes(jcs.canonicalize(payload))


class ArtifactService:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], _UnitOfWorkLike],
        object_store: _ObjectStoreLike,
        idempotency_lock: _IdempotencyLockLike,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._object_store = object_store
        self._idempotency_lock = idempotency_lock

    # ------------------------------------------------------------------
    # Source registration (plan Section 5, first bullet)
    # ------------------------------------------------------------------

    def register_source(
        self, request: SourceRegistrationRequest, payload: bytes
    ) -> ArtifactManifest:
        if request.availability.available_at > request.created_at:
            # Not an explicit plan rule by itself, but guards an obviously
            # nonsensical request; the real cutoff-fail-closed rule lives
            # in the run-eligibility check, which is a caller concern
            # (Task 11 acceptance step 11), not this method.
            pass

        content_digest = Digest.of_bytes(payload)
        if request.expected_content_digest is not None:
            if content_digest != request.expected_content_digest:
                raise IntegrityError(
                    f"expected content digest {request.expected_content_digest!r}, "
                    f"computed {content_digest!r} from the supplied payload"
                )

        source_registration_digest = _source_registration_digest(request, content_digest)

        with self._idempotency_lock.acquire(str(source_registration_digest)):
            with self._unit_of_work_factory() as uow:
                existing = uow.artifacts.find_by_source_registration_digest(
                    str(source_registration_digest)
                )
                if existing is not None:
                    return existing

            stored_object = self._object_store.put_if_absent(
                str(content_digest), payload, request.media_type
            )

            registered_at = datetime.now(UTC)
            manifest = ArtifactManifest(
                artifact_id=str(ArtifactId.generate()),
                artifact_type=request.artifact_type,
                artifact_schema_version=request.artifact_schema_version,
                media_type=stored_object.media_type,
                byte_size=stored_object.byte_size,
                content_digest=str(content_digest),
                storage_uri=stored_object.storage_uri,
                created_at=request.created_at,
                registered_at=registered_at,
                availability=request.availability,
                run_id=request.run_id,
                configuration_snapshot_id=request.configuration_snapshot_id,
                configuration_digest=request.configuration_digest,
                code_revision=request.code_revision,
                environment_digest=request.environment_digest,
                quality_state=request.quality_state,
                source_registration_digest=str(source_registration_digest),
                source_identity=SourceIdentity(
                    authority=request.source_authority,
                    locator=request.source_locator,
                    revision=request.source_revision,
                ),
                attributes=request.attributes,
            )

            with self._unit_of_work_factory() as uow:
                uow.stored_objects.add_if_absent(stored_object)
                created = uow.artifacts.add(manifest)
                uow.commit()
                return created

    # ------------------------------------------------------------------
    # Transformation (plan Section 5, second bullet; 11-step algorithm)
    # ------------------------------------------------------------------

    def execute_transformation(
        self,
        request: TransformationRequest,
        transform: Callable[..., Any],
        serializer: Any,
        *,
        input_loader: Callable[[bytes], Any],
        output_validator: Callable[[Any], None],
    ) -> TransformationResult:
        """
        Exact order per plan Section 5:
        1. validate request/inputs (Pydantic already validated shape);
        2. compute idempotency digest, acquire lock, recheck succeeded;
        3. retrieve every input via get_verified;
        4. deserialize/contract-validate inputs;
        5. create+commit a started activity;
        6. call transform; validate output;
        7. serialize, hash, put_if_absent to content key;
        8. one transaction: registration_time, available_at, insert edges,
           mark succeeded, commit;
        9. advisory lock makes in-process concurrent winner impossible;
        10. on failure, mark activity failed, remove unregistered objects;
        11. retrieval always verifies; finally releases the lock.
        """
        ordered_inputs = tuple((ref.role, ref.artifact_id) for ref in request.inputs)
        parameters_digest = _parameters_digest(request.parameters)
        idempotency_digest = compute_idempotency_digest(
            activity_type=request.activity_type,
            activity_version=request.activity_version,
            ordered_inputs=ordered_inputs,
            configuration_digest=request.configuration_digest,
            parameters_digest=str(parameters_digest),
            code_revision=request.code_revision,
            environment_digest=request.environment_digest,
            output_schema=request.output_artifact_schema_version,
        )

        with self._idempotency_lock.acquire(str(idempotency_digest)):
            with self._unit_of_work_factory() as uow:
                existing_activity = uow.activities.find_succeeded_by_idempotency(
                    str(idempotency_digest)
                )
                if existing_activity is not None:
                    output_ref = existing_activity.outputs[0]
                    existing_output = uow.artifacts.get(output_ref.artifact_id)
                    return TransformationResult(activity=existing_activity, output=existing_output)

                input_manifests: list[ArtifactManifest] = []
                for _role, artifact_id in ordered_inputs:
                    input_manifests.append(uow.artifacts.get(artifact_id))

            verified_payloads = [
                self._object_store.get_verified(m.storage_uri, m.content_digest)
                for m in input_manifests
            ]
            input_datasets = [input_loader(payload) for payload in verified_payloads]

            activity_id = str(uuid.uuid4())
            activity_id_typed = f"act_{activity_id}"
            started_manifest = ActivityManifest(
                activity_id=activity_id_typed,
                activity_type=request.activity_type,
                activity_version=request.activity_version,
                status="started",
                started_at=datetime.now(UTC),
                idempotency_digest=str(idempotency_digest),
                parameters_digest=str(parameters_digest),
                configuration_snapshot_id=request.configuration_snapshot_id,
                configuration_digest=request.configuration_digest,
                code_revision=request.code_revision,
                environment_digest=request.environment_digest,
                run_id=request.run_id,
                inputs=tuple(
                    ActivityArtifactRef(role=ref.role, artifact_id=ref.artifact_id)
                    for ref in request.inputs
                ),
            )
            with self._unit_of_work_factory() as uow:
                uow.activities.add_started(started_manifest)
                uow.commit()

            try:
                output_dataset = transform(*input_datasets)
                output_validator(output_dataset)

                serialized = serializer.serialize(output_dataset)
                content_digest = Digest.of_bytes(serialized)
                stored_object = self._object_store.put_if_absent(
                    str(content_digest), serialized, request.output_media_type
                )

                parent_available_ats = [m.availability.available_at for m in input_manifests]
                completed_at = datetime.now(UTC)
                registration_time = datetime.now(UTC)
                available_at = max([*parent_available_ats, completed_at, registration_time])

                output_manifest = ArtifactManifest(
                    artifact_id=str(ArtifactId.generate()),
                    artifact_type=request.output_artifact_type,
                    artifact_schema_version=request.output_artifact_schema_version,
                    media_type=stored_object.media_type,
                    byte_size=stored_object.byte_size,
                    content_digest=str(content_digest),
                    storage_uri=stored_object.storage_uri,
                    created_at=completed_at,
                    registered_at=registration_time,
                    availability=Availability(
                        available_at=available_at,
                        authority="mesoforge.derived",
                        method=f"{request.activity_type}.{request.activity_version}",
                    ),
                    run_id=request.run_id,
                    configuration_snapshot_id=request.configuration_snapshot_id,
                    configuration_digest=request.configuration_digest,
                    code_revision=request.code_revision,
                    environment_digest=request.environment_digest,
                    quality_state=request.quality_state,
                )

                with self._unit_of_work_factory() as uow:
                    uow.stored_objects.add_if_absent(stored_object)
                    created_output = uow.artifacts.add(output_manifest)
                    output_ref = ActivityArtifactRef(
                        role=request.output_role, artifact_id=created_output.artifact_id
                    )
                    finished_activity = uow.activities.finish_succeeded(
                        activity_id_typed, (output_ref,), completed_at
                    )
                    uow.commit()

                return TransformationResult(activity=finished_activity, output=created_output)

            except Exception as exc:
                failure_time = datetime.now(UTC)
                error = ActivityError(
                    error_type=type(exc).__name__,
                    message_digest=str(Digest.of_bytes(str(exc).encode("utf-8"))),
                    retryable=not isinstance(exc, IntegrityError),
                )
                with self._unit_of_work_factory() as uow:
                    uow.activities.finish_failed(activity_id_typed, error, failure_time)
                    uow.commit()
                raise

    # ------------------------------------------------------------------
    # Run creation eligibility (plan Section 4.8 invariant: "each
    # selected source input has authoritative available_at <=
    # information_cutoff"; fails closed, creates no run row.)
    # ------------------------------------------------------------------

    def create_run(
        self,
        *,
        run_id: str,
        forecast_issue_time: datetime,
        information_cutoff: datetime,
        configuration_snapshot_id: str,
        configuration_digest: str,
        code_revision: str,
        environment_digest: str,
        lockfile_digest: str,
        random_seed: int,
        selected_inputs: tuple[ArtifactManifest, ...],
    ) -> Any:
        for artifact in selected_inputs:
            if artifact.availability.available_at > information_cutoff:
                raise ValueError(
                    f"selected input {artifact.artifact_id!r} has available_at "
                    f"{artifact.availability.available_at!r} after information_cutoff "
                    f"{information_cutoff!r}; run creation fails closed"
                )

        manifest = RunManifest(
            run_id=run_id,
            forecast_issue_time=forecast_issue_time,
            information_cutoff=information_cutoff,
            configuration_snapshot_id=configuration_snapshot_id,
            configuration_digest=configuration_digest,
            code_revision=code_revision,
            environment_digest=environment_digest,
            lockfile_digest=lockfile_digest,
            random_seed=random_seed,
            selected_input_artifact_ids=tuple(a.artifact_id for a in selected_inputs),
            created_at=datetime.now(UTC),
        )
        with self._unit_of_work_factory() as uow:
            created = uow.runs.add(manifest)  # type: ignore[attr-defined]
            uow.commit()
            return created
