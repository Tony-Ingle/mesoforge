"""The compact analytical block is a small, pure projection of one fact payload."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime

from mesoforge.verification.analytical_attributes import (
    ANALYTICAL_SCHEMA_VERSION,
    ATTRIBUTE_KEY,
    CONTEXT_FIELDS,
    analytical_block,
    build_analytical_attributes,
)
from mesoforge.verification.site_analysis import _REGIME_DIMENSIONS, _signature
from tests.unit.application.test_site_verification_analysis import VERSION_A, issued, payload

TARGET = datetime(2026, 9, 16, 22, tzinfo=UTC)


def rich_payload() -> dict:
    body = payload(issued(VERSION_A, TARGET), 1, forecast=296.0, observed=295.0)
    forecast = body["match"]["forecast"]
    forecast["surface"]["fields"] = {
        "air_temperature_2m": {"value": 296.0, "unit": "K", "weights": {"HRRR": 0.7}},
        "wind_speed_10m": {"value": 3.4, "unit": "m/s", "row_sha256": "x" * 64},
        "wind_from_direction_10m": {"value": 133.7, "unit": "degree"},
        "cloud_area_fraction": {"value": None, "unit": "1", "grib_keys": {"big": ["x"] * 500}},
        "precipitation_type": {"value": "rain", "unit": "category", "note": "prose " * 200},
        "probability_of_precipitation_1h": {"value": 0.51, "unit": "1", "provenance": {"u": "h"}},
    }
    forecast["surface"]["contributors"] = {"HRRR": {"url": "https://example.invalid/" + "a" * 900}}
    body["match"]["candidates"] = [{"station_id": f"K{i:03d}", "raw": "x" * 800} for i in range(60)]
    body["match"]["forecast_context"]["hourly_report"]["hours"] = [{"row": "y" * 4000}] * 36
    return body


def test_block_carries_identity_values_and_compact_context_only():
    body = rich_payload()
    original = deepcopy(body)
    block = build_analytical_attributes(body)
    assert body == original  # a projection never alters the evidence payload
    assert block["schema_version"] == ANALYTICAL_SCHEMA_VERSION
    assert block["field"] == "air_temperature_2m" and block["unit"] == "K"
    assert block["error_definition"] == "forecast_minus_observation"
    assert block["fact_schema_version"] == "issued-temperature-verification.v1"
    assert block["status"] == "verified"
    assert block["verification_policy_id"] == "issued-temperature-verification.v1"
    assert block["matching_policy_digest"].startswith("sha256:")
    assert block["issued_forecast_id"] == str(VERSION_A)
    assert block["issued_forecast_digest"] == f"sha256:{VERSION_A}"
    assert block["target_reference_time"] == "2026-09-16T22:00:00+00:00"
    assert block["valid_time"] == "2026-09-16T23:00:00Z"
    assert block["horizon_hours"] == 1
    assert (block["latitude"], block["longitude"]) == (44.98859, -93.25557)
    assert block["forecast_temperature_k"] == 296.0
    assert block["temperature_error_k"] == 1.0
    assert block["observation"] == {
        "station_id": "KMIC",
        "catalog_station_id": "station.kmic",
        "network": "METAR",
        "provider": "aviationweather.gov",
        "latitude": 45.0623,
        "longitude": -93.35108,
        "elevation_m": 263,
        "distance_km": 11.1248,
        "observation_time": "2026-09-16T22:53:00+00:00",
        "time_difference_seconds": -420,
        "temperature_k": 295.0,
        "revision_digest": "sha256:rev-1",
        "logical_observation_digest": "sha256:logical-KMIC-2026-09-16T23:00:00+00:00",
        "raw_record_digest": "sha256:record",
        "raw_artifact_id": "art_raw_1",
        "observations_artifact_id": "obs-for-art_raw_1",
    }
    assert block["display_timezone"] == "America/Chicago"
    assert block["context"] == {
        "fields": {
            "cloud_area_fraction": None,
            "wind_speed_10m": 3.4,
            "wind_from_direction_10m": 133.7,
            "probability_of_precipitation_1h": 0.51,
            "precipitation_type": "rain",
        },
        "units": {
            "cloud_area_fraction": "1",
            "wind_speed_10m": "m/s",
            "wind_from_direction_10m": "degree",
            "probability_of_precipitation_1h": "1",
            "precipitation_type": "category",
        },
        "model_temperatures_k": {"HRRR": 296.2, "GFS": 295.6, "IFS": None},
    }
    # Small on purpose: no candidates, provenance, GRIB keys, URLs, prose or report rows.
    encoded = json.dumps(block)
    assert len(encoded) < 2500 < len(json.dumps(body)) / 50
    for forbidden in ("candidates", "grib_keys", "https://", "prose", "row_sha256", "weights"):
        assert forbidden not in encoded
    assert {field for _, field, _ in _REGIME_DIMENSIONS} == set(CONTEXT_FIELDS)


def test_identity_distinguishes_reacquisition_revision_policy_and_opportunity():
    base = build_analytical_attributes(rich_payload())

    def signature(block):
        return _signature({**block, "schema_version": block["fact_schema_version"]})

    reacquired_body = rich_payload()
    reacquired_body["match"]["selected"]["provenance"]["raw_artifact_id"] = "art_raw_2"
    reacquired_body["match"]["input_provenance"]["observations"]["artifact_id"] = "obs-2"
    reacquired_body["verification_cutoff"] = "2026-09-17T03:20:28Z"
    reacquired_body["match"]["candidates"] = []
    reacquired = build_analytical_attributes(reacquired_body)
    assert reacquired != base  # acquisition provenance is still recorded
    assert signature(reacquired) == signature(base)  # ...but the evidence is the same

    revised_body = rich_payload()
    revised_body["match"]["selected"]["provenance"]["revision_digest"] = "sha256:rev-2"
    revised_body["match"]["selected"]["temperature"]["value"] = 294.4
    revised_body["temperature_error"]["value"] = 296.0 - 294.4
    revised = build_analytical_attributes(revised_body)
    assert signature(revised) != signature(base)
    assert (
        revised["observation"]["logical_observation_digest"]
        == base["observation"]["logical_observation_digest"]
    )
    assert revised["observation"]["revision_digest"] != base["observation"]["revision_digest"]

    other_policy_body = rich_payload()
    other_policy_body["match"]["selection_policy"]["max_distance_km"] = 25
    other_policy = build_analytical_attributes(other_policy_body)
    assert other_policy["matching_policy_digest"] != base["matching_policy_digest"]
    assert signature(other_policy) != signature(base)

    next_hour = build_analytical_attributes(
        payload(issued(VERSION_A, TARGET), 2, forecast=294.5, observed=295.0)
    )
    assert (next_hour["issued_forecast_id"], next_hour["valid_time"]) != (
        base["issued_forecast_id"],
        base["valid_time"],
    )


def test_missing_or_foreign_values_stay_null_instead_of_being_invented():
    body = rich_payload()
    body["match"]["forecast"]["temperature"] = {"value": 72.0, "unit": "degF"}
    body["temperature_error"] = {"value": 1.0, "unit": "degF"}
    body["match"]["forecast"].pop("surface")
    body["match"]["forecast_context"].pop("hourly_report")
    body["match"]["selection_policy"] = None
    body["code_identity"] = {}
    block = build_analytical_attributes(body)
    assert block["forecast_temperature_k"] is None
    assert block["temperature_error_k"] is None
    assert block["matching_policy_digest"] is None
    assert block["display_timezone"] is None
    assert block["code_commit"] is None
    assert block["context"]["fields"] == {} and block["context"]["units"] == {}
    assert build_analytical_attributes({})["observation"]["station_id"] is None


def test_attribute_reader_accepts_only_the_current_complete_schema():
    block = build_analytical_attributes(rich_payload())
    assert analytical_block({ATTRIBUTE_KEY: block}) == (block, "compact_attributes")
    assert analytical_block(None) == (None, "no_analytical_attributes")
    assert analytical_block({"issued_forecast_id": "x"}) == (None, "no_analytical_attributes")
    assert analytical_block({ATTRIBUTE_KEY: "text"}) == (None, "malformed_analytical_attributes")
    assert analytical_block({ATTRIBUTE_KEY: {**block, "schema_version": "other.v9"}}) == (
        None,
        "unsupported_analytical_schema",
    )
    assert analytical_block({ATTRIBUTE_KEY: {**block, "observation": None}}) == (
        None,
        "malformed_analytical_attributes",
    )
