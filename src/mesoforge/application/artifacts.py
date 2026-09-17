"""Application service for artifact registration and transformation
(plan Section 5). Composes provenance/catalog/storage.interfaces
protocols; owns transaction ordering, not meteorological algorithms.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol

import jcs
from pydantic import BaseModel, ConfigDict, field_validator

from mesoforge.common.errors import Conflict, IntegrityError, NotFound
from mesoforge.common.identifiers import (
    ActivityId,
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    RunId,
    validate_code_revision,
)
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
    content_digest: Digest
    storage_uri: str
    media_type: str
    byte_size: int


class _ObjectStoreLike(Protocol):
    def put_if_absent(
        self, content_digest: Digest, data: bytes, media_type: str
    ) -> _StoredObjectLike: ...
    def get_verified(self, storage_uri: str, expected_digest: Digest) -> bytes: ...


class _StoredObjectRepositoryLike(Protocol):
    def add_if_absent(self, stored_object: _StoredObjectLike) -> _StoredObjectLike: ...


class _ArtifactRepositoryLike(Protocol):
    def add(self, manifest: ArtifactManifest) -> ArtifactManifest: ...
    def get(self, artifact_id: ArtifactId) -> ArtifactManifest: ...
    def get_many(self, ids: tuple[ArtifactId, ...]) -> tuple[ArtifactManifest, ...]: ...
    def find_by_source_registration_digest(self, digest: Digest) -> ArtifactManifest | None: ...
    def add_derived(
        self,
        *,
        artifact_id: ArtifactId,
        artifact_type: str,
        artifact_schema_version: str,
        content_digest: Digest,
        created_at: datetime,
        availability_authority: str,
        availability_method: str,
        parent_available_ats: tuple[datetime, ...],
        activity_completed_at: datetime,
        run_id: RunId | None,
        configuration_snapshot_id: ConfigurationSnapshotId,
        configuration_digest: Digest,
        code_revision: str,
        environment_digest: Digest,
        quality_state: str,
        attributes: dict[str, object] | None,
    ) -> ArtifactManifest: ...


class _ActivityRepositoryLike(Protocol):
    def add_started(self, manifest: ActivityManifest) -> ActivityManifest: ...
    def finish_succeeded(
        self,
        activity_id: ActivityId,
        outputs: tuple[ActivityArtifactRef, ...],
        completed_at: datetime,
    ) -> ActivityManifest: ...
    def finish_failed(
        self, activity_id: ActivityId, error: ActivityError, completed_at: datetime
    ) -> ActivityManifest: ...
    def find_succeeded_by_idempotency(self, digest: Digest) -> ActivityManifest | None: ...


class _ConfigurationSnapshotLike(Protocol):
    configuration_snapshot_id: ConfigurationSnapshotId
    configuration_digest: Digest


class _ConfigurationRepositoryLike(Protocol):
    def get(self, snapshot_id: ConfigurationSnapshotId) -> _ConfigurationSnapshotLike: ...


class _RunRepositoryLike(Protocol):
    def add(self, manifest: RunManifest) -> RunManifest: ...
    def get(self, run_id: RunId) -> RunManifest: ...


class _UnitOfWorkLike(Protocol):
    stored_objects: _StoredObjectRepositoryLike
    artifacts: _ArtifactRepositoryLike
    activities: _ActivityRepositoryLike
    configurations: _ConfigurationRepositoryLike
    runs: _RunRepositoryLike

    def __enter__(self) -> _UnitOfWorkLike: ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> object | None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class _IdempotencyLockLike(Protocol):
    def acquire(self, digest: Digest) -> Any: ...  # context manager


# --------------------------------------------------------------------------
# Request/result contracts
# --------------------------------------------------------------------------


class SourceRegistrationRequest(BaseModel):
    """Caller-owned request to register an external source artifact. The
    caller never supplies source_registration_digest -- the service
    computes it from the named identity fields.

    ID/digest fields use the typed ``common.identifiers`` types directly
    (Codex review t_f569c45c finding 3: the public request boundary must
    not accept an unrestricted ``str`` for
    configuration_snapshot_id/configuration_digest/environment_digest/
    run_id/expected_content_digest/code_revision) so a malformed value
    is rejected by Pydantic at construction time, before the request
    ever reaches the service body.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_authority: str
    source_locator: str
    source_revision: str
    artifact_type: str
    artifact_schema_version: str
    media_type: str
    expected_content_digest: Digest | None = None
    created_at: UtcInstant
    availability: Availability
    run_id: RunId | None = None
    configuration_snapshot_id: ConfigurationSnapshotId
    configuration_digest: Digest
    code_revision: str
    environment_digest: Digest
    quality_state: str = "valid"
    attributes: dict[str, object] | None = None

    @field_validator("code_revision")
    @classmethod
    def _check_code_revision(cls, value: str) -> str:
        return validate_code_revision(value)


class TransformationInputRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: str
    artifact_id: ArtifactId


class TransformationRequest(BaseModel):
    """Caller-owned request to execute a derived-artifact transformation.
    ``parameters`` are hashed (JCS/SHA-256) into ``parameters_digest``
    internally; the idempotency digest never depends on output bytes.

    ID/digest fields use the typed ``common.identifiers`` types directly
    (Codex review t_f569c45c finding 3), matching
    ``SourceRegistrationRequest`` above.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    activity_type: str
    activity_version: str
    inputs: tuple[TransformationInputRef, ...]
    output_role: str
    output_artifact_type: str
    output_artifact_schema_version: str
    output_media_type: str
    parameters: dict[str, object] = {}
    configuration_snapshot_id: ConfigurationSnapshotId
    configuration_digest: Digest
    code_revision: str
    environment_digest: Digest
    run_id: RunId | None = None
    quality_state: str = "valid"
    # Searchable output metadata only; never part of the idempotency digest or payload.
    attributes: dict[str, object] | None = None

    @field_validator("code_revision")
    @classmethod
    def _check_code_revision(cls, value: str) -> str:
        return validate_code_revision(value)


class TransformationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    activity: ActivityManifest
    output: ArtifactManifest


class AtomicTransformationResult(BaseModel):
    """Two artifacts committed as outputs of one idempotent activity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    activity: ActivityManifest
    outputs: tuple[ArtifactManifest, ArtifactManifest]


class InputBinding:
    """A role-scoped ``(loader, validator)`` pair for
    ``execute_role_bound_transformation`` (plan Section 4.4).

    Both callables are mandatory. Raw inputs use an explicit raw-byte
    contract validator; validation may not be hidden in a loader or
    silently disabled with ``None``.
    """

    __slots__ = ("loader", "validator")

    def __init__(
        self,
        loader: Callable[[bytes], Any],
        validator: Callable[[Any], None],
    ) -> None:
        if not callable(loader) or not callable(validator):
            raise TypeError("InputBinding requires callable loader and validator")
        self.loader = loader
        self.validator = validator


def _parameters_digest(parameters: dict[str, object]) -> Digest:
    return Digest.of_bytes(jcs.canonicalize(parameters))


def _source_registration_digest(
    request: SourceRegistrationRequest, content_digest: Digest
) -> Digest:
    immutable_source_identity = _uses_immutable_source_identity(request)
    payload = {
        "source_authority": request.source_authority,
        "source_locator": request.source_locator,
        "source_revision": request.source_revision,
        "content_digest": (
            "immutable-source-identity" if immutable_source_identity else str(content_digest)
        ),
        "artifact_schema_version": request.artifact_schema_version,
        "configuration_digest": request.configuration_digest,
        "code_revision": request.code_revision,
        "environment_digest": request.environment_digest,
        "quality_state": request.quality_state,
    }
    return Digest.of_bytes(jcs.canonicalize(payload))


def _uses_immutable_source_identity(request: SourceRegistrationRequest) -> bool:
    """Reserve the Phase 2 METAR namespace as once-only per run.

    This is derived from the validated source contract rather than a caller
    flag, so a second caller cannot bypass byte immutability by omitting an
    option while reusing the same authoritative observation identity.
    """
    return (
        request.source_authority == "aviationweather.gov"
        and request.source_locator.startswith("phase2-metar://run_")
        and request.source_revision == "phase2.v1"
        and request.artifact_type == "aviationweather-metar-response"
        and request.artifact_schema_version == "aviationweather-metar-response.v1"
    )


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

    def _verify_configuration_consistency(
        self,
        uow: _UnitOfWorkLike,
        *,
        configuration_snapshot_id: ConfigurationSnapshotId,
        configuration_digest: Digest,
    ) -> None:
        """Load the referenced configuration snapshot from the repository
        and verify the caller-supplied ``configuration_digest`` matches
        the registered digest for that snapshot ID. Raises ``NotFound``
        for an unregistered snapshot ID, or ``ValueError`` for a
        tampered/inconsistent snapshot-ID+digest pair. Shared by
        ``register_source`` and ``execute_transformation`` (Codex review
        t_9bb13e2b finding 8: referenced configuration snapshot/digest
        consistency must be validated in both, not only in
        ``create_run``)."""
        snapshot = uow.configurations.get(configuration_snapshot_id)
        if snapshot.configuration_digest != configuration_digest:
            raise ValueError(
                f"configuration_digest {configuration_digest!r} does not match the "
                f"registered digest {snapshot.configuration_digest!r} for snapshot "
                f"{configuration_snapshot_id!r}; registration fails closed"
            )

    # ------------------------------------------------------------------
    # Authoritative artifact retrieval (immutable replay roots)
    # ------------------------------------------------------------------

    def load_verified_payload(self, artifact_id: ArtifactId) -> tuple[ArtifactManifest, bytes]:
        """Load an artifact's repository-authoritative manifest and its
        integrity-verified bytes.

        Callers that must reconstruct a run from persisted state (Phase 2
        replay) may not trust a caller-supplied manifest for
        availability, quality state, configuration identity, or storage
        location: the manifest is re-read from the repository by ID and
        the payload is retrieved through ``get_verified``, so a tampered
        object or a forged manifest fails closed before any policy is
        reconstructed from it.
        """
        artifact_id = ArtifactId(artifact_id)
        with self._unit_of_work_factory() as uow:
            manifest = uow.artifacts.get(artifact_id)
        payload = self._object_store.get_verified(manifest.storage_uri, manifest.content_digest)
        return manifest, payload

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

        with self._idempotency_lock.acquire(source_registration_digest):
            with self._unit_of_work_factory() as uow:
                self._verify_configuration_consistency(
                    uow,
                    configuration_snapshot_id=request.configuration_snapshot_id,
                    configuration_digest=request.configuration_digest,
                )
                existing = uow.artifacts.find_by_source_registration_digest(
                    source_registration_digest
                )
                if existing is not None:
                    if (
                        _uses_immutable_source_identity(request)
                        and existing.content_digest != content_digest
                    ):
                        raise IntegrityError(
                            "immutable source identity was already registered with different bytes"
                        )
                    return existing

            stored_object = self._object_store.put_if_absent(
                content_digest, payload, request.media_type
            )

            registered_at = datetime.now(UTC)
            manifest = ArtifactManifest(
                artifact_id=ArtifactId.generate(),
                artifact_type=request.artifact_type,
                artifact_schema_version=request.artifact_schema_version,
                media_type=stored_object.media_type,
                byte_size=stored_object.byte_size,
                content_digest=content_digest,
                storage_uri=stored_object.storage_uri,
                created_at=request.created_at,
                registered_at=registered_at,
                availability=request.availability,
                run_id=(RunId(request.run_id) if request.run_id is not None else None),
                configuration_snapshot_id=ConfigurationSnapshotId(
                    request.configuration_snapshot_id
                ),
                configuration_digest=Digest(request.configuration_digest),
                code_revision=request.code_revision,
                environment_digest=Digest(request.environment_digest),
                quality_state=request.quality_state,
                source_registration_digest=source_registration_digest,
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
        input_validator: Callable[[Any], None],
    ) -> TransformationResult:
        """Canonical transformation API for scientific (xarray.Dataset)
        payloads. ``input_validator`` is a REQUIRED keyword argument (no
        default) -- Codex review t_f569c45c finding 1 found the previous
        signature made ``input_validator`` optional and silently skipped
        contract validation whenever a caller omitted it, letting
        malformed units/semantics/vertical definitions/dimensions reach
        ``transform`` unnoticed. There is no way to call this method
        without supplying a validator; callers that genuinely transform
        non-dataset payloads (raw bytes/strings, never a scientific
        dataset) must use ``execute_raw_transformation`` instead, whose
        distinct name makes it impossible to confuse with this canonical
        dataset-validating path.
        """
        return self._execute_transformation(
            request,
            transform,
            serializer,
            input_loader=input_loader,
            output_validator=output_validator,
            input_validator=input_validator,
        )

    def execute_raw_transformation(
        self,
        request: TransformationRequest,
        transform: Callable[..., Any],
        serializer: Any,
        *,
        input_loader: Callable[[bytes], Any],
        output_validator: Callable[[Any], None],
    ) -> TransformationResult:
        """Non-dataset transformation API: no scientific/contract
        validation is performed on deserialized inputs (there is no
        ``input_validator`` parameter at all -- this is the "clearly
        non-dataset API that cannot be confused with" the canonical
        dataset-validating ``execute_transformation`` per Codex review
        t_f569c45c finding 1). Use only for payloads that are not
        canonical xarray.Dataset guidance (e.g. the in-memory
        unit-test doubles in tests/unit/application/test_artifact_service.py,
        which transform raw bytes/strings).
        """
        return self._execute_transformation(
            request,
            transform,
            serializer,
            input_loader=input_loader,
            output_validator=output_validator,
            input_validator=None,
        )

    def execute_atomic_raw_pair(
        self,
        first: TransformationRequest,
        second: TransformationRequest,
        transform: Callable[..., tuple[Any, Any]],
        *,
        serializers: tuple[Any, Any],
        input_loader: Callable[[bytes], Any],
        output_validators: tuple[Callable[[Any], None], Callable[[Any], None]],
    ) -> AtomicTransformationResult:
        """Execute and commit exactly two raw outputs under one activity.

        Phase 2's forecast and contribution manifest are an atomic pair.  The
        ordinary transformation API intentionally has one output, so this narrow
        API provides the required all-or-nothing database edge without weakening
        that simpler contract.
        """
        shared_fields = (
            "activity_type",
            "activity_version",
            "inputs",
            "parameters",
            "configuration_snapshot_id",
            "configuration_digest",
            "code_revision",
            "environment_digest",
            "run_id",
            "quality_state",
        )
        mismatched = [
            name for name in shared_fields if getattr(first, name) != getattr(second, name)
        ]
        if mismatched:
            raise ValueError(f"atomic pair requests differ in shared fields: {mismatched!r}")
        if first.output_role == second.output_role:
            raise ValueError("atomic pair output roles must be distinct")

        ordered_inputs = tuple((ref.role, ref.artifact_id) for ref in first.inputs)
        parameters_digest = _parameters_digest(first.parameters)
        output_schema = (
            f"{first.output_artifact_schema_version}+{second.output_artifact_schema_version}"
        )
        idempotency_digest = compute_idempotency_digest(
            activity_type=first.activity_type,
            activity_version=first.activity_version,
            ordered_inputs=ordered_inputs,
            configuration_digest=first.configuration_digest,
            parameters_digest=parameters_digest,
            code_revision=first.code_revision,
            environment_digest=first.environment_digest,
            output_schema=output_schema,
        )
        with self._idempotency_lock.acquire(idempotency_digest):
            with self._unit_of_work_factory() as uow:
                self._verify_configuration_consistency(
                    uow,
                    configuration_snapshot_id=first.configuration_snapshot_id,
                    configuration_digest=first.configuration_digest,
                )
                existing = uow.activities.find_succeeded_by_idempotency(idempotency_digest)
                if existing is not None:
                    if len(existing.outputs) != 2:
                        raise IntegrityError(
                            "atomic pair activity does not have exactly two outputs"
                        )
                    manifests = uow.artifacts.get_many(
                        tuple(reference.artifact_id for reference in existing.outputs)
                    )
                    return AtomicTransformationResult(
                        activity=existing, outputs=(manifests[0], manifests[1])
                    )
                input_manifests = [
                    uow.artifacts.get(artifact_id) for _, artifact_id in ordered_inputs
                ]

            inputs = [
                input_loader(self._object_store.get_verified(item.storage_uri, item.content_digest))
                for item in input_manifests
            ]
            activity_id = ActivityId(f"act_{uuid.uuid4()}")
            started = ActivityManifest(
                activity_id=activity_id,
                activity_type=first.activity_type,
                activity_version=first.activity_version,
                status="started",
                started_at=datetime.now(UTC),
                idempotency_digest=idempotency_digest,
                parameters_digest=parameters_digest,
                configuration_snapshot_id=first.configuration_snapshot_id,
                configuration_digest=first.configuration_digest,
                code_revision=first.code_revision,
                environment_digest=first.environment_digest,
                run_id=first.run_id,
                inputs=tuple(
                    ActivityArtifactRef(role=role, artifact_id=artifact_id)
                    for role, artifact_id in ordered_inputs
                ),
            )
            with self._unit_of_work_factory() as uow:
                uow.activities.add_started(started)
                uow.commit()
            try:
                values = transform(*inputs)
                if not isinstance(values, tuple) or len(values) != 2:
                    raise ValueError("atomic pair transform must return exactly two values")
                for validator, value in zip(output_validators, values, strict=True):
                    validator(value)
                payloads = tuple(
                    serializer.serialize(value)
                    for serializer, value in zip(serializers, values, strict=True)
                )
                stored = tuple(
                    self._object_store.put_if_absent(
                        Digest.of_bytes(payload), payload, request.output_media_type
                    )
                    for payload, request in zip(payloads, (first, second), strict=True)
                )
                completed_at = datetime.now(UTC)
                parent_times = tuple(item.availability.available_at for item in input_manifests)
                created: list[ArtifactManifest] = []
                with self._unit_of_work_factory() as uow:
                    for stored_object, request in zip(stored, (first, second), strict=True):
                        uow.stored_objects.add_if_absent(stored_object)
                        created.append(
                            uow.artifacts.add_derived(
                                artifact_id=ArtifactId.generate(),
                                artifact_type=request.output_artifact_type,
                                artifact_schema_version=request.output_artifact_schema_version,
                                content_digest=stored_object.content_digest,
                                created_at=completed_at,
                                availability_authority="mesoforge.derived",
                                availability_method=f"{first.activity_type}.{first.activity_version}",
                                parent_available_ats=parent_times,
                                activity_completed_at=completed_at,
                                run_id=first.run_id,
                                configuration_snapshot_id=first.configuration_snapshot_id,
                                configuration_digest=first.configuration_digest,
                                code_revision=first.code_revision,
                                environment_digest=first.environment_digest,
                                quality_state=request.quality_state,
                                attributes=request.attributes,
                            )
                        )
                    refs = tuple(
                        ActivityArtifactRef(
                            role=request.output_role, artifact_id=manifest.artifact_id
                        )
                        for request, manifest in zip((first, second), created, strict=True)
                    )
                    finished = uow.activities.finish_succeeded(activity_id, refs, completed_at)
                    uow.commit()
                return AtomicTransformationResult(
                    activity=finished, outputs=(created[0], created[1])
                )
            except Exception as exc:
                failure_time = datetime.now(UTC)
                error = ActivityError(
                    error_type=type(exc).__name__,
                    message_digest=Digest.of_bytes(str(exc).encode()),
                    retryable=not isinstance(exc, IntegrityError),
                )
                with self._unit_of_work_factory() as uow:
                    uow.activities.finish_failed(activity_id, error, failure_time)
                    uow.commit()
                raise

    def execute_role_bound_transformation(
        self,
        request: TransformationRequest,
        transform: Callable[[Mapping[str, Any]], Any],
        serializer: Any,
        *,
        input_bindings: Mapping[str, InputBinding],
        output_validator: Callable[[Any], None],
    ) -> TransformationResult:
        """Mixed-codec transformation API (plan Section 4.4): each input
        role is deserialized and validated with its own
        ``InputBinding(loader, validator)`` rather than one uniform
        loader/validator applied to every input. ``transform`` receives
        a single immutable ``role -> deserialized value`` mapping
        (``types.MappingProxyType``), built in request role order, so a
        transform combining NetCDF/Parquet/canonical-JSON inputs can
        address each by its declared role rather than positional index.

        ``input_bindings`` keys must equal ``request.inputs`` roles
        exactly (no missing role, no extra role) -- both directions are
        checked before any I/O so a caller cannot silently skip
        validating one role of a multi-role request.
        """
        request_roles = tuple(ref.role for ref in request.inputs)
        binding_roles = tuple(input_bindings.keys())
        missing = set(request_roles) - set(binding_roles)
        extra = set(binding_roles) - set(request_roles)
        if missing or extra:
            raise ValueError(
                "input_bindings roles must equal request input roles exactly; "
                f"missing={sorted(missing)!r} extra={sorted(extra)!r}"
            )
        if len(set(request_roles)) != len(request_roles):
            raise ValueError(
                f"request.inputs roles must be unique for role-bound execution, got "
                f"{request_roles!r}"
            )

        return self._execute_transformation(
            request,
            transform,
            serializer,
            input_loader=None,
            output_validator=output_validator,
            input_validator=None,
            role_bindings=input_bindings,
        )

    def _execute_transformation(
        self,
        request: TransformationRequest,
        transform: Callable[..., Any],
        serializer: Any,
        *,
        input_loader: Callable[[bytes], Any] | None,
        output_validator: Callable[[Any], None],
        input_validator: Callable[[Any], None] | None,
        role_bindings: Mapping[str, InputBinding] | None = None,
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

        ``input_validator``, when not ``None``, is called once per
        deserialized input dataset (step 4) before ``transform`` runs --
        closing Codex review finding 1 (t_9bb13e2b). Only
        ``execute_raw_transformation`` ever calls this private method
        with ``input_validator=None``; the public canonical
        ``execute_transformation`` always supplies one (Codex review
        t_f569c45c finding 1: the public method itself must not make
        validation optional).

        Cycle prevention (Codex review t_9bb13e2b finding 8): this service
        never attaches a pre-existing artifact as an activity's output --
        every successful transformation always generates a brand-new
        ``ArtifactId`` via ``add_derived`` (or returns the pre-existing
        winner's own manifest on an idempotent repeat, never a
        newly-composed edge). A lineage cycle would require an activity
        whose output already exists as one of its own ancestors, which is
        structurally impossible here because outputs are never selected
        from existing records. ``provenance.lineage.detect_cycle`` (used
        directly in ``tests/unit/provenance/test_lineage.py``) remains the
        primitive for any future service that *does* attach pre-existing
        outputs (e.g. a batch/merge activity); it is exercised there
        rather than through this service, matching the plan's
        "if the service can attach pre-existing outputs" qualifier.
        """
        ordered_inputs = tuple((ref.role, ref.artifact_id) for ref in request.inputs)
        parameters_digest = _parameters_digest(request.parameters)
        idempotency_digest = compute_idempotency_digest(
            activity_type=request.activity_type,
            activity_version=request.activity_version,
            ordered_inputs=ordered_inputs,
            configuration_digest=request.configuration_digest,
            parameters_digest=parameters_digest,
            code_revision=request.code_revision,
            environment_digest=request.environment_digest,
            output_schema=request.output_artifact_schema_version,
        )

        with self._idempotency_lock.acquire(idempotency_digest):
            with self._unit_of_work_factory() as uow:
                self._verify_configuration_consistency(
                    uow,
                    configuration_snapshot_id=request.configuration_snapshot_id,
                    configuration_digest=request.configuration_digest,
                )
                existing_activity = uow.activities.find_succeeded_by_idempotency(idempotency_digest)
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
            if role_bindings is not None:
                # Role-aware path (plan Section 4.4): load/validate in
                # request role order using each role's own binding; the
                # transform receives an immutable role->value mapping,
                # not a positional dataset list.
                input_datasets = []
                role_to_value: dict[str, Any] = {}
                for (role, _artifact_id), payload in zip(
                    ordered_inputs, verified_payloads, strict=True
                ):
                    binding = role_bindings[role]
                    value = binding.loader(payload)
                    binding.validator(value)
                    input_datasets.append(value)
                    role_to_value[role] = value
                transform_input: Any = MappingProxyType(role_to_value)
            else:
                assert input_loader is not None  # noqa: S101 -- guarded by public API split
                input_datasets = [input_loader(payload) for payload in verified_payloads]
                if input_validator is not None:
                    for dataset in input_datasets:
                        input_validator(dataset)
                transform_input = tuple(input_datasets)

            activity_id = str(uuid.uuid4())
            activity_id_typed = ActivityId(f"act_{activity_id}")
            started_manifest = ActivityManifest(
                activity_id=activity_id_typed,
                activity_type=request.activity_type,
                activity_version=request.activity_version,
                status="started",
                started_at=datetime.now(UTC),
                idempotency_digest=idempotency_digest,
                parameters_digest=parameters_digest,
                configuration_snapshot_id=ConfigurationSnapshotId(
                    request.configuration_snapshot_id
                ),
                configuration_digest=Digest(request.configuration_digest),
                code_revision=request.code_revision,
                environment_digest=Digest(request.environment_digest),
                run_id=(RunId(request.run_id) if request.run_id is not None else None),
                inputs=tuple(
                    ActivityArtifactRef(role=ref.role, artifact_id=ArtifactId(ref.artifact_id))
                    for ref in request.inputs
                ),
            )
            with self._unit_of_work_factory() as uow:
                uow.activities.add_started(started_manifest)
                uow.commit()

            try:
                if role_bindings is not None:
                    output_dataset = transform(transform_input)
                else:
                    output_dataset = transform(*input_datasets)
                output_validator(output_dataset)

                serialized = serializer.serialize(output_dataset)
                content_digest = Digest.of_bytes(serialized)
                stored_object = self._object_store.put_if_absent(
                    content_digest, serialized, request.output_media_type
                )

                # completed_at is real wall-clock time for the activity's
                # own record (it is not itself the authoritative
                # registration/availability instant). registered_at and
                # available_at are computed inside one PostgreSQL
                # transaction_timestamp() by add_derived below (plan
                # Section 5 step 8; Codex review t_9bb13e2b finding 3) --
                # never with datetime.now() here.
                completed_at = datetime.now(UTC)
                parent_available_ats = tuple(m.availability.available_at for m in input_manifests)
                output_artifact_id = ArtifactId.generate()

                with self._unit_of_work_factory() as uow:
                    uow.stored_objects.add_if_absent(stored_object)
                    created_output = uow.artifacts.add_derived(
                        artifact_id=output_artifact_id,
                        artifact_type=request.output_artifact_type,
                        artifact_schema_version=request.output_artifact_schema_version,
                        content_digest=content_digest,
                        created_at=completed_at,
                        availability_authority="mesoforge.derived",
                        availability_method=f"{request.activity_type}.{request.activity_version}",
                        parent_available_ats=parent_available_ats,
                        activity_completed_at=completed_at,
                        run_id=request.run_id,
                        configuration_snapshot_id=request.configuration_snapshot_id,
                        configuration_digest=request.configuration_digest,
                        code_revision=request.code_revision,
                        environment_digest=request.environment_digest,
                        quality_state=request.quality_state,
                        attributes=request.attributes,
                    )
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
                    message_digest=Digest.of_bytes(str(exc).encode("utf-8")),
                    retryable=not isinstance(exc, IntegrityError),
                )
                with self._unit_of_work_factory() as uow:
                    uow.activities.finish_failed(activity_id_typed, error, failure_time)
                    uow.commit()
                raise

    # ------------------------------------------------------------------
    # Run creation (plan Section 4.8: selected inputs are loaded
    # transactionally from the repository, not trusted from the caller;
    # invariant "each selected source input has authoritative
    # available_at <= information_cutoff" fails closed and creates no
    # run row. Codex review t_9bb13e2b finding 5.)
    # ------------------------------------------------------------------

    def create_run(
        self,
        *,
        run_id: RunId,
        forecast_issue_time: datetime,
        information_cutoff: datetime,
        configuration_snapshot_id: ConfigurationSnapshotId,
        configuration_digest: Digest,
        code_revision: str,
        environment_digest: Digest,
        lockfile_digest: Digest,
        random_seed: int,
        selected_input_artifact_ids: tuple[ArtifactId, ...],
        require_source_inputs: bool = True,
    ) -> RunManifest:
        """
        Exact order (fail-closed at every step; no run row is created
        unless all checks pass):

        0. every ID/digest parameter is re-validated against its typed
           ``common.identifiers`` type immediately -- the public
           signature itself only accepts the typed ``RunId`` /
           ``ConfigurationSnapshotId`` / ``Digest`` /
           ``tuple[ArtifactId, ...]`` types (final re-review HIGH
           finding 6/t_1ecb8414: a ``str | Typed`` union boundary is
           not a typed boundary), and re-constructing each typed value
           here also rejects a caller that bypasses static typing (e.g.
           an untyped/dynamic caller) at runtime, before any
           repository call (Codex review t_f569c45c finding 3);
        1. load the configuration snapshot from the repository and
           verify the caller-supplied ``configuration_digest`` matches
           it -- a tampered/inconsistent snapshot+digest pair is
           rejected before any artifact lookup;
        2. load every selected artifact transactionally from the
           repository by ID, inside the same unit of work as the
           eventual run insert -- never trust a caller-supplied
           manifest for authoritative fields (availability, source
           identity). A nonexistent artifact ID raises ``NotFound``;
        3. when ``require_source_inputs`` is true (the default -- runs
           select *source* inputs, matching the plan's reviewed run
           manifest semantics), reject any selected artifact that is
           itself derived (``source_registration_digest is None``);
        4. validate authoritative availability against the cutoff:
           every selected input's repository-loaded
           ``availability.available_at`` must be ``<=
           information_cutoff``;
        5. insert the run row and its ordered ``run_selected_inputs``
           relational rows in the same transaction, then commit.
        """
        run_id = RunId(run_id)
        configuration_snapshot_id = ConfigurationSnapshotId(configuration_snapshot_id)
        configuration_digest = Digest(configuration_digest)
        validate_code_revision(code_revision)
        environment_digest = Digest(environment_digest)
        lockfile_digest = Digest(lockfile_digest)
        selected_input_artifact_ids = tuple(ArtifactId(a) for a in selected_input_artifact_ids)

        with self._unit_of_work_factory() as uow:
            snapshot = uow.configurations.get(configuration_snapshot_id)
            if snapshot.configuration_digest != configuration_digest:
                raise ValueError(
                    f"configuration_digest {configuration_digest!r} does not match the "
                    f"registered digest {snapshot.configuration_digest!r} for snapshot "
                    f"{configuration_snapshot_id!r}; run creation fails closed"
                )

            selected_inputs = uow.artifacts.get_many(selected_input_artifact_ids)

            if require_source_inputs:
                for artifact in selected_inputs:
                    if artifact.source_registration_digest is None:
                        raise ValueError(
                            f"selected input {artifact.artifact_id!r} is a derived artifact, "
                            "not a source/root artifact; run creation requires source inputs "
                            "and fails closed"
                        )

            for artifact in selected_inputs:
                if artifact.availability.available_at > information_cutoff:
                    raise ValueError(
                        f"selected input {artifact.artifact_id!r} has available_at "
                        f"{artifact.availability.available_at!r} after information_cutoff "
                        f"{information_cutoff!r}; run creation fails closed"
                    )

            try:
                existing = uow.runs.get(run_id)
            except NotFound:
                existing = None
            if existing is not None:
                requested_identity = (
                    forecast_issue_time,
                    information_cutoff,
                    configuration_snapshot_id,
                    configuration_digest,
                    code_revision,
                    environment_digest,
                    lockfile_digest,
                    random_seed,
                    tuple(a.artifact_id for a in selected_inputs),
                )
                existing_identity = (
                    existing.forecast_issue_time,
                    existing.information_cutoff,
                    existing.configuration_snapshot_id,
                    existing.configuration_digest,
                    existing.code_revision,
                    existing.environment_digest,
                    existing.lockfile_digest,
                    existing.random_seed,
                    existing.selected_input_artifact_ids,
                )
                if existing_identity != requested_identity:
                    raise Conflict(
                        f"run_id {run_id!r} already exists with a different immutable identity"
                    )
                return existing

            manifest = RunManifest(
                run_id=RunId(run_id),
                forecast_issue_time=forecast_issue_time,
                information_cutoff=information_cutoff,
                configuration_snapshot_id=ConfigurationSnapshotId(configuration_snapshot_id),
                configuration_digest=Digest(configuration_digest),
                code_revision=code_revision,
                environment_digest=Digest(environment_digest),
                lockfile_digest=Digest(lockfile_digest),
                random_seed=random_seed,
                selected_input_artifact_ids=tuple(
                    ArtifactId(a.artifact_id) for a in selected_inputs
                ),
                created_at=datetime.now(UTC),
            )
            created = uow.runs.add(manifest)
            uow.commit()
            return created
