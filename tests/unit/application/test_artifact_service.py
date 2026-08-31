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
from mesoforge.common.errors import Conflict, IntegrityError, NotFound
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


_STANDARD_SNAPSHOT_ID = "cfg_sha256_" + "a" * 64
_STANDARD_CONFIG_DIGEST = "sha256:" + "a" * 64


@pytest.fixture()
def service_and_uow():
    object_store = InMemoryObjectStore()
    uow_factory = InMemoryUnitOfWorkFactory()
    lock = InMemoryIdempotencyLock()
    # Pre-register the standard configuration snapshot most tests
    # reference (configuration_snapshot_id="cfg_sha256_"+"a"*64), so
    # register_source/execute_transformation's configuration
    # snapshot/digest consistency check (Codex review t_9bb13e2b
    # finding 8) passes for the common case; tests that specifically
    # exercise a tampered/unregistered snapshot register their own.
    with uow_factory() as uow:
        uow.configurations.add_if_absent(
            _FakeSnapshot(_STANDARD_SNAPSHOT_ID, _STANDARD_CONFIG_DIGEST)
        )
        uow.commit()
    service = ArtifactService(
        unit_of_work_factory=uow_factory, object_store=object_store, idempotency_lock=lock
    )
    return service, uow_factory, object_store


class _FakeSnapshot:
    def __init__(self, snapshot_id: str, digest: str) -> None:
        self.configuration_snapshot_id = snapshot_id
        self.configuration_digest = digest


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

    def test_tampered_configuration_digest_rejected(self, service_and_uow) -> None:
        """Finding 8 (Codex review t_9bb13e2b): register_source must
        validate referenced configuration snapshot/digest consistency."""
        service, uow_factory, _ = service_and_uow
        request = _source_request(configuration_digest="sha256:" + "f" * 64)
        with pytest.raises(ValueError, match="does not match"):
            service.register_source(request, b"payload-bytes")
        assert len(uow_factory.artifacts) == 0

    def test_unregistered_configuration_snapshot_raises_not_found(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        request = _source_request(configuration_snapshot_id="cfg_sha256_" + "9" * 64)
        with pytest.raises(NotFound):
            service.register_source(request, b"payload-bytes")
        assert len(uow_factory.artifacts) == 0


class TestExecuteTransformation:
    def _transformation_request(self, **overrides: object) -> TransformationRequest:
        kwargs: dict[str, object] = dict(
            activity_type="unit-conversion",
            activity_version="1.0.0",
            inputs=(
                TransformationInputRef(
                    role="primary", artifact_id="art_00000000-0000-0000-0000-000000000000"
                ),
            ),
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

        result = service.execute_raw_transformation(
            request,
            transform=lambda data: str(float(data) + 273.15).encode(),
            serializer=_Serializer(),
            input_loader=lambda payload: payload,
            output_validator=lambda _output: None,
        )
        assert result.activity.status == "succeeded"
        assert result.output.artifact_type == "synthetic-derived"

    def test_atomic_pair_registers_both_outputs_on_one_activity(self, service_and_uow) -> None:
        service, _, _ = service_and_uow
        source = service.register_source(_source_request(), b"input")
        common = dict(
            activity_type="atomic-pair",
            activity_version="1.0.0",
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),),
            output_media_type="application/octet-stream",
            configuration_snapshot_id=_STANDARD_SNAPSHOT_ID,
            configuration_digest=_STANDARD_CONFIG_DIGEST,
            code_revision="a" * 40,
            environment_digest="sha256:" + "b" * 64,
        )
        left = TransformationRequest(
            **common,
            output_role="forecast",
            output_artifact_type="uncorrected-blend-forecast",
            output_artifact_schema_version="uncorrected-blend-forecast.v1",
        )
        right = TransformationRequest(
            **common,
            output_role="contributions",
            output_artifact_type="blend-contribution-manifest",
            output_artifact_schema_version="blend-contribution-manifest.v1",
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        first = service.execute_atomic_raw_pair(
            left,
            right,
            transform=lambda value: (value + b"-forecast", value + b"-contributions"),
            serializers=(_Serializer(), _Serializer()),
            input_loader=lambda payload: payload,
            output_validators=(lambda _: None, lambda _: None),
        )
        second = service.execute_atomic_raw_pair(
            left,
            right,
            transform=lambda value: (value + b"-forecast", value + b"-contributions"),
            serializers=(_Serializer(), _Serializer()),
            input_loader=lambda payload: payload,
            output_validators=(lambda _: None, lambda _: None),
        )
        assert first.activity.outputs == second.activity.outputs
        assert first.outputs == second.outputs
        assert {ref.role for ref in first.activity.outputs} == {"forecast", "contributions"}
        assert tuple(output.artifact_type for output in first.outputs) == (
            "uncorrected-blend-forecast",
            "blend-contribution-manifest",
        )

    def test_repeat_identical_request_returns_same_output(self, service_and_uow) -> None:
        service, uow_factory, object_store = service_and_uow
        source = service.register_source(_source_request(), b"10.0")
        request = self._transformation_request(
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),)
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        first = service.execute_raw_transformation(
            request,
            transform=lambda data: data,
            serializer=_Serializer(),
            input_loader=lambda payload: payload,
            output_validator=lambda _output: None,
        )
        second = service.execute_raw_transformation(
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
            service.execute_raw_transformation(
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
            service.execute_raw_transformation(
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
            service.execute_raw_transformation(
                request,
                transform=lambda data: data,
                serializer=_Serializer(),
                input_loader=lambda payload: payload,
                output_validator=_reject,
            )

        failed = [a for a in uow_factory.activities.values() if a.status == "failed"]
        assert len(failed) == 1

    def test_tampered_configuration_digest_rejected(self, service_and_uow) -> None:
        """Finding 8 (Codex review t_9bb13e2b): execute_transformation
        must validate referenced configuration snapshot/digest
        consistency."""
        service, uow_factory, _ = service_and_uow
        source = service.register_source(_source_request(), b"10.0")
        request = self._transformation_request(
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),),
            configuration_digest="sha256:" + "f" * 64,
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        with pytest.raises(ValueError, match="does not match"):
            service.execute_raw_transformation(
                request,
                transform=lambda data: data,
                serializer=_Serializer(),
                input_loader=lambda payload: payload,
                output_validator=lambda _output: None,
            )
        assert all(a.status != "succeeded" for a in uow_factory.activities.values())

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
            service.execute_raw_transformation(
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

        result = service.execute_raw_transformation(
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
                    service.execute_raw_transformation(
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


class TestCreateRun:
    """Finding 5 (Codex review t_9bb13e2b): run creation must load
    selected artifacts transactionally from the repository, reject
    nonexistent/derived inputs where source inputs are required, validate
    authoritative availability against cutoff, and validate configuration
    snapshot/digest consistency."""

    def _register_config(self, uow_factory, *, snapshot_id: str, digest: str) -> None:
        with uow_factory() as uow:
            uow.configurations.add_if_absent(_FakeSnapshot(snapshot_id, digest))
            uow.commit()

    def _run_kwargs(self, **overrides: object) -> dict[str, object]:
        kwargs: dict[str, object] = dict(
            run_id="run_00000000-0000-0000-0000-000000000001",
            forecast_issue_time=datetime(2026, 1, 1, tzinfo=UTC),
            information_cutoff=datetime(2026, 1, 1, 6, tzinfo=UTC),
            configuration_snapshot_id="cfg_sha256_" + "a" * 64,
            configuration_digest="sha256:" + "a" * 64,
            code_revision="a" * 40,
            environment_digest="sha256:" + "b" * 64,
            lockfile_digest="sha256:" + "d" * 64,
            random_seed=1,
            selected_input_artifact_ids=(),
        )
        kwargs.update(overrides)
        return kwargs

    def test_nonexistent_input_id_raises_not_found_and_creates_no_run(
        self, service_and_uow
    ) -> None:
        service, uow_factory, _ = service_and_uow
        self._register_config(
            uow_factory,
            snapshot_id="cfg_sha256_" + "a" * 64,
            digest="sha256:" + "a" * 64,
        )
        with pytest.raises(NotFound):
            service.create_run(
                **self._run_kwargs(
                    selected_input_artifact_ids=("art_00000000-0000-0000-0000-000000000099",)
                )
            )
        assert len(uow_factory.runs) == 0

    def test_derived_input_rejected_when_source_inputs_required(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        self._register_config(
            uow_factory,
            snapshot_id="cfg_sha256_" + "a" * 64,
            digest="sha256:" + "a" * 64,
        )
        # Build a derived (non-source) artifact directly via the
        # transformation path so source_registration_digest is None.
        source = service.register_source(_source_request(), b"10.0")
        request = TestExecuteTransformation()._transformation_request(
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),)
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        derived_result = service.execute_raw_transformation(
            request,
            transform=lambda data: data,
            serializer=_Serializer(),
            input_loader=lambda payload: payload,
            output_validator=lambda _output: None,
        )

        with pytest.raises(ValueError, match="not a source/root artifact"):
            service.create_run(
                **self._run_kwargs(selected_input_artifact_ids=(derived_result.output.artifact_id,))
            )
        assert len(uow_factory.runs) == 0

    def test_cutoff_violation_rejected_and_creates_no_run(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        self._register_config(
            uow_factory,
            snapshot_id="cfg_sha256_" + "a" * 64,
            digest="sha256:" + "a" * 64,
        )
        far_future = datetime(2030, 1, 1, tzinfo=UTC)
        source = service.register_source(
            _source_request(availability=_availability(available_at=far_future)), b"10.0"
        )

        with pytest.raises(ValueError, match="fails closed"):
            service.create_run(
                **self._run_kwargs(
                    information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
                    selected_input_artifact_ids=(source.artifact_id,),
                )
            )
        assert len(uow_factory.runs) == 0

    def test_tampered_configuration_digest_rejected(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        self._register_config(
            uow_factory,
            snapshot_id="cfg_sha256_" + "a" * 64,
            digest="sha256:" + "a" * 64,
        )

        with pytest.raises(ValueError, match="does not match"):
            service.create_run(**self._run_kwargs(configuration_digest="sha256:" + "f" * 64))
        assert len(uow_factory.runs) == 0

    def test_valid_run_creation_persists_ordered_selection(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        self._register_config(
            uow_factory,
            snapshot_id="cfg_sha256_" + "a" * 64,
            digest="sha256:" + "a" * 64,
        )
        source_a = service.register_source(_source_request(source_locator="loc-a"), b"a-bytes")
        source_b = service.register_source(_source_request(source_locator="loc-b"), b"b-bytes")

        created = service.create_run(
            **self._run_kwargs(
                selected_input_artifact_ids=(source_a.artifact_id, source_b.artifact_id)
            )
        )
        assert created.selected_input_artifact_ids == (
            source_a.artifact_id,
            source_b.artifact_id,
        )
        assert len(uow_factory.runs) == 1

    def test_identical_run_creation_is_idempotent(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        self._register_config(
            uow_factory,
            snapshot_id="cfg_sha256_" + "a" * 64,
            digest="sha256:" + "a" * 64,
        )
        source = service.register_source(_source_request(), b"source")
        kwargs = self._run_kwargs(selected_input_artifact_ids=(source.artifact_id,))
        first = service.create_run(**kwargs)
        second = service.create_run(**kwargs)
        assert second == first
        assert len(uow_factory.runs) == 1

    def test_reused_run_id_with_changed_identity_fails_closed(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        self._register_config(
            uow_factory,
            snapshot_id="cfg_sha256_" + "a" * 64,
            digest="sha256:" + "a" * 64,
        )
        service.create_run(**self._run_kwargs())
        with pytest.raises(Conflict, match="different immutable identity"):
            service.create_run(**self._run_kwargs(random_seed=2))
        assert len(uow_factory.runs) == 1


class TestCanonicalTransformationValidationMandatory:
    """Finding 1 (Codex review t_f569c45c): the canonical
    ``execute_transformation`` API must not allow callers to omit
    scientific input validation. ``input_validator`` is a required
    keyword argument with no default -- a caller that tries to omit it
    gets a ``TypeError`` from Python itself, before the service ever
    runs. Callers that genuinely transform non-dataset payloads must use
    the differently-named ``execute_raw_transformation`` instead."""

    def _transformation_request(self, **overrides: object) -> TransformationRequest:
        kwargs: dict[str, object] = dict(
            activity_type="unit-conversion",
            activity_version="1.0.0",
            inputs=(
                TransformationInputRef(
                    role="primary", artifact_id="art_00000000-0000-0000-0000-000000000000"
                ),
            ),
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

    def test_execute_transformation_requires_input_validator_keyword(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        source = service.register_source(_source_request(), b"10.0")
        request = self._transformation_request(
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),)
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        with pytest.raises(TypeError):
            # input_validator intentionally omitted: this must fail at
            # the call site (missing required keyword argument), never
            # silently skip scientific validation.
            service.execute_transformation(  # type: ignore[call-arg]
                request,
                transform=lambda data: data,
                serializer=_Serializer(),
                input_loader=lambda payload: payload,
                output_validator=lambda _output: None,
            )

    def test_execute_transformation_runs_supplied_input_validator(self, service_and_uow) -> None:
        service, uow_factory, _ = service_and_uow
        source = service.register_source(_source_request(), b"10.0")
        request = self._transformation_request(
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),)
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        validated_payloads: list[bytes] = []

        def _reject_all(dataset: bytes) -> None:
            validated_payloads.append(dataset)
            raise ValueError("malformed scientific dataset")

        with pytest.raises(ValueError, match="malformed scientific dataset"):
            service.execute_transformation(
                request,
                transform=lambda data: data,
                serializer=_Serializer(),
                input_loader=lambda payload: payload,
                output_validator=lambda _output: None,
                input_validator=_reject_all,
            )
        # the validator ran (and rejected) before transform ever could;
        # since validation happens before the started activity is even
        # created (step 4 precedes step 5), no activity row exists at
        # all for the rejected request.
        assert validated_payloads == [b"10.0"]
        assert len(uow_factory.activities) == 0

    def test_execute_raw_transformation_has_no_input_validator_parameter(
        self, service_and_uow
    ) -> None:
        """execute_raw_transformation's signature has no input_validator
        parameter at all -- passing one is a TypeError, proving the two
        APIs cannot be confused with each other."""
        service, uow_factory, _ = service_and_uow
        source = service.register_source(_source_request(), b"10.0")
        request = self._transformation_request(
            inputs=(TransformationInputRef(role="primary", artifact_id=source.artifact_id),)
        )

        class _Serializer:
            def serialize(self, value: bytes) -> bytes:
                return value

        with pytest.raises(TypeError):
            service.execute_raw_transformation(  # type: ignore[call-arg]
                request,
                transform=lambda data: data,
                serializer=_Serializer(),
                input_loader=lambda payload: payload,
                output_validator=lambda _output: None,
                input_validator=lambda _dataset: None,
            )
