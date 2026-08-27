"""Activity manifest contract (plan Section 4.8).

Status-shape invariants (started/succeeded/failed) and input/output role
uniqueness are enforced here. Lineage graph construction from persisted
activity edges lives in ``provenance/lineage.py``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.common.identifiers import (
    ActivityId,
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    RunId,
    validate_code_revision,
)
from mesoforge.common.time import UtcInstant


class ActivityArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: str
    artifact_id: ArtifactId


class ActivityError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    error_type: str
    message_digest: Digest
    retryable: bool


def _check_unique_roles(refs: tuple[ActivityArtifactRef, ...], label: str) -> None:
    seen: set[str] = set()
    for ref in refs:
        if ref.role in seen:
            raise ValueError(f"duplicate {label} role {ref.role!r}")
        seen.add(ref.role)


class ActivityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["activity-manifest.v1"] = "activity-manifest.v1"
    activity_id: ActivityId
    activity_type: str
    activity_version: str
    status: str
    started_at: UtcInstant
    completed_at: UtcInstant | None = None
    idempotency_digest: Digest
    parameters_digest: Digest
    configuration_snapshot_id: ConfigurationSnapshotId
    configuration_digest: Digest
    code_revision: str
    environment_digest: Digest
    run_id: RunId | None = None
    inputs: tuple[ActivityArtifactRef, ...] = ()
    outputs: tuple[ActivityArtifactRef, ...] = ()
    error: ActivityError | None = None

    @model_validator(mode="after")
    def _check_status_values(self) -> ActivityManifest:
        if self.status not in {"started", "succeeded", "failed"}:
            raise ValueError(f"status must be one of started/succeeded/failed, got {self.status!r}")
        return self

    @model_validator(mode="after")
    def _check_status_shape(self) -> ActivityManifest:
        if self.status == "started":
            if self.completed_at is not None:
                raise ValueError("status='started' must not have completed_at")
            if self.outputs:
                raise ValueError("status='started' must not have outputs")
            if self.error is not None:
                raise ValueError("status='started' must not have an error")
        elif self.status == "succeeded":
            if self.completed_at is None:
                raise ValueError("status='succeeded' requires completed_at")
            if not self.inputs and not self.outputs:
                raise ValueError("status='succeeded' requires at least one input or output")
            if self.error is not None:
                raise ValueError("status='succeeded' must not have an error")
        elif self.status == "failed":
            if self.completed_at is None:
                raise ValueError("status='failed' requires completed_at")
            if self.error is None:
                raise ValueError("status='failed' requires an error")
            if self.outputs:
                raise ValueError("status='failed' must not have outputs")
        return self

    @model_validator(mode="after")
    def _check_unique_roles_per_direction(self) -> ActivityManifest:
        _check_unique_roles(self.inputs, "input")
        _check_unique_roles(self.outputs, "output")
        return self

    @model_validator(mode="after")
    def _check_code_revision(self) -> ActivityManifest:
        validate_code_revision(self.code_revision)
        return self
