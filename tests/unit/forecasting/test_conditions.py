"""Condition preview consumes an immutable field canvas without inventing new weather."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import pytest

from mesoforge.application.cloud_cover import extract_cloud_contributors
from mesoforge.application.local_surface_grid import (
    SurfaceGridGeometry,
    build_local_surface_grid,
    extract_grid_point,
)
from mesoforge.application.precipitation_type import POLICY as TYPE_POLICY
from mesoforge.common.errors import IntegrityError
from mesoforge.forecasting.cloud_cover import CLOUD, CLOUD_ACTIVE_POLICY, SKY_CATEGORY_POLICY
from mesoforge.forecasting.conditions import (
    ConditionsPreviewUnavailableError,
    build_conditions_preview,
)
from mesoforge.forecasting.thunder import ACTIVE_POLICY as THUNDER_POLICY
from mesoforge.guidance.sources.thunder import SOURCES as THUNDER_SOURCES
from mesoforge.storage.json import CanonicalJsonSerializer
from tests.unit.application.test_cloud_cover import cloud_view

LATITUDE, LONGITUDE = 44.98859, -93.25557
TARGET = "2026-09-11T12:00:00Z"
QPF = "liquid_equivalent_precipitation_amount_1h"
POP = "probability_of_precipitation_1h"
PTYPE = "precipitation_type"
THUNDER = "probability_of_thunder_1h"
FIELD_NAMES = {
    "temperature": "air_temperature_2m",
    "dew_point": "dew_point_temperature_2m",
    "relative_humidity": "relative_humidity_2m",
    "wind_u": "eastward_wind_10m",
    "wind_v": "northward_wind_10m",
    "wind_speed": "wind_speed_10m",
    "wind_direction": "wind_from_direction_10m",
    "wind_gust": "wind_gust_10m",
    "qpf": QPF,
    "pop": POP,
    "precipitation_type": PTYPE,
    "thunder": THUNDER,
}
JSON = CanonicalJsonSerializer()


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _hour(horizon):
    cycle = datetime.fromisoformat(TARGET)
    end = cycle + timedelta(hours=horizon)
    valid, start = _iso(end), _iso(end - timedelta(hours=1))
    weights = {"HRRR": 0.7, "GFS": 0.3} if horizon <= 18 else {"HRRR": 0.6, "GFS": 0.4}

    def scalar(value, unit, policy, weights=None):
        return {
            "value": value,
            "unit": unit,
            "status": "available",
            "missing_reasons": [],
            "policy": policy,
            "weights": weights or {},
        }

    fields = {
        "air_temperature_2m": scalar(280.1, "K", "unchanged-temperature-control"),
        "dew_point_temperature_2m": scalar(274.1, "K", "phase2-scalar-vector-fallback.v1", weights),
        "relative_humidity_2m": scalar(
            65.4321, "%", "bolton-1980-relative-humidity-liquid-water.v1"
        ),
        "eastward_wind_10m": scalar(3.0, "m/s", "phase2-scalar-vector-fallback.v1", weights),
        "northward_wind_10m": scalar(4.0, "m/s", "phase2-scalar-vector-fallback.v1", weights),
        "wind_speed_10m": scalar(5.0, "m/s", "phase2-scalar-vector-fallback.v1", weights),
        "wind_from_direction_10m": scalar(
            216.86989764584402, "degree", "phase2-scalar-vector-fallback.v1", weights
        ),
        "wind_gust_10m": scalar(7.0, "m/s", "phase2-scalar-vector-fallback.v1", weights),
        QPF: {
            **scalar(0.127, "kg/m^2", "phase2-qpf-fallback.v1", weights),
            "temporal_semantics": "accumulation",
            "interval_start": start,
            "interval_end": valid,
            "interval_closure": "left_open_right_closed",
        },
        POP: {
            **scalar(
                0.4,
                "1",
                {"schema_version": "pop-blend-policy.v1", "sole_contributor": "NBM", "weight": 1.0},
                {"NBM": 1.0},
            ),
            "model": "NBM",
            "source_cycle": TARGET,
            "source_lead_hours": horizon,
            "temporal_semantics": "probability",
            "interval_start": start,
            "interval_end": valid,
            "interval_closure": "left_open_right_closed",
            "threshold": {"value": 0.254, "unit": "kg/m^2", "comparison": "gt"},
            "spatial_support": {"kind": "grid_point"},
            "provenance": {"manifest_sha256": "a" * 64, "prepared_sha256": "b" * 64},
        },
        PTYPE: {
            "value": "rain",
            "unit": "category",
            "status": "interim_baseline",
            "valid_time": valid,
            "temporal_semantics": "instantaneous",
            "interval_start": None,
            "interval_end": None,
            "supported_types": ["rain"],
            "policy": deepcopy(TYPE_POLICY),
            "missing_reasons": [],
            "contributor_disagreement": False,
        },
        THUNDER: {
            **deepcopy(THUNDER_SOURCES["NBM_1H"]),
            "value": 0.2,
            "native_value": 20.0,
            "unit": "1",
            "status": "available",
            "source_cycle": TARGET,
            "source_lead_hours": horizon,
            "valid_time": valid,
            "interval_start": start,
            "interval_end": valid,
            "interval_closure": "left_open_right_closed",
            "temporal_semantics": "interval_probability",
            "policy": deepcopy(THUNDER_POLICY),
            "weights": {"NBM": 1.0},
            "missing_reasons": [],
        },
    }
    fields["relative_humidity_2m"].update(
        saturation_reference="liquid_water",
        derived_from=["air_temperature_2m", "dew_point_temperature_2m"],
    )
    return {
        "horizon_hours": horizon,
        "valid_time": valid,
        "temperature": {"value": 280.1, "unit": "K"},
        "sources": [
            {
                "model": model,
                "cycle": TARGET,
                "source_lead_hours": horizon,
                "weight": weight,
                "temperature": {"value": value, "unit": "K"},
                "missing_reasons": [],
            }
            for model, weight, value in (("HRRR", 0.7, 281.0), ("GFS", 0.3, 278.0))
        ],
        "missing_reasons": [],
        "surface": {"fields": fields, "contributors": {}},
    }


@lru_cache(maxsize=1)
def _saved_template():
    hours = [_hour(h) for h in range(1, 37)]

    def column(*, latitude, longitude):
        return {
            "latitude": latitude,
            "longitude": longitude,
            "target_reference_time": TARGET,
            "data_kind": "synthetic_demonstration",
            "notice": "Synthetic condition-test fixture",
            "hours": hours,
        }

    grid = build_local_surface_grid(
        latitude=LATITUDE,
        longitude=LONGITUDE,
        calculate_column=column,
        geometry=SurfaceGridGeometry(
            context_half_width_cells=2, editable_half_width_cells=1, spacing_m=3000.0
        ),
    )
    forecast = extract_grid_point(grid, latitude=LATITUDE, longitude=LONGITUDE)
    return {
        "schema_version": "issued-forecast.v1",
        "issued_forecast_id": "ed771bc5-c8da-4371-99bc-cf59d28e8ad8",
        "batch_run_id": "0467e1fd-59f5-41ec-af77-13e493f16eac",
        "location_index": 0,
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "issued_at": TARGET,
        "target_reference_time": TARGET,
        "code_identity": {"git_commit": "c" * 40},
        "forecast": forecast,
    }


def saved_forecast():
    """Small valid current grid, reusable by read/API tests; no model files or provider calls."""
    return deepcopy(_saved_template())


def center_hour(saved, index=0):
    return next(
        c for c in saved["forecast"]["local_grid_baseline"]["cells"] if c["is_forecast_point"]
    )["hours"][index]


def refresh_saved_grid(saved):
    """Reseal a deliberately changed synthetic grid fixture; never used on runtime artifacts."""
    grid = saved["forecast"]["local_grid_baseline"]
    saved["forecast"] = extract_grid_point(grid, latitude=LATITUDE, longitude=LONGITUDE)
    return saved


def preview_hour(saved, index=0):
    return build_conditions_preview(saved)["center_point"]["hours"][index]


def active_sky_field(percentage, valid_time, *, latitude=LATITUDE, longitude=LONGITUDE):
    """Reuse native cloud evidence fixtures and the approved active-field construction."""
    view = cloud_view("NBM", amount=percentage, end=valid_time)
    view.dataset.coords.update({"x": [-94.0, -92.0], "y": [44.0, 46.0]})
    return extract_cloud_contributors(
        [view], latitude=latitude, longitude=longitude, valid_time=valid_time
    )["field"]


@pytest.mark.parametrize(
    "percentage,category",
    [(0.0, "clear"), (25.0, "mostly_clear"), (25.0000001, "partly_cloudy"), (87.0000001, "cloudy")],
)
def test_active_sky_uses_unrounded_nbm_percentage_and_existing_categories(percentage, category):
    saved = saved_forecast()
    hour = center_hour(saved)
    field = active_sky_field(percentage, hour["valid_time"])
    hour["surface"]["fields"][CLOUD] = field
    refresh_saved_grid(saved)
    output = preview_hour(saved)
    sky = output["components"]["sky"]
    assert sky["state"] == "known" and sky["value"] == field["value"]
    assert sky["value"] == pytest.approx(percentage / 100)
    assert sky["unit"] == "1" and sky["cloud_percentage"] == field["cloud_percentage"]
    assert sky["sky_category"] == category and sky["sky_category_policy"] == SKY_CATEGORY_POLICY
    assert sky["source_policy"] == CLOUD_ACTIVE_POLICY and sky["weights"] == {"NBM": 1.0}
    for key in (
        "provenance",
        "manifest_sha256",
        "prepared_file",
        "source_cycle",
        "source_lead_hours",
        "spatial_extraction",
    ):
        assert sky[key] == field[key]
    assert sky["evidence_refs"][0].endswith("/surface/fields/cloud_area_fraction")
    assert output["rendering"]["text"].startswith(f"Sky {category.replace('_', ' ')} ")
    assert output["components"]["occurrence"]["state"] == "unavailable"
    assert "dry" not in output["rendering"]["text"].lower()


@pytest.mark.parametrize(
    "change",
    [
        {"status": "unavailable", "missing_reasons": ["NBM guidance missing"]},
        {"model": "HRRR"},
        {"weights": {"NBM": 0.5, "HRRR": 0.5}},
        {"unit": "percent"},
        {"cloud_definition": "low_cloud_cover"},
        {"valid_time": "2026-09-11T14:00:00Z"},
        {"sky_category": "clear"},
    ],
)
def test_ineligible_active_sky_never_falls_back_to_native_shadow_evidence(change):
    saved = saved_forecast()
    hour = center_hour(saved)
    field = active_sky_field(80.0, hour["valid_time"])
    field.update(change)
    hour["surface"]["fields"][CLOUD] = field
    hour["surface"]["cloud_guidance"] = {
        "contributors": [
            {
                "model": "HRRR",
                "value": 0.0,
                "unit": "percent",
                "sky_category": "clear",
                "status": "available",
                "active_weight": 0.0,
            }
        ]
    }
    refresh_saved_grid(saved)
    output = preview_hour(saved)
    sky = output["components"]["sky"]
    assert sky["state"] == "unavailable" and sky["value"] is None
    assert sky["cloud_percentage"] is None and sky["sky_category"] is None
    assert sky["reasons"] and sky["evidence_refs"]
    assert "Sky " not in output["rendering"]["text"]


def test_active_sky_spans_saved_grid_and_preserves_other_components_and_original_payload():
    saved = saved_forecast()
    previous = build_conditions_preview(saved)
    for cell in saved["forecast"]["local_grid_baseline"]["cells"]:
        for hour in cell["hours"]:
            percentage = float(
                (7 * cell["x_index"] + 11 * cell["y_index"] + 3 * hour["horizon_hours"]) % 101
            )
            hour["surface"]["fields"][CLOUD] = active_sky_field(
                percentage,
                hour["valid_time"],
                latitude=cell["latitude"],
                longitude=cell["longitude"],
            )
    refresh_saved_grid(saved)
    original = JSON.serialize(saved)
    output = build_conditions_preview(saved)
    assert JSON.serialize(output) == JSON.serialize(
        build_conditions_preview(JSON.deserialize(original))
    )
    assert JSON.serialize(saved) == original
    assert {cell["context_only"] for cell in output["cells"]} == {False, True}
    for cell, old in zip(output["cells"], previous["cells"], strict=True):
        assert len(cell["hours"]) == 36
        for hour, old_hour in zip(cell["hours"], old["hours"], strict=True):
            assert hour["components"]["sky"]["state"] == "known"
            for name, component in old_hour["components"].items():
                if name != "sky":
                    assert hour["components"][name] == component
            assert hour["rendering"]["text"].split("; ", 1)[1] == old_hour["rendering"]["text"]
    center = next(cell for cell in output["cells"] if cell["is_forecast_point"])
    assert output["center_point"]["hours"] == center["hours"]
    unavailable = saved["forecast"]["local_grid_baseline"]["cells"][0]
    unavailable.update(status="unavailable", missing_reasons=["No spatial coverage"])
    refresh_saved_grid(saved)
    missing = build_conditions_preview(saved)["cells"][0]["hours"][0]
    assert missing["components"]["sky"]["state"] == "unavailable"
    assert missing["components"]["sky"]["sky_category"] is None
    assert missing["components"]["sky"]["cloud_percentage"] is None
    assert missing["rendering"]["text"] == "Weather conditions unavailable."


def test_active_numerical_fields_and_exact_native_event_metadata_are_preserved():
    saved = saved_forecast()
    result = build_conditions_preview(saved)
    original = center_hour(saved)
    hour = result["center_point"]["hours"][0]
    assert result["input"]["issued_forecast_id"] == saved["issued_forecast_id"]
    for component, field in FIELD_NAMES.items():
        output = hour["components"][component]
        assert output["state"] == "known", (component, output)
        assert output["value"] == original["surface"]["fields"][field]["value"]
        assert output["unit"] == original["surface"]["fields"][field]["unit"]
        assert output["source_policy"] == original["surface"]["fields"][field]["policy"]
        assert output["evidence_refs"]
    probability = hour["components"]["pop"]
    assert probability["threshold"] == {"value": 0.254, "unit": "kg/m^2", "comparison": "gt"}
    assert probability["interval"] == {
        "start": TARGET,
        "end": "2026-09-11T13:00:00Z",
        "closure": "left_open_right_closed",
    }
    assert probability["spatial_support"] == {"kind": "grid_point"}
    assert hour["components"]["precipitation_type"]["interval"] is None
    thunder = hour["components"]["thunder"]
    assert thunder["event_definition"]["physical_threshold"] is None
    assert thunder["spatial_support"]["radius_km"] is None
    for component in ("occurrence", "intensity", "fog", "wind_descriptor", "transitions"):
        assert hour["components"][component]["state"] == "unavailable"


def test_all_cells_and_36_hours_share_the_same_preview_path_and_exact_center_result():
    saved = saved_forecast()
    # A context-only cell has genuinely different retained temperature; no center duplication.
    saved["forecast"]["local_grid_baseline"]["cells"][0]["hours"][0]["temperature"]["value"] = 281.2
    saved["forecast"]["local_grid_baseline"]["cells"][0]["hours"][0]["surface"]["fields"][
        "air_temperature_2m"
    ]["value"] = 281.2
    refresh_saved_grid(saved)
    preview = build_conditions_preview(saved)
    assert len(preview["cells"]) == 25
    assert all(
        [h["horizon_hours"] for h in c["hours"]] == list(range(1, 37)) for c in preview["cells"]
    )
    assert {c["context_only"] for c in preview["cells"]} == {False, True}
    center = next(c for c in preview["cells"] if c["is_forecast_point"])
    assert preview["center_point"]["hours"] == center["hours"]
    assert preview["cells"][0]["hours"][0]["components"]["temperature"]["value"] == 281.2
    assert center["hours"][0]["components"]["temperature"]["value"] == 280.1
    assert preview["geometry"] == saved["forecast"]["local_grid_baseline"]["geometry"]


@pytest.mark.parametrize(
    "component,field,value", [("qpf", QPF, 0.0), ("pop", POP, 0.0), ("thunder", THUNDER, 0.0)]
)
def test_zero_event_or_amount_remains_known_and_is_not_a_dry_weather_assertion(
    component, field, value
):
    saved = saved_forecast()
    center_hour(saved)["surface"]["fields"][field]["value"] = value
    refresh_saved_grid(saved)
    hour = preview_hour(saved)
    assert hour["components"][component]["state"] == "known"
    assert hour["components"][component]["value"] == 0
    assert hour["components"]["occurrence"]["state"] == "unavailable"
    assert "dry" not in hour["rendering"]["text"].lower()


@pytest.mark.parametrize(
    "value,status,types",
    [
        ("unknown", "unknown", []),
        ("ambiguous", "ambiguous", ["rain", "snow"]),
        ("mixed", "interim_baseline", ["rain", "snow"]),
        ("unavailable", "unavailable", []),
    ],
)
def test_type_unknown_ambiguous_mixed_and_unavailable_are_distinct(value, status, types):
    saved = saved_forecast()
    field = center_hour(saved)["surface"]["fields"][PTYPE]
    field.update(
        value=value,
        status=status,
        supported_types=types,
        missing_reasons=[] if value == "mixed" else ["Original native type state"],
    )
    refresh_saved_grid(saved)
    output = preview_hour(saved)["components"]["precipitation_type"]
    assert output["state"] == ("known" if value == "mixed" else status)
    assert output["value"] == value
    assert output["supported_types"] == types


def test_calm_direction_is_not_applicable_only_when_eligible_components_confirm_zero_wind():
    saved = saved_forecast()
    fields = center_hour(saved)["surface"]["fields"]
    for field in ("eastward_wind_10m", "northward_wind_10m", "wind_speed_10m"):
        fields[field]["value"] = 0.0
    fields["wind_from_direction_10m"].update(
        value=None,
        status="unavailable",
        missing_reasons=["wind direction undefined for exactly calm blended wind"],
    )
    refresh_saved_grid(saved)
    assert preview_hour(saved)["components"]["wind_direction"]["state"] == "not_applicable"
    fields = center_hour(saved)["surface"]["fields"]
    fields["eastward_wind_10m"].update(
        value=None, status="unavailable", missing_reasons=["No native U"]
    )
    refresh_saved_grid(saved)
    assert preview_hour(saved)["components"]["wind_direction"]["state"] == "unavailable"


@pytest.mark.parametrize(
    "component,field,value,unit",
    [
        ("sky", "cloud_area_fraction", 0.8, "1"),
        ("visibility", "visibility", 500.0, "m"),
        ("swe", "snowfall_water_equivalent_amount", 5.0, "kg/m^2"),
        ("snowfall", "snowfall_amount", 0.05, "m"),
        ("freezing_rain_liquid", "freezing_rain_liquid_equivalent_amount", 1.0, "kg/m^2"),
        ("flat_ice", "flat_ice_accretion_mass_equivalent", 1.0, "kg/m^2"),
    ],
)
def test_evidence_only_values_cannot_become_active_conditions(component, field, value, unit):
    saved = saved_forecast()
    surface = center_hour(saved)["surface"]
    surface["fields"][field] = {
        "value": value,
        "unit": unit,
        "status": "available",
        "weights": {"NBM": 1.0},
        "policy": "unapproved-shadow-promotion",
        "missing_reasons": [],
    }
    surface["cloud_guidance"] = {
        "contributors": [
            {"model": "NBM", "value": 80.0, "sky_category": "mostly_cloudy", "active_weight": 0.0}
        ]
    }
    refresh_saved_grid(saved)
    hour = preview_hour(saved)
    assert hour["components"][component]["state"] == "unavailable"
    assert hour["components"][component]["value"] is None
    assert "mostly cloudy" not in hour["rendering"]["text"].lower()
    assert "fog" not in hour["rendering"]["text"].lower()


@pytest.mark.parametrize(
    "component,field,change",
    [
        ("temperature", "air_temperature_2m", {"policy": "new_unapproved_temperature"}),
        ("dew_point", "dew_point_temperature_2m", {"weights": {"HRRR": 0.5, "GFS": 0.5}}),
        ("qpf", QPF, {"policy": "new_qpf_weights"}),
        ("pop", POP, {"model": "GEFS"}),
        ("pop", POP, {"weights": {"NBM": 0.5, "GEFS": 0.5}}),
        ("precipitation_type", PTYPE, {"policy": {"id": "nbm_argmax"}}),
        ("thunder", THUNDER, {"source_id": "NBM_3H"}),
    ],
)
def test_source_or_blend_policy_mismatch_does_not_invent_a_replacement(component, field, change):
    saved = saved_forecast()
    center_hour(saved)["surface"]["fields"][field].update(change)
    refresh_saved_grid(saved)
    output = preview_hour(saved)["components"][component]
    assert output["state"] == "unavailable" and output["value"] is None
    assert output["reasons"]


@pytest.mark.parametrize(
    "component,field,change",
    [
        ("pop", POP, {"value": 1.0001}),
        ("pop", POP, {"value": -0.0001}),
        ("pop", POP, {"unit": "percent"}),
        ("qpf", QPF, {"value": -0.01}),
        ("qpf", QPF, {"unit": "inch"}),
        ("relative_humidity", "relative_humidity_2m", {"value": 100.01}),
        (
            "temperature",
            "air_temperature_2m",
            {"value": None, "status": "unavailable", "missing_reasons": ["No required guidance"]},
        ),
        ("wind_speed", "wind_speed_10m", {"value": -1.0}),
    ],
)
def test_invalid_units_ranges_and_missing_values_are_explicit_without_repair(
    component, field, change
):
    saved = saved_forecast()
    center_hour(saved)["surface"]["fields"][field].update(change)
    refresh_saved_grid(saved)
    output = preview_hour(saved)["components"][component]
    assert output["state"] == "unavailable" and output["value"] is None
    assert output["reasons"]


@pytest.mark.parametrize(
    "field,component,change",
    [
        (POP, "pop", {"threshold": {"value": 0.254, "unit": "kg/m^2", "comparison": "ge"}}),
        (POP, "pop", {"interval_start": "2026-09-11T07:00:00Z"}),
        (QPF, "qpf", {"interval_end": "2026-09-11T14:00:00Z"}),
        (QPF, "qpf", {"temporal_semantics": "instantaneous"}),
        (
            PTYPE,
            "precipitation_type",
            {"interval_start": TARGET, "interval_end": "2026-09-11T13:00:00Z"},
        ),
        (PTYPE, "precipitation_type", {"valid_time": "2026-09-11T14:00:00Z"}),
        (THUNDER, "thunder", {"interval_start": "2026-09-11T10:00:00Z", "duration_hours": 3}),
    ],
)
def test_matching_hour_index_never_overrides_exact_event_or_time_semantics(
    field, component, change
):
    saved = saved_forecast()
    center_hour(saved)["surface"]["fields"][field].update(change)
    refresh_saved_grid(saved)
    output = preview_hour(saved)["components"][component]
    assert output["state"] == "unavailable" and output["value"] is None
    assert output["reasons"]


def test_explicit_phase2_late_and_single_source_rows_are_eligible_without_new_renormalization():
    saved = saved_forecast()
    late = preview_hour(saved, 18)["components"]
    assert late["qpf"]["weights"] == late["wind_u"]["weights"] == {"HRRR": 0.6, "GFS": 0.4}
    fields = center_hour(saved)["surface"]["fields"]
    for field in (
        QPF,
        "dew_point_temperature_2m",
        "eastward_wind_10m",
        "northward_wind_10m",
        "wind_speed_10m",
        "wind_from_direction_10m",
        "wind_gust_10m",
    ):
        fields[field].update(
            weights={"GFS": 1.0}, status="fallback", missing_reasons=["HRRR excluded"]
        )
    refresh_saved_grid(saved)
    result = preview_hour(saved)["components"]
    assert result["qpf"]["state"] == "known" and result["wind_u"]["state"] == "known"
    assert result["qpf"]["weights"] == {"GFS": 1.0}


def test_preview_replays_exactly_without_mutating_or_recalculating_saved_grid(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **kw: pytest.fail("Provider call forbidden")
    )
    saved = saved_forecast()
    before = JSON.serialize(saved)
    first = build_conditions_preview(saved)
    second = build_conditions_preview(JSON.deserialize(before))
    assert JSON.serialize(first) == JSON.serialize(second)
    assert JSON.serialize(saved) == before
    # Diagnostic is deliberately retained exactly, not recalculated from T/Td.
    assert first["center_point"]["hours"][0]["components"]["relative_humidity"]["value"] == 65.4321


def test_missing_grid_or_unsupported_legacy_record_returns_an_explicit_preview_reason():
    saved = saved_forecast()
    saved["forecast"].pop("local_grid_baseline")
    with pytest.raises(ConditionsPreviewUnavailableError):
        build_conditions_preview(saved)


def test_grid_digest_mismatch_is_rejected_without_rebuilding():
    saved = saved_forecast()
    center_hour(saved)["surface"]["fields"][QPF]["value"] = 4
    with pytest.raises(IntegrityError, match="checksum"):
        build_conditions_preview(saved)


def test_unavailable_saved_type_is_not_rendered_as_known_rain():
    saved = saved_forecast()
    center_hour(saved)["surface"]["fields"][PTYPE].update(
        value="rain", status="unavailable", missing_reasons=["Required source evidence failed"]
    )
    refresh_saved_grid(saved)
    output = preview_hour(saved)
    assert output["components"]["precipitation_type"]["state"] == "unavailable"
    assert "Model p-type" not in output["rendering"]["text"]


@pytest.mark.parametrize(
    "change",
    [
        {
            "event_definition": {
                "id": "different_published_lightning_event",
                "parameter": "LTNG",
                "physical_threshold": 1,
                "threshold_status": "documented_count",
            }
        },
        {
            "spatial_support": {
                "kind": "neighborhood",
                "geometry_status": "known",
                "radius_km": 40,
                "operator": "at_least_one_flash",
            }
        },
    ],
)
def test_changed_thunder_event_or_spatial_support_cannot_reuse_the_nbm_hourly_policy(change):
    saved = saved_forecast()
    center_hour(saved)["surface"]["fields"][THUNDER].update(change)
    refresh_saved_grid(saved)
    output = preview_hour(saved)
    assert output["components"]["thunder"]["state"] == "unavailable"
    assert output["components"]["thunder"]["value"] is None
    assert "NBM native thunder probability" not in output["rendering"]["text"]


@pytest.mark.parametrize("malformation", ["missing_cell", "duplicate_index", "coordinate"])
def test_resealed_malformed_lattice_is_rejected_without_regenerating_cells(malformation):
    saved = saved_forecast()
    cells = saved["forecast"]["local_grid_baseline"]["cells"]
    if malformation == "missing_cell":
        cells.pop(0)
    elif malformation == "duplicate_index":
        cells[1]["x_index"] = cells[0]["x_index"]
        cells[1]["y_index"] = cells[0]["y_index"]
    else:
        cells[0]["latitude"] += 0.02
    refresh_saved_grid(saved)
    before = JSON.serialize(saved)
    with pytest.raises(IntegrityError):
        build_conditions_preview(saved)
    assert JSON.serialize(saved) == before


def test_unavailable_cell_suppresses_stale_positive_fields_but_retains_reasons_and_refs():
    saved = saved_forecast()
    cell = saved["forecast"]["local_grid_baseline"]["cells"][0]
    reason = "Native guidance did not cover this context cell"
    cell.update(status="unavailable", missing_reasons=[reason])
    assert cell["hours"][0]["surface"]["fields"][QPF]["value"] > 0
    refresh_saved_grid(saved)
    before = JSON.serialize(saved)
    preview = build_conditions_preview(saved)
    result = preview["cells"][0]
    assert result["status"] == "unavailable" and result["missing_reasons"] == [reason]
    for index, hour in enumerate(result["hours"]):
        for name, field in FIELD_NAMES.items():
            component = hour["components"][name]
            assert component["state"] == "unavailable" and component["value"] is None
            assert reason in component["reasons"]
            assert component["evidence_refs"] == [
                f"/forecast/local_grid_baseline/cells/0/hours/{index}/surface/fields/{field}"
            ]
        assert hour["rendering"]["text"] == "Weather conditions unavailable."
    assert preview["center_point"]["hours"][0]["components"]["qpf"]["state"] == "known"
    assert JSON.serialize(saved) == before
