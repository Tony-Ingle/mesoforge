"""Run manifest contract (plan Section 4.8)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from mesoforge.common.identifiers import (
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    RunId,
    validate_code_revision,
)
from mesoforge.common.time import UtcInstant


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["run-manifest.v1"] = "run-manifest.v1"
    run_id: RunId
    forecast_issue_time: UtcInstant
    information_cutoff: UtcInstant
    configuration_snapshot_id: ConfigurationSnapshotId
    configuration_digest: Digest
    code_revision: str
    environment_digest: Digest
    lockfile_digest: Digest
    random_seed: int
    selected_input_artifact_ids: tuple[ArtifactId, ...] = ()
    created_at: UtcInstant

    @field_validator("code_revision")
    @classmethod
    def _check_code_revision(cls, value: str) -> str:
        return validate_code_revision(value)
