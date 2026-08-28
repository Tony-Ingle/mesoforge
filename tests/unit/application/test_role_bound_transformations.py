"""Unit tests for ArtifactService.execute_role_bound_transformation
(plan Section 4.4/Task 3): mixed-codec (canonical JSON + raw bytes)
transformations with per-role loaders/validators.

Uses the same behaviorally complete in-memory test doubles as
tests/unit/application/test_artifact_service.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mesoforge.application.artifacts import (
    ArtifactService,
    InputBinding,
    SourceRegistrationRequest,
    TransformationInputRef,
    TransformationRequest,
)
from mesoforge.contracts.artifacts import Availability
from mesoforge.contracts.serialization import canonical_json_bytes, parse_canonical_json
from mesoforge.storage.json import CanonicalJsonSerializer
from tests.support.in_memory_uow import (
    InMemoryIdempotencyLock,
    InMemoryObjectStore,
    InMemoryUnitOfWorkFactory,
)

_STANDARD_SNAPSHOT_ID = "cfg_sha256_" + "a" * 64
_STANDARD_CONFIG_DIGEST = "sha256:" + "a" * 64


class _FakeSnapshot:
    def __init__(self, snapshot_id: str, digest: str) -> None:
        self.configuration_snapshot_id = snapshot_id
        self.configuration_digest = digest


def _availability(**overrides: object) -> Availability:
    kwargs: dict[str, object] = dict(
        available_at=datetime(2026, 1, 1, tzinfo=UTC),
        authority="mesoforge.synthetic",
        method="synthetic.v1",
    )
    kwargs.update(overrides)
    return Availability(**kwargs)


def _source_request(**overrides: object) -> SourceRegistrationRequest:
    kwargs: dict[str, object] = dict(
        source_authority="noaa.synthetic",
        source_locator="synthetic://source/1",
        source_revision="v1",
        artifact_type="synthetic-source",
        artifact_schema_version="synthetic-source.v1",
        media_type="application/octet-stream",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        availability=_availability(),
        configuration_snapshot_id=_STANDARD_SNAPSHOT_ID,
        configuration_digest=_STANDARD_CONFIG_DIGEST,
        code_revision="a" * 40,
        environment_digest="sha256:" + "b" * 64,
    )
    kwargs.update(overrides)
    return SourceRegistrationRequest(**kwargs)


@pytest.fixture()
def service_and_uow():
    object_store = InMemoryObjectStore()
    uow_factory = InMemoryUnitOfWorkFactory()
    lock = InMemoryIdempotencyLock()
    with uow_factory() as uow:
        uow.configurations.add_if_absent(
            _FakeSnapshot(_STANDARD_SNAPSHOT_ID, _STANDARD_CONFIG_DIGEST)
        )
        uow.commit()
    service = ArtifactService(
        unit_of_work_factory=uow_factory, object_store=object_store, idempotency_lock=lock
    )
    return service, uow_factory, object_store


def _transformation_request(inputs: tuple[TransformationInputRef, ...]) -> TransformationRequest:
    return TransformationRequest(
        activity_type="test-role-bound-merge",
        activity_version="v1",
        inputs=inputs,
        output_role="merged",
        output_artifact_type="synthetic-merged",
        output_artifact_schema_version="synthetic-merged.v1",
        output_media_type="application/json",
        configuration_snapshot_id=_STANDARD_SNAPSHOT_ID,
        configuration_digest=_STANDARD_CONFIG_DIGEST,
        code_revision="a" * 40,
        environment_digest="sha256:" + "b" * 64,
    )


def _raw_bytes_loader(payload: bytes) -> bytes:
    return payload


def _validate_any(_value: object) -> None:
    return None


class TestExecuteRoleBoundTransformation:
    def test_binding_cannot_omit_validation(self) -> None:
        with pytest.raises(TypeError, match="requires callable"):
            InputBinding(parse_canonical_json, None)  # type: ignore[arg-type]

    def test_merges_json_and_raw_inputs_by_role(self, service_and_uow) -> None:
        service, _uow_factory, _object_store = service_and_uow
        json_artifact = service.register_source(
            _source_request(source_locator="loc-json"),
            canonical_json_bytes({"station": "KCBG"}),
        )
        raw_artifact = service.register_source(
            _source_request(source_locator="loc-raw"), b"raw-grib-bytes"
        )

        request = _transformation_request(
            (
                TransformationInputRef(role="inventory", artifact_id=json_artifact.artifact_id),
                TransformationInputRef(role="grib", artifact_id=raw_artifact.artifact_id),
            )
        )

        def transform(inputs: object) -> dict[str, object]:
            inventory = inputs["inventory"]  # type: ignore[index]
            grib = inputs["grib"]  # type: ignore[index]
            return {"station": inventory["station"], "grib_length": len(grib)}

        result = service.execute_role_bound_transformation(
            request,
            transform,
            CanonicalJsonSerializer(),
            input_bindings={
                "inventory": InputBinding(parse_canonical_json, _validate_any),
                "grib": InputBinding(_raw_bytes_loader, _validate_any),
            },
            output_validator=lambda output: None,
        )

        assert result.output.artifact_type == "synthetic-merged"

    def test_validator_runs_per_role_before_transform(self, service_and_uow) -> None:
        service, _uow_factory, _object_store = service_and_uow
        json_artifact = service.register_source(
            _source_request(source_locator="loc-json-2"),
            canonical_json_bytes({"station": "KCBG"}),
        )
        request = _transformation_request(
            (TransformationInputRef(role="inventory", artifact_id=json_artifact.artifact_id),),
        )

        seen_calls: list[str] = []

        def failing_validator(value: object) -> None:
            seen_calls.append("validated")
            raise ValueError("synthetic validation failure")

        def transform(inputs: object) -> dict[str, object]:
            seen_calls.append("transformed")
            return {}

        with pytest.raises(ValueError, match="synthetic validation failure"):
            service.execute_role_bound_transformation(
                request,
                transform,
                CanonicalJsonSerializer(),
                input_bindings={"inventory": InputBinding(parse_canonical_json, failing_validator)},
                output_validator=lambda output: None,
            )

        assert seen_calls == ["validated"]

    def test_missing_role_binding_raises_before_any_io(self, service_and_uow) -> None:
        service, uow_factory, _object_store = service_and_uow
        json_artifact = service.register_source(
            _source_request(source_locator="loc-json-3"),
            canonical_json_bytes({"station": "KCBG"}),
        )
        request = _transformation_request(
            (TransformationInputRef(role="inventory", artifact_id=json_artifact.artifact_id),),
        )

        with pytest.raises(ValueError, match="input_bindings roles must equal"):
            service.execute_role_bound_transformation(
                request,
                lambda inputs: {},
                CanonicalJsonSerializer(),
                input_bindings={},
                output_validator=lambda output: None,
            )
        assert len(uow_factory.activities) == 0

    def test_extra_role_binding_raises(self, service_and_uow) -> None:
        service, _uow_factory, _object_store = service_and_uow
        json_artifact = service.register_source(
            _source_request(source_locator="loc-json-4"),
            canonical_json_bytes({"station": "KCBG"}),
        )
        request = _transformation_request(
            (TransformationInputRef(role="inventory", artifact_id=json_artifact.artifact_id),),
        )

        with pytest.raises(ValueError, match="input_bindings roles must equal"):
            service.execute_role_bound_transformation(
                request,
                lambda inputs: {},
                CanonicalJsonSerializer(),
                input_bindings={
                    "inventory": InputBinding(parse_canonical_json, _validate_any),
                    "unexpected": InputBinding(parse_canonical_json, _validate_any),
                },
                output_validator=lambda output: None,
            )

    def test_repeat_role_bound_request_reuses_succeeded_activity(self, service_and_uow) -> None:
        service, uow_factory, _object_store = service_and_uow
        json_artifact = service.register_source(
            _source_request(source_locator="loc-json-5"),
            canonical_json_bytes({"station": "KCBG"}),
        )
        request = _transformation_request(
            (TransformationInputRef(role="inventory", artifact_id=json_artifact.artifact_id),),
        )

        def transform(inputs: object) -> dict[str, object]:
            return {"station": inputs["inventory"]["station"]}  # type: ignore[index]

        bindings = {"inventory": InputBinding(parse_canonical_json, _validate_any)}
        first = service.execute_role_bound_transformation(
            request,
            transform,
            CanonicalJsonSerializer(),
            input_bindings=bindings,
            output_validator=lambda output: None,
        )
        second = service.execute_role_bound_transformation(
            request,
            transform,
            CanonicalJsonSerializer(),
            input_bindings=bindings,
            output_validator=lambda output: None,
        )
        assert first.output.artifact_id == second.output.artifact_id
        assert len(uow_factory.artifacts) == 2  # source + one derived output, not two

    def test_concurrent_identical_mixed_codec_requests_yield_one_output(
        self, service_and_uow
    ) -> None:
        import concurrent.futures

        service, uow_factory, _object_store = service_and_uow
        json_artifact = service.register_source(
            _source_request(source_locator="loc-json-6"),
            canonical_json_bytes({"station": "KCBG"}),
        )
        request = _transformation_request(
            (TransformationInputRef(role="inventory", artifact_id=json_artifact.artifact_id),),
        )
        bindings = {"inventory": InputBinding(parse_canonical_json, _validate_any)}

        def transform(inputs: object) -> dict[str, object]:
            return {"station": inputs["inventory"]["station"]}  # type: ignore[index]

        def run() -> str:
            result = service.execute_role_bound_transformation(
                request,
                transform,
                CanonicalJsonSerializer(),
                input_bindings=bindings,
                output_validator=lambda output: None,
            )
            return result.output.artifact_id

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            artifact_ids = list(executor.map(lambda _: run(), range(8)))

        assert len(set(artifact_ids)) == 1
        succeeded = [a for a in uow_factory.activities.values() if a.status == "succeeded"]
        assert len(succeeded) == 1


class TestExecuteTransformationBackwardCompatible:
    """The existing dataset-shaped API must be unaffected by the
    role-bound refactor (plan Section 4.4: 'existing execute_transformation
    and execute_raw_transformation delegate to the same private core and
    remain backward compatible')."""

    def test_execute_raw_transformation_still_works(self, service_and_uow) -> None:
        service, _uow_factory, _object_store = service_and_uow
        source = service.register_source(
            _source_request(source_locator="loc-raw-compat"), b"payload"
        )
        request = _transformation_request(
            (TransformationInputRef(role="only", artifact_id=source.artifact_id),),
        )

        def transform(payload: bytes) -> dict[str, object]:
            return {"length": len(payload)}

        result = service.execute_raw_transformation(
            request,
            transform,
            CanonicalJsonSerializer(),
            input_loader=_raw_bytes_loader,
            output_validator=lambda output: None,
        )
        assert result.output.artifact_type == "synthetic-merged"
