"""Unit tests for mesoforge.forecasting.contributions (plan Section
4.7/5.1, Task 10): blend-contribution-manifest.v1 reconstruction and
completeness.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mesoforge.forecasting.contributions import (
    BlendContributionManifest,
    BlendContributionRow,
    ContributorRecord,
    IdentityBiasCorrection,
)


def _contributor(model: str, value: float, weight: float) -> ContributorRecord:
    return ContributorRecord(
        model=model,  # type: ignore[arg-type]
        source_cycle_reference_time="2026-08-30T12:00:00Z",
        source_forecast_hour=6,
        artifact_id="art_00000000-0000-4000-8000-000000000000",
        aligned_value=value,
        configured_weight=weight,
        weighted_contribution=value * weight,
    )


def _row(**overrides: object) -> BlendContributionRow:
    contributors = (
        _contributor("HRRR", 280.0, 0.6),
        _contributor("NBM", 290.0, 0.4),
    )
    values: dict[str, object] = {
        "variable_id": "air_temperature_2m",
        "location": "station.kcbg",
        "target_horizon": 6,
        "target_valid_time": "2026-08-30T18:00:00Z",
        "operator_id": "scalar-blend.v1",
        "availability_state": "fallback",
        "fallback_row_id": "scalar-vector.hn.h01-h18",
        "contributors": contributors,
        "unrounded_sum": sum(c.weighted_contribution for c in contributors),
        "serialized_output": sum(c.weighted_contribution for c in contributors),
    }
    values.update(overrides)
    return BlendContributionRow(**values)  # type: ignore[arg-type]


class TestContributorRecord:
    def test_accepts_correct_weighted_contribution(self) -> None:
        record = _contributor("HRRR", 10.0, 0.5)
        assert record.weighted_contribution == pytest.approx(5.0)

    def test_rejects_wrong_weighted_contribution(self) -> None:
        with pytest.raises(ValidationError, match="weighted_contribution"):
            ContributorRecord(
                model="HRRR",
                source_cycle_reference_time="2026-08-30T12:00:00Z",
                source_forecast_hour=6,
                artifact_id="art_00000000-0000-4000-8000-000000000000",
                aligned_value=10.0,
                configured_weight=0.5,
                weighted_contribution=999.0,
            )


class TestBlendContributionRow:
    def test_reconstructs_within_tolerance(self) -> None:
        row = _row()
        assert row.unrounded_sum == pytest.approx(row.serialized_output)

    def test_rejects_non_reconstructable_sum(self) -> None:
        with pytest.raises(ValidationError, match="does not reconstruct"):
            _row(unrounded_sum=999.0)

    def test_rejects_out_of_range_horizon(self) -> None:
        with pytest.raises(ValidationError, match="1..36"):
            _row(target_horizon=37)

    def test_unavailable_state_skips_reconstruction_check(self) -> None:
        row = _row(
            availability_state="unavailable",
            fallback_row_id=None,
            contributors=(),
            unrounded_sum=0.0,
            serialized_output=0.0,
        )
        assert row.availability_state == "unavailable"


class TestBlendContributionManifest:
    def test_complete_manifest_validates(self) -> None:
        manifest = BlendContributionManifest(
            rows=(_row(),),
            expected_variable_ids=("air_temperature_2m",),
            expected_locations=("station.kcbg",),
            expected_target_horizons=(6,),
            configuration_digest="sha256:" + "0" * 64,
            code_revision="a" * 40,
            environment_digest="sha256:" + "1" * 64,
            lockfile_digest="sha256:" + "2" * 64,
        )
        assert len(manifest.rows) == 1

    def test_rejects_missing_row(self) -> None:
        with pytest.raises(ValidationError, match="missing"):
            BlendContributionManifest(
                rows=(),
                expected_variable_ids=("air_temperature_2m",),
                expected_locations=("station.kcbg",),
                expected_target_horizons=(6,),
                configuration_digest="sha256:" + "0" * 64,
                code_revision="a" * 40,
                environment_digest="sha256:" + "1" * 64,
                lockfile_digest="sha256:" + "2" * 64,
            )

    def test_rejects_duplicate_row(self) -> None:
        with pytest.raises(ValidationError, match="duplicate"):
            BlendContributionManifest(
                rows=(_row(), _row()),
                expected_variable_ids=("air_temperature_2m",),
                expected_locations=("station.kcbg",),
                expected_target_horizons=(6,),
                configuration_digest="sha256:" + "0" * 64,
                code_revision="a" * 40,
                environment_digest="sha256:" + "1" * 64,
                lockfile_digest="sha256:" + "2" * 64,
            )

    def test_rejects_extra_row(self) -> None:
        extra_row = _row(
            variable_id="dew_point_temperature_2m",
            contributors=(
                _contributor("HRRR", 270.0, 0.6),
                _contributor("NBM", 275.0, 0.4),
            ),
            unrounded_sum=270.0 * 0.6 + 275.0 * 0.4,
            serialized_output=270.0 * 0.6 + 275.0 * 0.4,
        )
        with pytest.raises(ValidationError, match="unexpected"):
            BlendContributionManifest(
                rows=(_row(), extra_row),
                expected_variable_ids=("air_temperature_2m",),
                expected_locations=("station.kcbg",),
                expected_target_horizons=(6,),
                configuration_digest="sha256:" + "0" * 64,
                code_revision="a" * 40,
                environment_digest="sha256:" + "1" * 64,
                lockfile_digest="sha256:" + "2" * 64,
            )


class TestIdentityBiasCorrection:
    def test_default_is_zero(self) -> None:
        correction = IdentityBiasCorrection()
        assert correction.applied_delta == 0.0

    def test_rejects_nonzero_delta(self) -> None:
        with pytest.raises(ValidationError, match="exactly 0.0"):
            IdentityBiasCorrection(applied_delta=1.0)
