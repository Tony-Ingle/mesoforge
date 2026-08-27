"""Run manifest contract (plan Section 4.8)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from mesoforge.common.identifiers import ArtifactId, ConfigurationSnapshotId, Digest, RunId
from mesoforge.common.time import UtcInstant

_CODE_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


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
        if not _CODE_REVISION_RE.match(value):
            raise ValueError(
                f"code_revision must be a 40-character lowercase hex Git SHA, got {value!r}"
            )
        return value
