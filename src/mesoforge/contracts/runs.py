"""Run manifest contract (plan Section 4.8)."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator

from mesoforge.common.time import UtcInstant

_CODE_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "run-manifest.v1"
    run_id: str
    forecast_issue_time: UtcInstant
    information_cutoff: UtcInstant
    configuration_snapshot_id: str
    configuration_digest: str
    code_revision: str
    environment_digest: str
    lockfile_digest: str
    random_seed: int
    selected_input_artifact_ids: tuple[str, ...] = ()
    created_at: UtcInstant

    @field_validator("code_revision")
    @classmethod
    def _check_code_revision(cls, value: str) -> str:
        if not _CODE_REVISION_RE.match(value):
            raise ValueError(
                f"code_revision must be a 40-character lowercase hex Git SHA, got {value!r}"
            )
        return value
