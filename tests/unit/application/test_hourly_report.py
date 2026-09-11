"""The hourly presentation preserves the numerical baseline and unrun stages."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mesoforge.application.hourly_report import build_hourly_report, render_hourly_report


def test_surface_report_preserves_baseline_and_displays_convenient_units(forecast):
    fields = {
        "air_temperature_2m": {"value": 273.15, "unit": "K", "missing_reasons": []},
        "dew_point_temperature_2m": {"value": 263.15, "unit": "K", "missing_reasons": []},
        "relative_humidity_2m": {"value": 46.0, "unit": "percent", "missing_reasons": []},
        "wind_speed_10m": {"value": 4.4704, "unit": "m/s", "missing_reasons": []},
        "wind_from_direction_10m": {"value": 359.0, "unit": "degree", "missing_reasons": []},
        "wind_gust_10m": {"value": None, "unit": "m/s", "missing_reasons": ["missing gust"]},
    }
    for hour in forecast["hours"]:
        hour["surface"] = {
            "fields": deepcopy(fields),
            "contributors": {
                "HRRR": {
                    "fields": deepcopy(fields),
                    "cycle": "2026-11-01T00:00:00Z",
                    "source_lead_hours": hour["horizon_hours"] + 4,
                    "role": "active",
                }
            },
        }
    original = deepcopy(forecast)
    report = build_hourly_report(forecast, display_timezone="America/Chicago")
    text = render_hourly_report(report)
    assert "Td °F" in text and "RH %" in text and "Gust mph" in text
    assert "32.0 | 14.0 | 46.0 | 10.0 | N (359°) | unavailable" in text
    assert "36 |" in text and "HRRR native contributor" in text
    assert "not_run" in text and "not_implemented" in text
    assert forecast == original
    assert report["hours"][0]["final_surface_fields"] == fields
    report["hours"][0]["final_surface_fields"]["air_temperature_2m"]["value"] = 0
    assert report["hours"][0]["surface"]["fields"] == fields

    # An approved single-model fallback can still produce a value. Its hour must
    # not falsely claim there were no contributor exclusions.
    for hour in forecast["hours"]:
        hour["surface"]["fields"]["wind_gust_10m"].update(
            value=8.0, missing_reasons=[], status="fallback", weights={"HRRR": 0.7, "GFS": 0.3}
        )
    forecast["hours"][0]["surface"]["fields"]["wind_gust_10m"].update(
        weights={"GFS": 1.0}, missing_reasons=["HRRR: coupled wind/gust tuple rejected"]
    )
    rendered = render_hourly_report(build_hourly_report(forecast))
    first = next(line for line in rendered.splitlines() if line.startswith("| 1 |"))
    second = next(line for line in rendered.splitlines() if line.startswith("| 2 |"))
    assert "wind_gust_10m: GFS-only fallback" in first
    assert "none; cloud unavailable" not in first
    assert "none; cloud unavailable" in second
    assert "Hours 1: wind_gust_10m: HRRR: coupled wind/gust tuple rejected" in rendered


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


@pytest.fixture
def qpf_forecast(forecast: dict[str, Any]) -> dict[str, Any]:
    for hour in forecast["hours"]:
        end = datetime.fromisoformat(hour["valid_time"])
        field = {
            "value": 25.4,
            "unit": "kg/m^2",
            "interval_start": (end - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "interval_end": end.isoformat().replace("+00:00", "Z"),
            "missing_reasons": [],
            "weights": {"HRRR": 0.7, "GFS": 0.3},
            "provenance": {"parents": ["sha256:current", "sha256:previous"]},
        }
        hour["surface"] = {
            "fields": {"liquid_equivalent_precipitation_amount_1h": deepcopy(field)},
            "contributors": {
                source["model"]: {
                    "fields": {"liquid_equivalent_precipitation_amount_1h": deepcopy(field)},
                    "cycle": source["cycle"],
                    "source_lead_hours": source["source_lead_hours"],
                    "role": "active",
                }
                for source in hour["sources"]
            },
        }
    return forecast


@pytest.mark.parametrize(
    ("amount", "inches", "text"),
    [
        (25.4, 1.0, "1"),
        (0.254, 0.01, "0.01"),
        (0.0, 0.0, "0"),
        (2.54e-11, 1e-12, "1e-12"),
        (None, None, "unavailable"),
    ],
)
def test_qpf_display_preserves_small_positive_zero_and_missing(
    qpf_forecast: dict[str, Any], amount: float | None, inches: float | None, text: str
) -> None:
    variable = "liquid_equivalent_precipitation_amount_1h"
    field = qpf_forecast["hours"][0]["surface"]["fields"][variable]
    field["value"] = amount
    if amount is None:
        field["missing_reasons"] = ["No exact matching accumulation interval"]
    report = build_hourly_report(qpf_forecast)
    displayed = report["hours"][0]["display_qpf"]
    assert displayed["value"] == (
        None if inches is None else pytest.approx(inches, rel=1e-12, abs=0)
    )
    assert displayed["unit"] == "inch"
    assert displayed["interval_start"] == "2026-11-01T04:00:00Z"
    assert displayed["interval_end"] == "2026-11-01T05:00:00Z"
    assert displayed["interval_closure"] == "left_open_right_closed"
    rendered = render_hourly_report(report)
    first = next(line for line in rendered.splitlines() if line.startswith("| 1 |"))
    assert f"| {text} | 2026-11-01T04:00:00Z | 2026-11-01T05:00:00Z |" in first
    if amount is None:
        assert "No exact matching accumulation interval" in rendered


def test_qpf_report_preserves_native_values_provenance_and_intervals(
    qpf_forecast: dict[str, Any],
) -> None:
    variable = "liquid_equivalent_precipitation_amount_1h"
    native = qpf_forecast["hours"][0]["surface"]["contributors"]["GFS"]["fields"][variable]
    native["value"] = 0.254
    original = deepcopy(qpf_forecast)
    report = build_hourly_report(qpf_forecast)
    rendered = render_hourly_report(report)
    assert qpf_forecast == original
    assert "liquid-equivalent accumulation over (start, end]" in rendered
    assert "precipitation probability or precipitation type" in rendered
    assert rendered.count("QPF in | Accumulation start UTC (exclusive) | End UTC (inclusive)") == 3
    assert len([line for line in rendered.splitlines() if line.startswith("| 36 |")]) == 3
    gfs_table = rendered.split("### GFS native contributor")[1].split("### HRRR")[0]
    assert "| 0.01 | 2026-11-01T04:00:00Z | 2026-11-01T05:00:00Z |" in gfs_table
    for saved, display in zip(qpf_forecast["hours"], report["hours"], strict=True):
        assert display["surface"] == saved["surface"]
        assert display["final_surface_fields"] == saved["surface"]["fields"]
    report["hours"][0]["final_surface_fields"][variable]["provenance"]["parents"].append("edit")
    report["hours"][0]["display_qpf"]["value"] = 999
    assert qpf_forecast == original
    assert report["hours"][0]["surface"] == original["hours"][0]["surface"]


def test_qpf_wrong_units_are_not_silently_reinterpreted(qpf_forecast: dict[str, Any]) -> None:
    field = qpf_forecast["hours"][0]["surface"]["fields"][
        "liquid_equivalent_precipitation_amount_1h"
    ]
    field["unit"] = "inch"
    with pytest.raises(ValueError, match="canonical QPF"):
        build_hourly_report(qpf_forecast)


def test_historical_report_does_not_invent_qpf(forecast: dict[str, Any]) -> None:
    report = build_hourly_report(forecast)
    assert all("display_qpf" not in hour for hour in report["hours"])
    assert "QPF" not in render_hourly_report(report)


@pytest.fixture
def pop_forecast(qpf_forecast: dict[str, Any]) -> dict[str, Any]:
    for hour in qpf_forecast["hours"]:
        qpf = hour["surface"]["fields"]["liquid_equivalent_precipitation_amount_1h"]
        pop = {
            "value": 0.4,
            "unit": "1",
            "temporal_semantics": "probability",
            "interval_start": qpf["interval_start"],
            "interval_end": qpf["interval_end"],
            "interval_closure": "left_open_right_closed",
            "threshold": {"value": 0.254, "unit": "kg/m^2", "comparison": "gt"},
            "status": "available",
            "weights": {"NBM": 1.0},
            "missing_reasons": [],
            "provenance": {"raw_sha256": "nbm-fixture-hash"},
            "source_cycle": "2026-11-01T03:00:00Z",
            "source_lead_hours": hour["horizon_hours"] + 1,
        }
        hour["surface"]["fields"]["probability_of_precipitation_1h"] = pop
        hour["surface"]["contributors"]["NBM"] = {
            "fields": {"probability_of_precipitation_1h": deepcopy(pop)},
            "role": "field_source",
        }
    return qpf_forecast


@pytest.mark.parametrize(
    "fraction,percent,text",
    [
        (0.0, 0.0, "0"),
        (0.450001, 45.0001, "45.0001"),
        (1.0, 100.0, "100"),
        (None, None, "unavailable"),
    ],
)
def test_pop_percentage_keeps_exact_native_event_and_zero_missing_distinction(
    pop_forecast, fraction, percent, text
):
    field = pop_forecast["hours"][0]["surface"]["fields"]["probability_of_precipitation_1h"]
    field["value"] = fraction
    if fraction is None:
        field["missing_reasons"] = ["No native probability for this exact interval"]
    report = build_hourly_report(pop_forecast)
    display = report["hours"][0]["display_pop"]
    assert display["value"] == (pytest.approx(percent) if percent is not None else None)
    assert display["unit"] == "percent"
    assert display["interval_start"] == "2026-11-01T04:00:00Z"
    assert display["interval_end"] == "2026-11-01T05:00:00Z"
    assert display["threshold"] == {"value": 0.254, "unit": "kg/m^2", "comparison": "gt"}
    assert display["interval_closure"] == "left_open_right_closed"
    rendered = render_hourly_report(report)
    first = next(line for line in rendered.splitlines() if line.startswith("| 1 |"))
    assert f"| {text} | 2026-11-01T04:00:00Z | 2026-11-01T05:00:00Z |" in first
    if fraction is None:
        assert "No native probability for this exact interval" in rendered


def test_pop_report_has_nbm_probability_only_and_preserves_qpf_and_original_evidence(pop_forecast):
    original = deepcopy(pop_forecast)
    report = build_hourly_report(pop_forecast)
    rendered = render_hourly_report(report)
    assert "strictly greater than 0.254 kg/m² (0.01 inch)" in rendered
    assert "no new probability windows are synthesized" in rendered
    assert "NBM-only fallback" not in rendered
    nbm_table = rendered.split("### NBM native contributor")[1].split("Explicit missingness")[0]
    assert "Native-period PoP %" in nbm_table
    assert "cycle 2026-11-01T03:00:00Z" in nbm_table
    assert "| 1 | 2 | 40 |" in nbm_table
    assert "T °F" not in nbm_table and "Gust mph" not in nbm_table and "QPF in" not in nbm_table
    assert len([row for row in nbm_table.splitlines() if row.startswith("| ")]) == 38
    for saved, hour in zip(original["hours"], report["hours"], strict=True):
        assert hour["surface"] == saved["surface"]
        assert hour["final_surface_fields"] == saved["surface"]["fields"]
        assert hour["display_qpf"]["value"] == 1.0
    report["hours"][0]["display_pop"]["threshold"]["value"] = 0.5
    report["hours"][0]["surface"]["contributors"]["NBM"]["fields"][
        "probability_of_precipitation_1h"
    ]["provenance"].clear()
    assert pop_forecast == original


def test_historical_qpf_report_does_not_invent_probability(qpf_forecast):
    report = build_hourly_report(qpf_forecast)
    assert all("display_pop" not in hour for hour in report["hours"])
    assert "Native-period PoP %" not in render_hourly_report(report)


@pytest.mark.parametrize("unit,value", [("percent", 40), ("1", 1.1), ("1", -0.1)])
def test_pop_report_rejects_invalid_units_or_probability_range(pop_forecast, unit, value):
    pop_forecast["hours"][0]["surface"]["fields"]["probability_of_precipitation_1h"].update(
        unit=unit, value=value
    )
    with pytest.raises(ValueError, match="PoP"):
        build_hourly_report(pop_forecast)


@pytest.fixture
def native_probability_forecast(pop_forecast):
    sources = {"NBM_6H": (6, 6, 0.6), "GEFS_6H": (6, 6, 0.35), "ECMWF_ENS_24H": (24, 24, 0.0)}
    for hour in pop_forecast["hours"]:
        rows = []
        for source_id, (end_hour, duration, value) in sources.items():
            row = {
                "source_id": source_id,
                "event_id": None,
                "value": None,
                "unit": "1",
                "role": "shadow",
                "active_weight": 0.0,
                "status": "unavailable",
                "missing_reasons": ["No native interval ends at this hour; no temporal filling"],
            }
            if hour["horizon_hours"] == end_hour:
                end = datetime.fromisoformat(hour["valid_time"])
                row.update(
                    event_id=f"{source_id}:{end_hour}",
                    value=value,
                    interval_start=(end - timedelta(hours=duration)).isoformat(),
                    interval_end=end.isoformat(),
                    interval_closure="left_open_right_closed",
                    threshold={
                        "value": 1.0 if duration == 24 else 0.254,
                        "unit": "kg/m^2",
                        "comparison": "ge" if duration == 24 else "gt",
                    },
                    spatial_support={"kind": "grid_point"},
                    source_cycle="2026-11-01T00:00:00Z",
                    source_lead_hours=end_hour + 4,
                    status="available",
                    missing_reasons=[],
                    provenance={"raw_sha256": source_id + "-fixture"},
                )
            rows.append(row)
        comparisons = []
        for row in rows:
            comparisons.append(
                {
                    "left": {"source_id": "NBM_HOURLY_CONTROL", "event_id": "hourly_control"},
                    "right": {key: row[key] for key in ("source_id", "event_id")},
                    "status": "incompatible",
                    "delta": None,
                    "unit": "1",
                    "reasons": ["Different native accumulation interval or interval closure"],
                }
            )
        if hour["horizon_hours"] == 6:
            comparisons.append(
                {
                    "left": {key: rows[0][key] for key in ("source_id", "event_id")},
                    "right": {key: rows[1][key] for key in ("source_id", "event_id")},
                    "status": "comparable",
                    "delta": 0.25,
                    "unit": "1",
                    "reasons": [],
                }
            )
        hour["surface"]["probability_guidance"] = {"contributors": rows, "comparisons": comparisons}
    return pop_forecast


def test_probability_shadows_keep_native_periods_and_only_display_comparable_differences(
    native_probability_forecast,
):
    original = deepcopy(native_probability_forecast)
    baseline = deepcopy(original)
    for hour in baseline["hours"]:
        del hour["surface"]["probability_guidance"]
    report = build_hourly_report(native_probability_forecast)
    rendered = render_hourly_report(report)
    prefix, shadow = rendered.split("### Native-period probability shadows")
    assert prefix.strip() == render_hourly_report(build_hourly_report(baseline)).strip()
    assert "sole active probability source with weight 1.0" in prefix
    assert "QPF in" in prefix
    assert "zero active weight" in shadow
    assert "six-hour and 24-hour probabilities are not hourly probabilities" in shadow
    assert "| 6 | NBM_6H / NBM_6H:6 |" in shadow
    assert "2026-11-01T04:00:00+00:00 | 2026-11-01T10:00:00+00:00" in shadow
    assert "| > 0.254 kg/m^2 | kind=grid_point | 60 | available |" in shadow
    assert "| > 0.254 kg/m^2 | kind=grid_point | 35 | available |" in shadow
    assert "| ≥ 1 kg/m^2 | kind=grid_point | 0 | available |" in shadow
    assert "| 6 | NBM_6H / NBM_6H:6 minus GEFS_6H / GEFS_6H:6 | +25 |" in shadow
    assert "not verification scores" in shadow
    assert "Hours 1–5, 7–36: NBM_6H: No native interval ends at this hour" in shadow
    assert "Hours 1–23, 25–36: ECMWF_ENS_24H:" in shadow
    assert (
        "Hours 24: NBM_HOURLY_CONTROL / hourly_control minus "
        "ECMWF_ENS_24H / ECMWF_ENS_24H:24: incompatible" in shadow
    )
    assert "Different native accumulation interval" in shadow
    assert len([line for line in shadow.splitlines() if line.startswith("| 6 |")]) == 3
    assert native_probability_forecast == original
    for source, shown in zip(original["hours"], report["hours"], strict=True):
        assert shown["surface"] == source["surface"]
        assert shown["display_pop"]["value"] == 40
        assert shown["display_qpf"]["value"] == 1
    report["hours"][5]["surface"]["probability_guidance"]["contributors"][0]["provenance"].clear()
    assert native_probability_forecast == original


@pytest.mark.parametrize("unit,value", [("percent", 40), ("1", 1.1), ("1", -0.1)])
def test_native_shadow_probability_report_does_not_mislabel_invalid_values(
    native_probability_forecast, unit, value
):
    source = native_probability_forecast["hours"][5]["surface"]["probability_guidance"][
        "contributors"
    ][0]
    source.update(unit=unit, value=value)
    with pytest.raises(ValueError, match="PoP"):
        render_hourly_report(build_hourly_report(native_probability_forecast))
