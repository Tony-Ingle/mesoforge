"""Unit tests for ArtifactService using behaviorally complete in-memory
test doubles (plan Section 4.9/5, Task 10).

RED: written before src/mesoforge/application/artifacts.py exists
(module now exists; this file drives the ordering/error-branch tests
listed in the plan).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from mesoforge.application.artifacts import (
    ArtifactService,
    SourceRegistrationRequest,
    TransformationInputRef,
    TransformationRequest,
)
from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.contracts.artifacts import Availability
from tests.support.in_memory_uow import (
    InMemoryIdempotencyLock,
    InMemoryObjectStore,
    InMemoryUnitOfWorkFactory,
)


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
        configuration_snapshot_id="cfg_sha256_" + "a" * 64,
        configuration_digest="sha256:" + "a" * 64,
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
    service = ArtifactService(
        unit_of_work_factory=uow_factory, object_store=object_store, idempotency_lock=lock
    )
    return service, uow_factory, object_store


class TestRegisterSource:
    def test_successful_registration_round_trips_identity(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        request = _source_request()
        manifest = service.register_source(request, b"payload-bytes")

        assert manifest.source_identity is not None
        assert manifest.source_identity.authority == request.source_authority
        assert manifest.source_identity.locator == request.source_locator
        assert manifest.source_identity.revision == request.source_revision
        assert manifest.source_registration_digest is not None

    def test_repeat_request_returns_original_artifact(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        request = _source_request()
        first = service.register_source(request, b"payload-bytes")
        second = service.register_source(request, b"payload-bytes")
        assert first.artifact_id == second.artifact_id
        assert len(uow_factory.artifacts) == 1

    def test_wrong_expected_checksum_raises_and_creates_no_manifest(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        request = _source_request(expected_content_digest="sha256:" + "0" * 64)
        with pytest.raises(IntegrityError):
            service.register_source(request, b"payload-bytes")
        assert len(uow_factory.artifacts) == 0

    def test_duplicate_content_distinct_provenance_creates_two_artifacts(
        self, service_and_uow
    ) -> None:
        service, uow_factory, object_store = service_and_uow
        payload = b"shared-bytes"
        first = service.register_source(_source_request(source_locator="loc-a"), payload)
        second = service.register_source(_source_request(source_locator="loc-b"), payload)

        assert first.artifact_id != second.artifact_id
        assert first.content_digest == second.content_digest
        assert len(object_store.objects) == 1  # bytes deduplicated

    def test_upload_failure_creates_no_manifest(self, service_and_uow) -> None:
        service, uow_factory, object_store = service_and_uow
        object_store.fail_next_put = True
        request = _source_request(source_locator="loc-fail")
        with pytest.raises(RuntimeError):
            service.register_source(request, b"payload-bytes")
        assert len(uow_factory.artifacts) == 0


class TestExecuteTransformation:
    def _transformation_request(self, **overrides: object) -> TransformationRequest:
        kwargs: dict[str, object] = dict(
            activity_type="unit-conversion",
            activity_version="1.0.0",
            inputs=(TransformationInputRef(role="primary", artifact_id="art_placeholder"),),
            output_role="primary",
            output_artifact_type="synthetic-derived",
            output_artifact_schema_version="synthetic-derived.v1",
            output_media_type="application/octet-stream",
            configuration_snapshot_id="cfg_sha256_" + "a" * 64,
            configuration_digest="sha256:" + "a" * 64,
            code_revision="a" * 40,
            environment_digest="sha256:" + "b" * 64,
        )
        kwargs.update(overrides)
        return TransformationRequest(**kwargs)

    def test_successful_transformation(self, service_and_uow) -> None:
        service, uow_factory, object_store = service_and_uow
        source = service.register_source(_source_request(), b"10.0")

        request = self._transformation_request(
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),)
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        result = service.execute_transformation(
            request,
            transform=lambda data: str(float(data) + 273.15).encode(),
            serializer=_Serializer(),
            input_loader=lambda payload: payload,
            output_validator=lambda _output: None,
        )
        assert result.activity.status == "succeeded"
        assert result.output.artifact_type == "synthetic-derived"

    def test_repeat_identical_request_returns_same_output(self, service_and_uow) -> None:
        service, uow_factory, object_store = service_and_uow
        source = service.register_source(_source_request(), b"10.0")
        request = self._transformation_request(
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),)
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        first = service.execute_transformation(
            request,
            transform=lambda data: data,
            serializer=_Serializer(),
            input_loader=lambda payload: payload,
            output_validator=lambda _output: None,
        )
        second = service.execute_transformation(
            request,
            transform=lambda data: data,
            serializer=_Serializer(),
            input_loader=lambda payload: payload,
            output_validator=lambda _output: None,
        )
        assert first.output.artifact_id == second.output.artifact_id
        succeeded_activities = [
            a for a in uow_factory.activities.values() if a.status == "succeeded"
        ]
        assert len(succeeded_activities) == 1

    def test_input_checksum_mismatch_raises(self, service_and_uow) -> None:
        service, uow_factory, object_store = service_and_uow
        source = service.register_source(_source_request(), b"10.0")
        object_store.corrupt_next_get = True
        request = self._transformation_request(
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),)
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        with pytest.raises(IntegrityError):
            service.execute_transformation(
                request,
                transform=lambda data: data,
                serializer=_Serializer(),
                input_loader=lambda payload: payload,
                output_validator=lambda _output: None,
            )

    def test_transform_exception_marks_activity_failed_with_no_outputs(
        self, service_and_uow
    ) -> None:
        service, uow_factory, _ = service_and_uow
        source = service.register_source(_source_request(), b"10.0")
        request = self._transformation_request(
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),)
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        def _boom(_data: bytes) -> bytes:
            raise ValueError("transform exploded")

        with pytest.raises(ValueError, match="transform exploded"):
            service.execute_transformation(
                request,
                transform=_boom,
                serializer=_Serializer(),
                input_loader=lambda payload: payload,
                output_validator=lambda _output: None,
            )

        failed = [a for a in uow_factory.activities.values() if a.status == "failed"]
        assert len(failed) == 1
        assert failed[0].outputs == ()
        assert failed[0].error is not None

    def test_output_contract_violation_marks_activity_failed(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        source = service.register_source(_source_request(), b"10.0")
        request = self._transformation_request(
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),)
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        def _reject(_output: bytes) -> None:
            raise ValueError("contract violated")

        with pytest.raises(ValueError, match="contract violated"):
            service.execute_transformation(
                request,
                transform=lambda data: data,
                serializer=_Serializer(),
                input_loader=lambda payload: payload,
                output_validator=_reject,
            )

        failed = [a for a in uow_factory.activities.values() if a.status == "failed"]
        assert len(failed) == 1

    def test_dangling_input_raises_not_found(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        request = self._transformation_request(
            inputs=(
                TransformationInputRef(
                    role="primary", artifact_id="art_00000000-0000-0000-0000-000000000099"
                ),
            )
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        with pytest.raises(NotFound):
            service.execute_transformation(
                request,
                transform=lambda data: data,
                serializer=_Serializer(),
                input_loader=lambda payload: payload,
                output_validator=lambda _output: None,
            )

    def test_derived_availability_not_earlier_than_parent(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        far_future = datetime(2030, 1, 1, tzinfo=UTC)
        source = service.register_source(
            _source_request(availability=_availability(available_at=far_future)), b"10.0"
        )
        request = self._transformation_request(
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),)
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        result = service.execute_transformation(
            request,
            transform=lambda data: data,
            serializer=_Serializer(),
            input_loader=lambda payload: payload,
            output_validator=lambda _output: None,
        )
        assert result.output.availability.available_at >= far_future

    def test_concurrent_duplicate_requests_produce_one_activity(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        source = service.register_source(_source_request(), b"10.0")
        request = self._transformation_request(
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),)
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        results: list[object] = []
        errors: list[Exception] = []

        def _run() -> None:
            try:
                results.append(
                    service.execute_transformation(
                        request,
                        transform=lambda data: data,
                        serializer=_Serializer(),
                        input_loader=lambda payload: payload,
                        output_validator=lambda _output: None,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)

        threads = [threading.Thread(target=_run) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors
        output_ids = {r.output.artifact_id for r in results}  # type: ignore[attr-defined]
        assert len(output_ids) == 1
        succeeded = [a for a in uow_factory.activities.values() if a.status == "succeeded"]
        assert len(succeeded) == 1
