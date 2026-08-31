"""Unit tests for mesoforge.guidance.precipitation (plan Section 2.4,
Task 4): GFS APCP bucket/continuous disambiguation and same-bucket
differencing.
"""

from __future__ import annotations

import pytest

from mesoforge.guidance.precipitation import (
    ApcpCandidateRecord,
    GfsPrecipitationError,
    compute_bucket_start,
    compute_one_hour_qpf,
    is_bucket_reset_hour,
    select_bucket_record,
    validate_dual_parent_equivalence,
)


class TestComputeBucketStart:
    @pytest.mark.parametrize(
        "forecast_hour,expected",
        [
            (1, 0),
            (6, 0),
            (7, 6),
            (8, 6),
            (12, 6),
            (13, 12),
            (18, 12),
            (19, 18),
            (24, 18),
            (25, 24),
            (30, 24),
            (31, 30),
            (36, 30),
            (37, 36),
            (42, 36),
            (43, 42),
            (47, 42),
            (48, 42),
        ],
    )
    def test_matches_plan_boundary_cases(self, forecast_hour: int, expected: int) -> None:
        assert compute_bucket_start(forecast_hour) == expected

    def test_rejects_forecast_hour_zero(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            compute_bucket_start(0)


class TestIsBucketResetHour:
    @pytest.mark.parametrize(
        "forecast_hour,expected",
        [
            (1, True),
            (7, True),
            (13, True),
            (19, True),
            (25, True),
            (31, True),
            (37, True),
            (43, True),
        ],
    )
    def test_reset_hours(self, forecast_hour: int, expected: bool) -> None:
        assert is_bucket_reset_hour(forecast_hour) is expected

    @pytest.mark.parametrize("forecast_hour", [2, 6, 8, 12, 18, 24, 36, 48])
    def test_non_reset_hours(self, forecast_hour: int) -> None:
        assert is_bucket_reset_hour(forecast_hour) is False


class TestComputeOneHourQpf:
    def test_reset_hour_passthrough(self) -> None:
        result = compute_one_hour_qpf(
            forecast_hour=1, bucket_value_current_kg_m2=2.5, bucket_value_previous_kg_m2=None
        )
        assert result.one_hour_qpf_kg_m2 == pytest.approx(2.5)
        assert result.is_reset_passthrough is True
        assert result.bucket_start_hour == 0

    def test_f07_passthrough(self) -> None:
        result = compute_one_hour_qpf(
            forecast_hour=7, bucket_value_current_kg_m2=1.0, bucket_value_previous_kg_m2=None
        )
        assert result.is_reset_passthrough is True
        assert result.one_hour_qpf_kg_m2 == pytest.approx(1.0)

    def test_reset_hour_rejects_previous_value(self) -> None:
        with pytest.raises(GfsPrecipitationError, match="reset hour"):
            compute_one_hour_qpf(
                forecast_hour=1, bucket_value_current_kg_m2=2.5, bucket_value_previous_kg_m2=1.0
            )

    def test_normal_differencing(self) -> None:
        result = compute_one_hour_qpf(
            forecast_hour=8, bucket_value_current_kg_m2=3.5, bucket_value_previous_kg_m2=1.0
        )
        assert result.one_hour_qpf_kg_m2 == pytest.approx(2.5)
        assert result.is_reset_passthrough is False
        assert result.bucket_start_hour == 6

    def test_f08_requires_previous_value(self) -> None:
        with pytest.raises(GfsPrecipitationError, match="requires a previous-bucket"):
            compute_one_hour_qpf(
                forecast_hour=8, bucket_value_current_kg_m2=3.5, bucket_value_previous_kg_m2=None
            )

    def test_finite_precision_negative_floors_to_zero(self) -> None:
        result = compute_one_hour_qpf(
            forecast_hour=8,
            bucket_value_current_kg_m2=1.0000001,
            bucket_value_previous_kg_m2=1.0000005,
        )
        assert result.one_hour_qpf_kg_m2 == 0.0
        assert result.finite_precision_floor_applied is True

    def test_material_negative_difference_raises(self) -> None:
        with pytest.raises(GfsPrecipitationError, match="non-monotonic"):
            compute_one_hour_qpf(
                forecast_hour=8, bucket_value_current_kg_m2=1.0, bucket_value_previous_kg_m2=5.0
            )

    def test_f13_boundary_reset(self) -> None:
        result = compute_one_hour_qpf(
            forecast_hour=13, bucket_value_current_kg_m2=0.5, bucket_value_previous_kg_m2=None
        )
        assert result.bucket_start_hour == 12
        assert result.is_reset_passthrough is True

    def test_f12_boundary_differencing(self) -> None:
        result = compute_one_hour_qpf(
            forecast_hour=12, bucket_value_current_kg_m2=4.0, bucket_value_previous_kg_m2=3.5
        )
        assert result.bucket_start_hour == 6
        assert result.one_hour_qpf_kg_m2 == pytest.approx(0.5)


class TestSelectBucketRecord:
    def test_selects_exact_bucket_boundary(self) -> None:
        candidates = (
            ApcpCandidateRecord(
                start_step=6, end_step=8, is_accumulation=True, values_kg_m2=(2.0,)
            ),
            ApcpCandidateRecord(
                start_step=0, end_step=8, is_accumulation=True, values_kg_m2=(5.0,)
            ),
        )
        selected = select_bucket_record(candidates, forecast_hour=8)
        assert selected.start_step == 6

    def test_rejects_when_no_match(self) -> None:
        candidates = (
            ApcpCandidateRecord(
                start_step=0, end_step=8, is_accumulation=True, values_kg_m2=(5.0,)
            ),
        )
        with pytest.raises(GfsPrecipitationError, match="no APCP candidate"):
            select_bucket_record(candidates, forecast_hour=8)

    def test_rejects_ambiguous_duplicate_boundary(self) -> None:
        candidates = (
            ApcpCandidateRecord(
                start_step=6, end_step=8, is_accumulation=True, values_kg_m2=(2.0,)
            ),
            ApcpCandidateRecord(
                start_step=6, end_step=8, is_accumulation=True, values_kg_m2=(2.1,)
            ),
        )
        with pytest.raises(GfsPrecipitationError, match="ambiguous"):
            select_bucket_record(candidates, forecast_hour=8)

    def test_never_selects_by_record_order_only(self) -> None:
        """Message-order-only selection is forbidden -- reversing the
        candidate order must not change which record is selected."""
        candidates_a = (
            ApcpCandidateRecord(
                start_step=6, end_step=8, is_accumulation=True, values_kg_m2=(2.0,)
            ),
            ApcpCandidateRecord(
                start_step=0, end_step=8, is_accumulation=True, values_kg_m2=(5.0,)
            ),
        )
        candidates_b = tuple(reversed(candidates_a))
        assert select_bucket_record(candidates_a, forecast_hour=8) == select_bucket_record(
            candidates_b, forecast_hour=8
        )


class TestValidateDualParentEquivalence:
    def test_equivalent_records(self) -> None:
        bucket = ApcpCandidateRecord(
            start_step=0, end_step=3, is_accumulation=True, values_kg_m2=(1.0, 2.0)
        )
        continuous = ApcpCandidateRecord(
            start_step=0, end_step=3, is_accumulation=True, values_kg_m2=(1.0, 2.0)
        )
        assert validate_dual_parent_equivalence(bucket, continuous) is True

    def test_divergent_values_are_not_equivalent(self) -> None:
        bucket = ApcpCandidateRecord(
            start_step=0, end_step=3, is_accumulation=True, values_kg_m2=(1.0, 2.0)
        )
        continuous = ApcpCandidateRecord(
            start_step=0, end_step=3, is_accumulation=True, values_kg_m2=(1.0, 9.0)
        )
        assert validate_dual_parent_equivalence(bucket, continuous) is False

    def test_different_start_step_is_not_equivalent(self) -> None:
        bucket = ApcpCandidateRecord(
            start_step=0, end_step=3, is_accumulation=True, values_kg_m2=(1.0,)
        )
        continuous = ApcpCandidateRecord(
            start_step=1, end_step=3, is_accumulation=True, values_kg_m2=(1.0,)
        )
        assert validate_dual_parent_equivalence(bucket, continuous) is False

    def test_never_silently_accepts_a_divergent_duplicate(self) -> None:
        """Section 2.4 mutation probe: a materially divergent duplicate
        candidate must never be silently canonicalized as equivalent."""
        bucket = ApcpCandidateRecord(
            start_step=0, end_step=6, is_accumulation=True, values_kg_m2=(3.0,)
        )
        continuous = ApcpCandidateRecord(
            start_step=0, end_step=6, is_accumulation=True, values_kg_m2=(3.5,)
        )
        assert validate_dual_parent_equivalence(bucket, continuous) is False
