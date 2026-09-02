from __future__ import annotations

import json
from datetime import timedelta

from mesoforge.verification.metrics import compute_verification_report_v2
from tests.contracts.test_verification_v2 import _row

ART = "art_00000000-0000-0000-0000-000000000001"
_FALLBACK_STATES = {
    "temperature_availability_state": "fallback",
    "dew_point_availability_state": "fallback",
    "eastward_wind_availability_state": "fallback",
    "northward_wind_availability_state": "fallback",
    "wind_speed_availability_state": "fallback",
    "wind_direction_availability_state": "fallback",
    "gust_availability_state": "fallback",
    "qpf_availability_state": "fallback",
    "pop_availability_state": "fallback",
}


def _matched_row():
    return _row(
        row_status="matched_any_field",
        availability_state="fallback",
        **_FALLBACK_STATES,
        temperature_status="matched",
        forecast_temperature_k=282.0,
        observed_temperature_k=280.0,
        dew_point_status="matched",
        forecast_dew_point_k=276.0,
        observed_dew_point_k=275.0,
        eastward_component_status="matched",
        forecast_eastward_wind_m_s=4.0,
        observed_eastward_wind_m_s=3.0,
        northward_component_status="matched",
        forecast_northward_wind_m_s=5.0,
        observed_northward_wind_m_s=4.0,
        wind_speed_status="matched",
        forecast_wind_speed_m_s=6.4,
        observed_wind_speed_m_s=5.0,
        wind_direction_status="matched",
        forecast_wind_from_direction_degrees=220.0,
        observed_wind_from_direction_degrees=210.0,
        gust_status="matched",
        forecast_wind_gust_m_s=9.0,
        observed_wind_gust_m_s=8.0,
        qpf_status="matched",
        forecast_qpf_kg_m2=3.0,
        observed_qpf_kg_m2=1.0,
        pop_status="matched",
        forecast_pop_probability=0.75,
    )


def _matched_rows():
    first = _matched_row()
    midnight = first.valid_time.replace(hour=0)
    rows = []
    for station in ("station.kcbg", "station.kjmr", "station.kros"):
        for horizon in range(1, 37):
            row = _row(
                station_id=station,
                target_horizon_hours=horizon,
                valid_time=midnight + timedelta(hours=horizon),
                precipitation_interval_start=midnight + timedelta(hours=horizon - 1),
                precipitation_interval_end=midnight + timedelta(hours=horizon),
                availability_state="fallback",
                **_FALLBACK_STATES,
            )
            rows.append(row)
    rows[0] = first
    return rows


def test_all_strata_metrics_thresholds_and_conditional_gust_label() -> None:
    report = compute_verification_report_v2(
        _matched_rows(),
        metric_set_id="phase2-multimodel-station.v1",
        baseline_artifact_id=ART,
        matched_pairs_artifact_id=ART,
    )
    assert {row.stratum_kind for row in report.rows} == {
        "overall",
        "by_target_horizon",
        "by_station",
        "by_availability_state",
    }
    overall = [row for row in report.rows if row.stratum_kind == "overall"]
    names = {row.metric_name for row in overall}
    assert {
        "qpf_csi_at_0.254",
        "qpf_ets_at_0.254",
        "qpf_csi_at_2.54",
        "qpf_ets_at_2.54",
        "pop_brier_score",
        "pop_roc_auc",
    } <= names
    assert all(
        row.label == "conditional_on_reported_gust"
        for row in overall
        if row.metric_name.startswith("gust_")
    )
    payload = report.model_dump_json()
    assert "NaN" not in payload and "Infinity" not in payload
    json.loads(payload, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def test_report_rejects_nonfinite_matched_inputs_instead_of_hiding_them() -> None:
    rows = _matched_rows()
    rows[0] = _matched_row().model_construct(
        **{**_matched_row().__dict__, "forecast_temperature_k": float("nan")}
    )
    try:
        compute_verification_report_v2(
            rows,
            metric_set_id="phase2-multimodel-station.v1",
            baseline_artifact_id=ART,
            matched_pairs_artifact_id=ART,
        )
    except ValueError as exc:
        assert "finite" in str(exc)
    else:
        raise AssertionError("nonfinite input was silently accepted")


def test_availability_strata_filter_each_metric_by_its_variable_state() -> None:
    rows = _matched_rows()
    rows[0] = _matched_row().model_copy(
        update={
            "availability_state": "unavailable",
            "temperature_availability_state": "fallback",
            "dew_point_availability_state": "fallback",
            "eastward_wind_availability_state": "fallback",
            "northward_wind_availability_state": "fallback",
            "wind_speed_availability_state": "fallback",
            "wind_direction_availability_state": "fallback",
            "gust_availability_state": "fallback",
            "qpf_availability_state": "fallback",
            "pop_availability_state": "unavailable",
        }
    )
    report = compute_verification_report_v2(
        rows,
        metric_set_id="phase2-multimodel-station.v1",
        baseline_artifact_id=ART,
        matched_pairs_artifact_id=ART,
    )
    availability = {
        (row.stratum_value, row.metric_name): row.sample_count
        for row in report.rows
        if row.stratum_kind == "by_availability_state"
    }
    assert availability[("fallback", "temperature_mae")] == 1
    assert availability[("fallback", "wind_speed_mae")] == 1
    assert availability[("fallback", "gust_mae")] == 1
    assert availability[("fallback", "qpf_mae")] == 1
    assert availability[("fallback", "pop_brier_score")] == 0
    assert availability[("unavailable", "temperature_mae")] == 0
    assert availability[("unavailable", "pop_brier_score")] == 1
