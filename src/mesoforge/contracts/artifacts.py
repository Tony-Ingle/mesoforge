"""Artifact manifest and availability contracts (plan Section 4.8).

Artifacts never carry a ``parents`` field -- lineage comes entirely from
first-class activity edges (``contracts/provenance.py``).
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.common.identifiers import (
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    RunId,
    validate_code_revision,
)
from mesoforge.common.time import UtcInstant

_MAX_ATTRIBUTES_BYTES = 32 * 1024


class Availability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["availability.v1"] = "availability.v1"
    available_at: UtcInstant
    authority: str
    method: str
    ingested_at: UtcInstant | None = None
    metadata: dict[str, str] | None = None


class SourceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    authority: str
    locator: str
    revision: str


class ArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["artifact-manifest.v1"] = "artifact-manifest.v1"
    artifact_id: ArtifactId
    artifact_type: str
    artifact_schema_version: str
    media_type: str
    byte_size: int
    content_digest: Digest
    storage_uri: str
    created_at: UtcInstant
    registered_at: UtcInstant
    availability: Availability
    run_id: RunId | None = None
    configuration_snapshot_id: ConfigurationSnapshotId
    configuration_digest: Digest
    code_revision: str
    environment_digest: Digest
    quality_state: str
    source_registration_digest: Digest | None = None
    source_identity: SourceIdentity | None = None
    attributes: dict[str, object] | None = None

    @model_validator(mode="after")
    def _check_byte_size(self) -> ArtifactManifest:
        if self.byte_size < 0:
            raise ValueError("byte_size must be nonnegative")
        return self

    @model_validator(mode="after")
    def _check_storage_uri_scheme(self) -> ArtifactManifest:
        if not self.storage_uri.startswith("s3://"):
            raise ValueError(f"storage_uri must use the s3:// scheme, got {self.storage_uri!r}")
        return self

    @model_validator(mode="after")
    def _check_quality_state(self) -> ArtifactManifest:
        if self.quality_state not in {"valid", "partial", "invalid"}:
            raise ValueError(
                f"quality_state must be one of valid/partial/invalid, got {self.quality_state!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_source_fields_all_or_nothing(self) -> ArtifactManifest:
        has_digest = self.source_registration_digest is not None
        has_identity = self.source_identity is not None
        if has_digest != has_identity:
            raise ValueError(
                "source_registration_digest and source_identity must be supplied together "
                "(both present for lineage-root/source artifacts, both absent for derived "
                "artifacts)"
            )
        return self

    @model_validator(mode="after")
    def _check_attributes_size(self) -> ArtifactManifest:
        if self.attributes is not None:
            encoded = json.dumps(self.attributes, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
            if len(encoded) > _MAX_ATTRIBUTES_BYTES:
                raise ValueError(
                    f"attributes exceed the {_MAX_ATTRIBUTES_BYTES}-byte limit after canonical "
                    f"serialization ({len(encoded)} bytes)"
                )
        return self

    @model_validator(mode="after")
    def _check_code_revision(self) -> ArtifactManifest:
        validate_code_revision(self.code_revision)
        return self
