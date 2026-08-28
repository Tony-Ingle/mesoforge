"""Unit tests for mesoforge.common.identifiers (Task 3, plan Section 4.1).

RED: written before src/mesoforge/common/identifiers.py exists.
"""

from __future__ import annotations

import uuid

import pytest

from mesoforge.common.errors import InvalidIdentifier
from mesoforge.common.identifiers import (
    ActivityId,
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    GridId,
    MatchingPolicyId,
    MetricSetId,
    RunId,
    StationId,
    VariableId,
    VerticalDefinitionId,
)


class TestArtifactId:
    def test_generate_produces_valid_id(self) -> None:
        artifact_id = ArtifactId.generate()
        assert artifact_id.startswith("art_")
        assert isinstance(artifact_id, str)

    def test_accepts_canonical_lowercase_uuid(self) -> None:
        raw = str(uuid.uuid4())
        artifact_id = ArtifactId(f"art_{raw}")
        assert artifact_id == f"art_{raw}"

    def test_rejects_wrong_prefix(self) -> None:
        with pytest.raises(InvalidIdentifier):
            ArtifactId(f"act_{uuid.uuid4()}")

    def test_rejects_uppercase_uuid(self) -> None:
        raw = str(uuid.uuid4()).upper()
        with pytest.raises(InvalidIdentifier):
            ArtifactId(f"art_{raw}")

    def test_rejects_braces(self) -> None:
        raw = str(uuid.uuid4())
        with pytest.raises(InvalidIdentifier):
            ArtifactId(f"art_{{{raw}}}")

    def test_rejects_malformed_length(self) -> None:
        with pytest.raises(InvalidIdentifier):
            ArtifactId("art_12345678-1234-1234-1234-1234567890")  # one hex short

    def test_two_generated_ids_differ(self) -> None:
        assert ArtifactId.generate() != ArtifactId.generate()


class TestActivityAndRunIds:
    def test_activity_id_prefix(self) -> None:
        activity_id = ActivityId.generate()
        assert activity_id.startswith("act_")

    def test_run_id_prefix(self) -> None:
        run_id = RunId.generate()
        assert run_id.startswith("run_")

    def test_activity_id_rejects_artifact_prefix(self) -> None:
        with pytest.raises(InvalidIdentifier):
            ActivityId(f"art_{uuid.uuid4()}")

    def test_run_id_rejects_activity_prefix(self) -> None:
        with pytest.raises(InvalidIdentifier):
            RunId(f"act_{uuid.uuid4()}")


class TestConfigurationSnapshotId:
    def test_accepts_well_formed_digest_id(self) -> None:
        digest_hex = "a" * 64
        snapshot_id = ConfigurationSnapshotId(f"cfg_sha256_{digest_hex}")
        assert snapshot_id == f"cfg_sha256_{digest_hex}"

    def test_rejects_short_hex(self) -> None:
        with pytest.raises(InvalidIdentifier):
            ConfigurationSnapshotId("cfg_sha256_" + "a" * 63)

    def test_rejects_uppercase_hex(self) -> None:
        with pytest.raises(InvalidIdentifier):
            ConfigurationSnapshotId("cfg_sha256_" + "A" * 64)

    def test_rejects_unknown_algorithm(self) -> None:
        with pytest.raises(InvalidIdentifier):
            ConfigurationSnapshotId("cfg_md5_" + "a" * 64)

    def test_from_digest_builds_id(self) -> None:
        digest = Digest(f"sha256:{'b' * 64}")
        snapshot_id = ConfigurationSnapshotId.from_digest(digest)
        assert snapshot_id == f"cfg_sha256_{'b' * 64}"


class TestDigest:
    def test_accepts_well_formed_digest(self) -> None:
        digest = Digest(f"sha256:{'c' * 64}")
        assert digest == f"sha256:{'c' * 64}"

    def test_rejects_unknown_algorithm(self) -> None:
        with pytest.raises(InvalidIdentifier):
            Digest(f"md5:{'c' * 32}")

    def test_rejects_uppercase_hex(self) -> None:
        with pytest.raises(InvalidIdentifier):
            Digest(f"sha256:{'C' * 64}")

    def test_rejects_wrong_length(self) -> None:
        with pytest.raises(InvalidIdentifier):
            Digest(f"sha256:{'c' * 63}")

    def test_of_bytes_computes_correct_digest(self) -> None:
        digest = Digest.of_bytes(b"hello world")
        assert digest == ("sha256:b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9")


class TestKebabDotIdentifiers:
    @pytest.mark.parametrize(
        "cls", [GridId, VariableId, VerticalDefinitionId, StationId, MatchingPolicyId, MetricSetId]
    )
    def test_accepts_valid_ids(self, cls: type) -> None:
        for value in ("synthetic-grid.v1", "air-temperature-2m", "height-agl-2m", "a"):
            assert cls(value) == value

    @pytest.mark.parametrize(
        "cls", [GridId, VariableId, VerticalDefinitionId, StationId, MatchingPolicyId, MetricSetId]
    )
    def test_rejects_uppercase(self, cls: type) -> None:
        with pytest.raises(InvalidIdentifier):
            cls("Synthetic-Grid.v1")

    @pytest.mark.parametrize(
        "cls", [GridId, VariableId, VerticalDefinitionId, StationId, MatchingPolicyId, MetricSetId]
    )
    def test_rejects_leading_digit(self, cls: type) -> None:
        with pytest.raises(InvalidIdentifier):
            cls("1grid")

    @pytest.mark.parametrize(
        "cls", [GridId, VariableId, VerticalDefinitionId, StationId, MatchingPolicyId, MetricSetId]
    )
    def test_rejects_double_separators(self, cls: type) -> None:
        with pytest.raises(InvalidIdentifier):
            cls("grid--v1")

    @pytest.mark.parametrize(
        "cls", [GridId, VariableId, VerticalDefinitionId, StationId, MatchingPolicyId, MetricSetId]
    )
    def test_rejects_trailing_separator(self, cls: type) -> None:
        with pytest.raises(InvalidIdentifier):
            cls("grid-")

    @pytest.mark.parametrize(
        "cls", [GridId, VariableId, VerticalDefinitionId, StationId, MatchingPolicyId, MetricSetId]
    )
    def test_rejects_empty_string(self, cls: type) -> None:
        with pytest.raises(InvalidIdentifier):
            cls("")


class TestPhase1KebabDotIdentifiers:
    def test_station_id_accepts_canonical_station_form(self) -> None:
        assert StationId("station.kcbg") == "station.kcbg"

    def test_matching_policy_id_accepts_versioned_form(self) -> None:
        assert MatchingPolicyId("metar-nearest-15m.v1") == "metar-nearest-15m.v1"

    def test_metric_set_id_accepts_versioned_form(self) -> None:
        assert MetricSetId("phase1-temperature-wind.v1") == "phase1-temperature-wind.v1"
