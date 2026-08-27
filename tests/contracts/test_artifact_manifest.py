"""Contract tests for ArtifactManifest and Availability (Task 7, plan
Section 4.8).

RED: written before src/mesoforge/contracts/artifacts.py exists.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mesoforge.contracts.artifacts import ArtifactManifest, Availability, SourceIdentity

_VALID_SHA = "a" * 40
_ART_ID = "art_00000000-0000-0000-0000-000000000001"
_DIGEST = "sha256:" + "a" * 64


def _availability(**overrides: object) -> Availability:
    kwargs: dict[str, object] = dict(
        available_at=datetime(2026, 1, 1, tzinfo=UTC),
        authority="mesoforge.synthetic",
        method="synthetic.v1",
    )
    kwargs.update(overrides)
    return Availability(**kwargs)


def _base_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = dict(
        artifact_id=_ART_ID,
        artifact_type="synthetic-dataset",
        artifact_schema_version="canonical-guidance.v1",
        media_type="application/x-netcdf",
        byte_size=1024,
        content_digest=_DIGEST,
        storage_uri="s3://mesoforge-phase0/objects/sha256/aa/" + "a" * 62,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        registered_at=datetime(2026, 1, 1, tzinfo=UTC),
        availability=_availability(),
        configuration_snapshot_id="cfg_sha256_" + "a" * 64,
        configuration_digest=_DIGEST,
        code_revision=_VALID_SHA,
        environment_digest=_DIGEST,
        quality_state="valid",
    )
    kwargs.update(overrides)
    return kwargs


class TestAvailability:
    def test_valid_construction(self) -> None:
        availability = _availability()
        assert availability.schema_version == "availability.v1"

    def test_optional_ingested_at(self) -> None:
        availability = _availability(ingested_at=datetime(2026, 1, 1, 1, tzinfo=UTC))
        assert availability.ingested_at is not None


class TestArtifactManifest:
    def test_valid_source_manifest(self) -> None:
        manifest = ArtifactManifest(
            **_base_kwargs(
                source_registration_digest=_DIGEST,
                source_identity=SourceIdentity(
                    authority="noaa.synthetic", locator="synthetic://x", revision="v1"
                ),
            )
        )
        assert manifest.schema_version == "artifact-manifest.v1"

    def test_valid_derived_manifest_has_no_source_fields(self) -> None:
        manifest = ArtifactManifest(**_base_kwargs())
        assert manifest.source_registration_digest is None
        assert manifest.source_identity is None

    def test_source_identity_requires_source_registration_digest(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactManifest(
                **_base_kwargs(
                    source_identity=SourceIdentity(
                        authority="noaa.synthetic", locator="x", revision="v1"
                    )
                )
            )

    def test_source_registration_digest_requires_source_identity(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactManifest(**_base_kwargs(source_registration_digest=_DIGEST))

    def test_byte_size_must_be_nonnegative(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactManifest(**_base_kwargs(byte_size=-1))

    def test_storage_uri_must_be_s3_scheme(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactManifest(**_base_kwargs(storage_uri="http://example.com/object"))

    def test_no_parents_field(self) -> None:
        manifest = ArtifactManifest(**_base_kwargs())
        assert not hasattr(manifest, "parents")

    def test_optional_run_id(self) -> None:
        manifest = ArtifactManifest(
            **_base_kwargs(run_id="run_00000000-0000-0000-0000-000000000001")
        )
        assert manifest.run_id is not None

    def test_attributes_size_limit(self) -> None:
        oversized = {"blob": "x" * (33 * 1024)}
        with pytest.raises(ValidationError):
            ArtifactManifest(**_base_kwargs(attributes=oversized))

    def test_frozen(self) -> None:
        manifest = ArtifactManifest(**_base_kwargs())
        with pytest.raises(ValidationError):
            manifest.quality_state = "invalid"  # type: ignore[misc]
