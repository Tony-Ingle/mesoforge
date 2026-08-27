"""Unit tests: public request boundaries reject malformed IDs/digests
(plan Section 4.1/5; Codex review t_f569c45c finding 3).

``SourceRegistrationRequest``, ``TransformationInputRef``,
``TransformationRequest``, and ``ArtifactService.create_run`` must use
the typed ``common.identifiers`` types (``ArtifactId``, ``RunId``,
``ConfigurationSnapshotId``, ``Digest``) and a validated
``code_revision``, not an unrestricted ``str`` -- a malformed value must
be rejected at construction/call time, before it ever reaches the
service body or a repository.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mesoforge.application.artifacts import (
    ArtifactService,
    SourceRegistrationRequest,
    TransformationInputRef,
    TransformationRequest,
)
from mesoforge.common.errors import InvalidIdentifier
from mesoforge.contracts.artifacts import Availability
from tests.support.in_memory_uow import (
    InMemoryIdempotencyLock,
    InMemoryObjectStore,
    InMemoryUnitOfWorkFactory,
)

_VALID_SNAPSHOT_ID = "cfg_sha256_" + "a" * 64
_VALID_CONFIG_DIGEST = "sha256:" + "a" * 64
_VALID_ENV_DIGEST = "sha256:" + "b" * 64
_VALID_CODE_REVISION = "a" * 40


def _source_kwargs(**overrides: object) -> dict[str, object]:
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
        configuration_snapshot_id=_VALID_SNAPSHOT_ID,
        configuration_digest=_VALID_CONFIG_DIGEST,
        code_revision=_VALID_CODE_REVISION,
        environment_digest=_VALID_ENV_DIGEST,
    )
    kwargs.update(overrides)
    return kwargs


class TestSourceRegistrationRequestTypedBoundary:
    def test_valid_request_round_trips(self) -> None:
        request = SourceRegistrationRequest(**_source_kwargs())
        assert request.configuration_snapshot_id == _VALID_SNAPSHOT_ID
        assert request.configuration_digest == _VALID_CONFIG_DIGEST

    def test_rejects_malformed_configuration_snapshot_id(self) -> None:
        with pytest.raises(ValidationError):
            SourceRegistrationRequest(**_source_kwargs(configuration_snapshot_id="not-cfg"))

    def test_rejects_malformed_configuration_digest(self) -> None:
        with pytest.raises(ValidationError):
            SourceRegistrationRequest(**_source_kwargs(configuration_digest="md5:bad"))

    def test_rejects_malformed_environment_digest(self) -> None:
        with pytest.raises(ValidationError):
            SourceRegistrationRequest(**_source_kwargs(environment_digest="not-a-digest"))

    def test_rejects_malformed_run_id(self) -> None:
        with pytest.raises(ValidationError):
            SourceRegistrationRequest(**_source_kwargs(run_id="not-prefixed"))

    def test_rejects_malformed_expected_content_digest(self) -> None:
        with pytest.raises(ValidationError):
            SourceRegistrationRequest(**_source_kwargs(expected_content_digest="sha1:" + "a" * 40))

    def test_rejects_malformed_code_revision(self) -> None:
        with pytest.raises(ValidationError):
            SourceRegistrationRequest(**_source_kwargs(code_revision="not-a-revision"))


class TestTransformationRequestTypedBoundary:
    def _kwargs(self, **overrides: object) -> dict[str, object]:
        kwargs: dict[str, object] = dict(
            activity_type="unit-conversion",
            activity_version="1.0.0",
            inputs=(
                TransformationInputRef(
                    role="primary",
                    artifact_id="art_00000000-0000-0000-0000-000000000001",
                ),
            ),
            output_role="primary",
            output_artifact_type="synthetic-derived",
            output_artifact_schema_version="synthetic-derived.v1",
            output_media_type="application/octet-stream",
            configuration_snapshot_id=_VALID_SNAPSHOT_ID,
            configuration_digest=_VALID_CONFIG_DIGEST,
            code_revision=_VALID_CODE_REVISION,
            environment_digest=_VALID_ENV_DIGEST,
        )
        kwargs.update(overrides)
        return kwargs

    def test_valid_request_round_trips(self) -> None:
        request = TransformationRequest(**self._kwargs())
        assert request.configuration_snapshot_id == _VALID_SNAPSHOT_ID

    def test_rejects_malformed_artifact_id_in_input_ref(self) -> None:
        with pytest.raises(ValidationError):
            TransformationInputRef(role="primary", artifact_id="not-prefixed")

    def test_rejects_malformed_configuration_snapshot_id(self) -> None:
        with pytest.raises(ValidationError):
            TransformationRequest(**self._kwargs(configuration_snapshot_id="not-cfg"))

    def test_rejects_malformed_configuration_digest(self) -> None:
        with pytest.raises(ValidationError):
            TransformationRequest(**self._kwargs(configuration_digest="md5:bad"))

    def test_rejects_malformed_environment_digest(self) -> None:
        with pytest.raises(ValidationError):
            TransformationRequest(**self._kwargs(environment_digest="not-a-digest"))

    def test_rejects_malformed_run_id(self) -> None:
        with pytest.raises(ValidationError):
            TransformationRequest(**self._kwargs(run_id="not-prefixed"))

    def test_rejects_malformed_code_revision(self) -> None:
        with pytest.raises(ValidationError):
            TransformationRequest(**self._kwargs(code_revision="short"))


class TestCreateRunTypedBoundary:
    """ArtifactService.create_run must reject malformed IDs/digests
    before ever calling the repository (Codex review t_f569c45c finding
    3)."""

    @pytest.fixture()
    def service(self) -> ArtifactService:
        return ArtifactService(
            unit_of_work_factory=InMemoryUnitOfWorkFactory(),
            object_store=InMemoryObjectStore(),
            idempotency_lock=InMemoryIdempotencyLock(),
        )

    def _kwargs(self, **overrides: object) -> dict[str, object]:
        kwargs: dict[str, object] = dict(
            run_id="run_00000000-0000-0000-0000-000000000001",
            forecast_issue_time=datetime(2026, 1, 1, tzinfo=UTC),
            information_cutoff=datetime(2026, 1, 1, 6, tzinfo=UTC),
            configuration_snapshot_id=_VALID_SNAPSHOT_ID,
            configuration_digest=_VALID_CONFIG_DIGEST,
            code_revision=_VALID_CODE_REVISION,
            environment_digest=_VALID_ENV_DIGEST,
            lockfile_digest="sha256:" + "d" * 64,
            random_seed=1,
            selected_input_artifact_ids=(),
        )
        kwargs.update(overrides)
        return kwargs

    def test_rejects_malformed_run_id(self, service: ArtifactService) -> None:
        with pytest.raises(InvalidIdentifier):
            service.create_run(**self._kwargs(run_id="not-prefixed"))

    def test_rejects_malformed_configuration_snapshot_id(self, service: ArtifactService) -> None:
        with pytest.raises(InvalidIdentifier):
            service.create_run(**self._kwargs(configuration_snapshot_id="not-cfg"))

    def test_rejects_malformed_configuration_digest(self, service: ArtifactService) -> None:
        with pytest.raises(InvalidIdentifier):
            service.create_run(**self._kwargs(configuration_digest="md5:bad"))

    def test_rejects_malformed_code_revision(self, service: ArtifactService) -> None:
        with pytest.raises(InvalidIdentifier):
            service.create_run(**self._kwargs(code_revision="not-a-revision"))

    def test_rejects_malformed_environment_digest(self, service: ArtifactService) -> None:
        with pytest.raises(InvalidIdentifier):
            service.create_run(**self._kwargs(environment_digest="not-a-digest"))

    def test_rejects_malformed_lockfile_digest(self, service: ArtifactService) -> None:
        with pytest.raises(InvalidIdentifier):
            service.create_run(**self._kwargs(lockfile_digest="not-a-digest"))

    def test_rejects_malformed_selected_input_artifact_id(self, service: ArtifactService) -> None:
        with pytest.raises(InvalidIdentifier):
            service.create_run(**self._kwargs(selected_input_artifact_ids=("not-prefixed",)))
