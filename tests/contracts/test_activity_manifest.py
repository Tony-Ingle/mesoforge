"""Contract tests for ActivityManifest (Task 7, plan Section 4.8).

RED: written before src/mesoforge/contracts/provenance.py exists.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mesoforge.contracts.provenance import ActivityArtifactRef, ActivityManifest

_VALID_SHA = "a" * 40
_DIGEST = "sha256:" + "a" * 64
_ART_ID_1 = "art_00000000-0000-0000-0000-000000000001"
_ART_ID_2 = "art_00000000-0000-0000-0000-000000000002"


def _ref(role: str, artifact_id: str) -> ActivityArtifactRef:
    return ActivityArtifactRef(role=role, artifact_id=artifact_id)


def _base_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = dict(
        activity_id="act_00000000-0000-0000-0000-000000000001",
        activity_type="unit-conversion",
        activity_version="1.0.0",
        status="started",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        idempotency_digest=_DIGEST,
        parameters_digest=_DIGEST,
        configuration_snapshot_id="cfg_sha256_" + "a" * 64,
        configuration_digest=_DIGEST,
        code_revision=_VALID_SHA,
        environment_digest=_DIGEST,
        inputs=(),
        outputs=(),
    )
    kwargs.update(overrides)
    return kwargs


class TestActivityManifestStatusInvariants:
    def test_started_has_no_completion_outputs_or_error(self) -> None:
        manifest = ActivityManifest(**_base_kwargs())
        assert manifest.completed_at is None
        assert manifest.outputs == ()
        assert manifest.error is None

    def test_started_with_completed_at_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ActivityManifest(**_base_kwargs(completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC)))

    def test_succeeded_requires_completion_and_io(self) -> None:
        manifest = ActivityManifest(
            **_base_kwargs(
                status="succeeded",
                completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
                inputs=(_ref("primary", _ART_ID_1),),
                outputs=(_ref("primary", _ART_ID_2),),
            )
        )
        assert manifest.status == "succeeded"

    def test_succeeded_without_completed_at_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ActivityManifest(
                **_base_kwargs(
                    status="succeeded",
                    inputs=(_ref("primary", _ART_ID_1),),
                    outputs=(_ref("primary", _ART_ID_2),),
                )
            )

    def test_succeeded_without_any_io_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ActivityManifest(
                **_base_kwargs(status="succeeded", completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC))
            )

    def test_succeeded_with_error_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ActivityManifest(
                **_base_kwargs(
                    status="succeeded",
                    completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
                    inputs=(_ref("primary", _ART_ID_1),),
                    outputs=(_ref("primary", _ART_ID_2),),
                    error={"error_type": "x", "message_digest": _DIGEST, "retryable": False},
                )
            )

    def test_failed_requires_completion_and_error_no_outputs(self) -> None:
        manifest = ActivityManifest(
            **_base_kwargs(
                status="failed",
                completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
                inputs=(_ref("primary", _ART_ID_1),),
                error={"error_type": "x", "message_digest": _DIGEST, "retryable": False},
            )
        )
        assert manifest.status == "failed"

    def test_failed_with_outputs_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ActivityManifest(
                **_base_kwargs(
                    status="failed",
                    completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
                    outputs=(_ref("primary", _ART_ID_2),),
                    error={"error_type": "x", "message_digest": _DIGEST, "retryable": False},
                )
            )

    def test_failed_without_error_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ActivityManifest(
                **_base_kwargs(status="failed", completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC))
            )


class TestActivityArtifactRefRoles:
    def test_duplicate_input_roles_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ActivityManifest(
                **_base_kwargs(
                    status="succeeded",
                    completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
                    inputs=(_ref("primary", _ART_ID_1), _ref("primary", _ART_ID_2)),
                    outputs=(_ref("primary", _ART_ID_2),),
                )
            )

    def test_duplicate_output_roles_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ActivityManifest(
                **_base_kwargs(
                    status="succeeded",
                    completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
                    inputs=(_ref("primary", _ART_ID_1),),
                    outputs=(_ref("primary", _ART_ID_2), _ref("primary", _ART_ID_1)),
                )
            )

    def test_frozen(self) -> None:
        manifest = ActivityManifest(**_base_kwargs())
        with pytest.raises(ValidationError):
            manifest.status = "succeeded"  # type: ignore[misc]
