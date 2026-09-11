"""The hourly presentation preserves the numerical baseline and unrun stages."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mesoforge.application.hourly_report import build_hourly_report, render_hourly_report


@pytest.fixture
def forecast() -> dict[str, Any]:
    target = datetime(2026, 11, 1, 4, tzinfo=UTC)
    hours = []
    for horizon in range(1, 37):
        sources = []
        for model, value, weight in (
            ("HRRR", 273.15, 0.7),
            ("GFS", 283.15, 0.3),
            ("RAP", 293.15, 0.0),
            ("IFS", 303.15 if horizon % 3 == 0 else None, 0.0),
        ):
            sources.append(
                {
                    "model": model,
                    "cycle": "2026-11-01T00:00:00Z",
                    "source_lead_hours": horizon + 4 if value is not None else None,
                    "weight": weight,
                    "temperature": {"value": value, "unit": "K"},
                    "missing_reasons": [] if value is not None else ["IFS: no native valid time"],
                    "raw_sha256": model.lower() + "-fixture-digest",
                    "acquisition": {"retrieved_at": "2026-11-01T03:59:00Z"},
                }
            )
        hours.append(
            {
                "horizon_hours": horizon,
                "valid_time": (target + timedelta(hours=horizon)).isoformat(),
                "temperature": {"value": 276.15, "unit": "K"},
                "sources": sources[:2],
                "shadow_sources": sources[2:],
                "missing_reasons": [],
            }
        )
    return {
        "latitude": 44.98859,
        "longitude": -93.25557,
        "target_reference_time": target.isoformat(),
        "data_kind": "synthetic_demonstration",
        "notice": "Synthetic demonstration data; not a current weather forecast.",
        "hours": hours,
    }


def test_original_baseline_and_contributor_evidence_are_preserved(forecast: dict[str, Any]) -> None:
    original = deepcopy(forecast)
    report = build_hourly_report(forecast)
    assert forecast == original
    assert len(report["hours"]) == 36
    for source_hour, hour in zip(forecast["hours"], report["hours"], strict=True):
        assert hour["raw_numerical_temperature"] == source_hour["temperature"]
        assert hour["final_temperature"] == source_hour["temperature"]
        assert hour["raw_display_temperature"] == {"value": pytest.approx(37.4), "unit": "degF"}
        assert hour["final_display_temperature"] == hour["raw_display_temperature"]
        for source in source_hour["sources"] + source_hour["shadow_sources"]:
            contributor = hour["contributors"][source["model"]]
            assert {key: contributor[key] for key in source} == source
        assert hour["contributors"]["HRRR"]["display_temperature"]["value"] == pytest.approx(32)
        assert hour["contributors"]["GFS"]["display_temperature"]["value"] == pytest.approx(50)
        assert hour["contributors"]["RAP"]["display_temperature"]["value"] == pytest.approx(68)
    assert report["hours"][0]["contributors"]["HRRR"]["provenance_ref"] == "#/hours/0/sources/0"
    assert report["hours"][0]["contributors"]["RAP"]["role"] == "shadow"
    # The report itself can be modified without mutating the saved numerical payload.
    report["hours"][0]["raw_numerical_temperature"]["value"] = 0
    report["hours"][0]["contributors"]["HRRR"]["acquisition"]["retrieved_at"] = "changed"
    assert forecast == original
    assert report["hours"][0]["final_temperature"] == original["hours"][0]["temperature"]


def test_unimplemented_stages_are_explicit_and_do_not_create_adjustments(
    forecast: dict[str, Any],
) -> None:
    for hour in build_hourly_report(forecast)["hours"]:
        assert hour["bias_correction"]["status"] == "not_implemented"
        assert hour["bias_correction"]["applied_delta"] == {"value": 0.0, "unit": "K"}
        assert hour["ai_adjustment"]["action"] == "not_run"
        assert hour["ai_adjustment"]["applied_delta"] == {"value": 0.0, "unit": "K"}
        assert "not implemented" in hour["ai_adjustment"]["reason"]
        assert hour["delivery_status"] == "not_delivered"
        assert hour["verification"]["status"] == "not_yet_verified"


def test_missing_active_temperature_and_native_ifs_gaps_stay_missing(
    forecast: dict[str, Any],
) -> None:
    forecast["hours"][0]["temperature"]["value"] = None
    forecast["hours"][0]["missing_reasons"] = ["HRRR: prepared input is missing"]
    forecast["hours"][0]["sources"][0]["temperature"]["value"] = None
    forecast["hours"][0]["sources"][0]["missing_reasons"] = forecast["hours"][0]["missing_reasons"]
    report = build_hourly_report(forecast)
    first = report["hours"][0]
    assert first["raw_numerical_temperature"]["value"] is None
    assert first["final_temperature"]["value"] is None
    assert first["final_display_temperature"]["value"] is None
    assert first["missing_reasons"] == ["HRRR: prepared input is missing"]
    assert first["contributors"]["GFS"]["weight"] == 0.3
    assert first["contributors"]["IFS"]["temperature"]["value"] is None
    assert first["contributors"]["IFS"]["missing_reasons"] == ["IFS: no native valid time"]
    available_ifs = report["hours"][2]["contributors"]["IFS"]["display_temperature"]
    assert available_ifs["value"] == pytest.approx(86)
    rendered = render_hourly_report(report)
    assert "Hour 1 HRRR: HRRR: prepared input is missing" in rendered
    assert "Hour 1 IFS: IFS: no native valid time" in rendered


def test_utc_default_and_display_timezone_preserve_dst_offsets(forecast: dict[str, Any]) -> None:
    utc = build_hourly_report(forecast)
    assert utc["display_timezone"] == "UTC"
    assert utc["hours"][0]["valid_time_utc"] == "2026-11-01T05:00:00Z"
    assert utc["hours"][0]["valid_time_local"] == "2026-11-01T05:00:00+00:00"
    local = build_hourly_report(forecast, display_timezone="America/Chicago")
    assert local["hours"][1]["valid_time_local"] == "2026-11-01T01:00:00-05:00"
    assert local["hours"][2]["valid_time_local"] == "2026-11-01T01:00:00-06:00"
    assert local["hours"][1]["valid_time_utc"] == "2026-11-01T06:00:00Z"
    assert local["hours"][2]["valid_time_utc"] == "2026-11-01T07:00:00Z"


def test_renderer_includes_all_36_hours_and_stage_explanations(forecast: dict[str, Any]) -> None:
    report = build_hourly_report(forecast, display_timezone="America/Chicago")
    rendered = render_hourly_report(report)
    rows = [line for line in rendered.splitlines() if line.startswith("| ")]
    assert len(rows) == 38  # Header, separator, then every forecast hour.
    for hour in report["hours"]:
        assert f"| {hour['horizon_hours']} | {hour['valid_time_utc']} |" in rendered
        assert hour["valid_time_local"] in rendered
    assert "not inferred from forecast coordinates" in rendered
    assert "Synthetic demonstration data" in rendered
    assert "not implemented yet" in rendered
    assert "delivery has not run" in rendered
    assert "| 0.0 | not_run / 0.0 | 37.4 | not_yet_verified |" in rendered


def test_wrong_units_and_naive_times_are_not_mislabeled(forecast: dict[str, Any]) -> None:
    forecast["hours"][0]["temperature"]["unit"] = "degC"
    with pytest.raises(ValueError, match="canonical Kelvin"):
        build_hourly_report(forecast)
    forecast["hours"][0]["temperature"]["unit"] = "K"
    forecast["hours"][0]["valid_time"] = "2026-11-01T05:00:00"
    with pytest.raises(ValueError, match="UTC offset"):
        build_hourly_report(forecast)
