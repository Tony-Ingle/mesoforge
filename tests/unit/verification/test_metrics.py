"""Unit tests for mesoforge.verification.metrics (plan Section 3.9,
Task 11): exact formulas, null-on-zero-samples, missing-count
breakdown, and overall/by-lead/by-station stratification."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mesoforge.catalog.configuration import MetricSet
from mesoforge.contracts.verification import MatchedPairRow
from mesoforge.verification.metrics import (
    _compute_direction_metric,
    _compute_scalar_metrics,
    _signed_circular_diff_degrees,
    compute_verification_report,
)

_ARTIFACT = "art_00000000-0000-0000-0000-000000000000"
_DIGEST = "sha256:" + "a" * 64
_METRIC_SET = MetricSet(
    metric_set_id="phase1-temperature-wind.v1",
    metric_names=(
        "temperature_bias",
        "temperature_mae",
        "temperature_rmse",
        "eastward_wind_bias",
        "eastward_wind_mae",
        "eastward_wind_rmse",
        "northward_wind_bias",
        "northward_wind_mae",
        "northward_wind_rmse",
        "wind_speed_bias",
        "wind_speed_mae",
        "wind_speed_rmse",
        "wind_direction_mean_absolute_circular_error",
    ),
)


def _matched_row(
    *,
    station_id: str = "station.kcbg",
    lead_hours: int = 0,
    forecast_temperature_k: float | None = 290.0,
    observed_temperature_k: float | None = 288.0,
    forecast_wind_from_direction_degrees: float | None = 10.0,
    observed_wind_from_direction_degrees: float | None = 350.0,
    temperature_status: str = "matched",
    wind_direction_status: str | None = None,
) -> MatchedPairRow:
    all_matched = temperature_status == "matched"
    if wind_direction_status is None:
        wind_direction_status = "matched" if all_matched else "no_report_within_tolerance"
    any_matched = (
        "matched"
        in (
            temperature_status,
            wind_direction_status,
        )
        or all_matched
    )
    return MatchedPairRow(
        station_id=station_id,  # type: ignore[arg-type]
        lead_hours=lead_hours,
        valid_time=datetime(2026, 8, 28, 12 + lead_hours, 0, tzinfo=UTC),
        baseline_artifact_id=_ARTIFACT,
        observations_artifact_id=_ARTIFACT,
        matching_policy_id="metar-nearest-15m.v1",  # type: ignore[arg-type]
        matching_policy_digest=_DIGEST,  # type: ignore[arg-type]
        verification_cutoff=datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
        forecast_temperature_k=forecast_temperature_k,
        forecast_eastward_wind_m_s=1.0,
        forecast_northward_wind_m_s=-2.0,
        forecast_wind_speed_m_s=2.236,
        forecast_wind_from_direction_degrees=forecast_wind_from_direction_degrees,
        observed_temperature_k=observed_temperature_k,
        observed_eastward_wind_m_s=1.5,
        observed_northward_wind_m_s=-1.5,
        observed_wind_speed_m_s=2.121,
        observed_wind_from_direction_degrees=observed_wind_from_direction_degrees,
        row_status="matched_any_field" if any_matched else "matched_no_fields",
        temperature_status=temperature_status,  # type: ignore[arg-type]
        eastward_component_status="matched" if all_matched else "no_report_within_tolerance",  # type: ignore[arg-type]
        northward_component_status="matched" if all_matched else "no_report_within_tolerance",  # type: ignore[arg-type]
        wind_speed_status="matched" if all_matched else "no_report_within_tolerance",  # type: ignore[arg-type]
        wind_direction_status=wind_direction_status,  # type: ignore[arg-type]
    )


class TestComputeScalarMetrics:
    def test_known_bias_mae_rmse(self) -> None:
        # errors: 2, -2 -> bias 0, mae 2, rmse 2
        bias, mae, rmse = _compute_scalar_metrics([2.0, -2.0])
        assert bias == pytest.approx(0.0)
        assert mae == pytest.approx(2.0)
        assert rmse == pytest.approx(2.0)

    def test_rmse_differs_from_mae_for_varying_errors(self) -> None:
        bias, mae, rmse = _compute_scalar_metrics([1.0, 3.0])
        assert bias == pytest.approx(2.0)
        assert mae == pytest.approx(2.0)
        assert rmse == pytest.approx((5.0) ** 0.5)  # sqrt((1+9)/2)

    def test_empty_gives_all_none(self) -> None:
        assert _compute_scalar_metrics([]) == (None, None, None)

    def test_rejects_nonfinite_input(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            _compute_scalar_metrics([1.0, float("nan")])


class TestSignedCircularDiff:
    def test_near_zero_wraparound(self) -> None:
        assert _signed_circular_diff_degrees(10.0, 350.0) == pytest.approx(20.0)

    def test_opposite_directions(self) -> None:
        assert abs(_signed_circular_diff_degrees(0.0, 180.0)) == pytest.approx(180.0)

    def test_identical_directions_zero(self) -> None:
        assert _signed_circular_diff_degrees(90.0, 90.0) == pytest.approx(0.0)


class TestComputeDirectionMetric:
    def test_mean_absolute_circular_error(self) -> None:
        value = _compute_direction_metric([20.0, 10.0])
        assert value == pytest.approx(15.0)

    def test_empty_gives_none(self) -> None:
        assert _compute_direction_metric([]) is None


class TestComputeVerificationReport:
    def test_overall_temperature_bias_is_correct(self) -> None:
        rows = [
            _matched_row(forecast_temperature_k=290.0, observed_temperature_k=288.0),
            _matched_row(forecast_temperature_k=286.0, observed_temperature_k=288.0),
        ]
        report = compute_verification_report(
            rows,
            metric_set=_METRIC_SET,
            station_ids=("station.kcbg",),
            lead_hours=(0,),
            baseline_artifact_id=_ARTIFACT,
            matched_pairs_artifact_id=_ARTIFACT,
        )
        overall_bias = next(
            r
            for r in report.rows
            if r.stratum_kind == "overall" and r.metric_name == "temperature_bias"
        )
        assert overall_bias.value == pytest.approx(0.0)
        assert overall_bias.sample_count == 2

    def test_zero_samples_gives_null_value(self) -> None:
        rows = [_matched_row(temperature_status="no_report_within_tolerance")]
        report = compute_verification_report(
            rows,
            metric_set=_METRIC_SET,
            station_ids=("station.kcbg",),
            lead_hours=(0,),
            baseline_artifact_id=_ARTIFACT,
            matched_pairs_artifact_id=_ARTIFACT,
        )
        overall_bias = next(
            r
            for r in report.rows
            if r.stratum_kind == "overall" and r.metric_name == "temperature_bias"
        )
        assert overall_bias.value is None
        assert overall_bias.sample_count == 0

    def test_missing_counts_broken_out_by_reason(self) -> None:
        rows = [
            _matched_row(temperature_status="no_report_within_tolerance"),
            _matched_row(temperature_status="station_metadata_conflict"),
        ]
        report = compute_verification_report(
            rows,
            metric_set=_METRIC_SET,
            station_ids=("station.kcbg",),
            lead_hours=(0,),
            baseline_artifact_id=_ARTIFACT,
            matched_pairs_artifact_id=_ARTIFACT,
        )
        overall_bias = next(
            r
            for r in report.rows
            if r.stratum_kind == "overall" and r.metric_name == "temperature_bias"
        )
        assert overall_bias.missing_counts.no_report_within_tolerance == 1
        assert overall_bias.missing_counts.station_metadata_conflict == 1
        assert overall_bias.missing_counts.total == 2

    def test_by_lead_stratum_isolates_lead(self) -> None:
        rows = [
            _matched_row(lead_hours=0, forecast_temperature_k=290.0, observed_temperature_k=288.0),
            _matched_row(lead_hours=1, forecast_temperature_k=300.0, observed_temperature_k=280.0),
        ]
        report = compute_verification_report(
            rows,
            metric_set=_METRIC_SET,
            station_ids=("station.kcbg",),
            lead_hours=(0, 1),
            baseline_artifact_id=_ARTIFACT,
            matched_pairs_artifact_id=_ARTIFACT,
        )
        lead0_bias = next(
            r
            for r in report.rows
            if r.stratum_kind == "by_lead"
            and r.stratum_lead_hours == 0
            and r.metric_name == "temperature_bias"
        )
        assert lead0_bias.value == pytest.approx(2.0)
        assert lead0_bias.sample_count == 1

    def test_by_station_stratum_isolates_station(self) -> None:
        rows = [
            _matched_row(
                station_id="station.kcbg",
                forecast_temperature_k=290.0,
                observed_temperature_k=288.0,
            ),
            _matched_row(
                station_id="station.kjmr",
                forecast_temperature_k=280.0,
                observed_temperature_k=288.0,
            ),
        ]
        report = compute_verification_report(
            rows,
            metric_set=_METRIC_SET,
            station_ids=("station.kcbg", "station.kjmr"),
            lead_hours=(0,),
            baseline_artifact_id=_ARTIFACT,
            matched_pairs_artifact_id=_ARTIFACT,
        )
        kcbg_bias = next(
            r
            for r in report.rows
            if r.stratum_kind == "by_station"
            and r.stratum_station_id == "station.kcbg"
            and r.metric_name == "temperature_bias"
        )
        assert kcbg_bias.value == pytest.approx(2.0)

    def test_wind_direction_metric_only_uses_matched_direction_rows(self) -> None:
        rows = [
            _matched_row(
                forecast_wind_from_direction_degrees=10.0,
                observed_wind_from_direction_degrees=350.0,
                wind_direction_status="matched",
            ),
            _matched_row(
                forecast_wind_from_direction_degrees=None,
                observed_wind_from_direction_degrees=None,
                wind_direction_status="calm_direction_excluded",
            ),
        ]
        report = compute_verification_report(
            rows,
            metric_set=_METRIC_SET,
            station_ids=("station.kcbg",),
            lead_hours=(0,),
            baseline_artifact_id=_ARTIFACT,
            matched_pairs_artifact_id=_ARTIFACT,
        )
        direction_metric = next(
            r
            for r in report.rows
            if r.stratum_kind == "overall"
            and r.metric_name == "wind_direction_mean_absolute_circular_error"
        )
        assert direction_metric.sample_count == 1
        assert direction_metric.value == pytest.approx(20.0)
        assert direction_metric.missing_counts.calm_direction_excluded == 1

    def test_report_contains_all_13_metrics_per_stratum(self) -> None:
        rows = [_matched_row()]
        report = compute_verification_report(
            rows,
            metric_set=_METRIC_SET,
            station_ids=("station.kcbg",),
            lead_hours=(0,),
            baseline_artifact_id=_ARTIFACT,
            matched_pairs_artifact_id=_ARTIFACT,
        )
        overall_rows = [r for r in report.rows if r.stratum_kind == "overall"]
        assert len(overall_rows) == 13
