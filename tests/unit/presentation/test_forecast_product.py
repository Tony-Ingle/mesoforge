"""Presentation fixtures prove aggregation/layout, never a 120-hour forecast policy."""

from copy import deepcopy
from datetime import datetime, timedelta
from io import BytesIO
from math import cos, pi
from zoneinfo import ZoneInfo

import pytest
from pypdf import PdfReader

from mesoforge.common.errors import IntegrityError
from mesoforge.contracts.serialization import canonical_json_digest
from mesoforge.forecasting.cloud_cover import CLOUD
from mesoforge.presentation.forecast_document import (
    HOURS_DOCUMENT_POLICY,
    ForecastCoverageError,
    build_forecast_document,
)
from mesoforge.presentation.forecast_pdf import render_forecast_pdf
from tests.unit.forecasting.test_conditions import _hour, active_sky_field

LOCATION = {
    "name": "Grasston, Minnesota",
    "lat": 45.80268,
    "lon": -93.07952,
    "display_timezone": "America/Chicago",
}


def five_day_saved(reference="2026-10-08T05:00:00Z", count=120):
    """Explicit synthetic final-grid fixture; never save/issue this operationally."""
    target = datetime.fromisoformat(reference)
    hours = []
    for index in range(1, count + 1):
        hour = _hour(index)
        end = target + timedelta(hours=index)
        hour["valid_time"] = end.isoformat().replace("+00:00", "Z")
        fields = hour["surface"]["fields"]
        for field in fields.values():
            if field.get("interval_start"):
                field["interval_start"] = (
                    (end - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
                )
                field["interval_end"] = hour["valid_time"]
            if "valid_time" in field:
                field["valid_time"] = hour["valid_time"]
        temperature = 284.0 + 6 * cos((index - 16) * 2 * pi / 24) - index / 120
        fields["air_temperature_2m"]["value"] = temperature
        hour["temperature"]["value"] = temperature
        fields["dew_point_temperature_2m"]["value"] = temperature - 5
        qpf = 0.6 if 47 <= index <= 57 else 0.0
        fields["liquid_equivalent_precipitation_amount_1h"]["value"] = qpf
        fields["probability_of_precipitation_1h"]["value"] = 0.7 if qpf else 0.05
        fields["probability_of_thunder_1h"]["value"] = 0.0
        cloud = 95.0 if 42 <= index <= 61 else 40.0 if index < 42 else 10.0
        field = active_sky_field(cloud, hour["valid_time"])
        fields[CLOUD] = field
        hours.append(hour)
    center = {
        "latitude": LOCATION["lat"],
        "longitude": LOCATION["lon"],
        "x_index": 0,
        "y_index": 0,
        "is_forecast_point": True,
        "inside_editable_domain": True,
        "status": "calculated",
        "hours": hours,
    }
    grid = {"version": "mesoforge.local-surface-baseline.v2", "cells": [center]}
    forecast = {
        "latitude": LOCATION["lat"],
        "longitude": LOCATION["lon"],
        "target_reference_time": reference,
        "hours": hours,
        "data_kind": "synthetic_demonstration",
        "notice": "TEST FIXTURE - NOT A REAL FORECAST",
        "local_grid_baseline": grid,
        "local_grid": {"sha256": str(canonical_json_digest(grid))},
        "ai_desk": {
            "provider": "fixture",
            "model": "fixture",
            "completion_reason": "no_edit",
            "accepted_recipes": [],
        },
    }
    return {
        "issued_forecast_id": "90c3393d-9459-4230-aea2-b5b3c0ab85f4",
        "issued_at": reference,
        "code_identity": {"git_commit": "a" * 40},
        "forecast": forecast,
    }


def reseal(saved):
    saved["forecast"]["local_grid"]["sha256"] = str(
        canonical_json_digest(saved["forecast"]["local_grid_baseline"])
    )


@pytest.fixture
def saved():
    return five_day_saved()


def test_five_complete_days_exact_amounts_hourly_extrema_and_vector_wind(saved):
    before = deepcopy(saved)
    document = build_forecast_document(saved, location=LOCATION)
    assert saved == before
    assert document["fixture"] is True
    assert len(document["days"]) == 5 and len(document["hours"]) == 120
    assert document["valid_start"] == "2026-10-08T05:00:00Z"
    assert document["valid_end"] == "2026-10-13T05:00:00Z"
    first = document["days"][0]
    temperatures = [hour["temperature"]["value"] for hour in saved["forecast"]["hours"][:24]]
    assert first["high_k"] == max(temperatures) and first["low_k"] == min(temperatures)
    assert first["vector_mean_wind_speed_mps"] == 5
    assert first["vector_mean_wind_direction"] == pytest.approx(216.86989764584402)
    assert first["max_gust_mps"] == 7
    assert first["maximum_hourly_pop"] == 0.05
    assert first["qpf_kg_m2"] == 0
    assert sum(day["qpf_kg_m2"] for day in document["days"]) == pytest.approx(11 * 0.6)
    # Hour ending midnight belongs to the just-completed accumulation day.
    assert document["days"][1]["qpf_kg_m2"] == 1.2
    assert document["days"][2]["qpf_kg_m2"] == pytest.approx(9 * 0.6)


def test_current_36_hour_and_midday_120_hour_are_not_five_complete_days():
    with pytest.raises(ForecastCoverageError, match="36 hours"):
        build_forecast_document(five_day_saved(count=36), location=LOCATION)
    with pytest.raises(ForecastCoverageError, match="partial first/last"):
        build_forecast_document(five_day_saved(reference="2026-10-08T15:00:00Z"), location=LOCATION)


@pytest.mark.parametrize(
    "reference,count", [("2026-03-07T06:00:00Z", 119), ("2026-10-31T05:00:00Z", 121)]
)
def test_five_calendar_days_respect_dst_not_fixed_24_hour_blocks(reference, count):
    document = build_forecast_document(five_day_saved(reference, count), location=LOCATION)
    assert len(document["hours"]) == count
    assert sum(day["hours"] for day in document["days"]) == count
    assert document["days"][1]["hours"] == (23 if count == 119 else 25)


def test_qpf_missing_or_bad_interval_is_not_dry_daily_total(saved):
    fields = saved["forecast"]["hours"][0]["surface"]["fields"]
    fields["liquid_equivalent_precipitation_amount_1h"]["interval_start"] = "2026-10-08T04:00:00Z"
    fields["probability_of_precipitation_1h"]["threshold"]["value"] = 1
    reseal(saved)
    document = build_forecast_document(saved, location=LOCATION)
    first = document["days"][0]
    assert first["qpf_kg_m2"] is None and first["maximum_hourly_pop"] is None
    assert first["availability"]["qpf"]["available_hours"] == 23
    assert document["days"][1]["qpf_kg_m2"] == 1.2


def test_evidence_does_not_fill_missing_active_fields_or_create_winter_conditions(saved):
    for hour in saved["forecast"]["hours"]:
        hour["surface"]["fields"][CLOUD]["status"] = "unavailable"
        hour["surface"]["fields"][CLOUD]["value"] = None
        hour["surface"]["fields"]["liquid_equivalent_precipitation_amount_1h"]["value"] = None
        hour["surface"]["fields"]["probability_of_precipitation_1h"]["value"] = None
        hour["surface"]["snowfall_amount_guidance"] = {"value": 50}
        hour["surface"]["cloud_guidance"] = {"HRRR": {"value": 0.4}}
    reseal(saved)
    result = build_forecast_document(saved, location=LOCATION)
    assert all(hour["sky"] is None for hour in result["hours"])
    assert all("snow" not in hour["condition"].lower() for hour in result["hours"])
    assert all(day["qpf_kg_m2"] is None for day in result["days"])


def test_corrupt_grid_wrong_location_and_time_gaps_rejected(saved):
    with pytest.raises(IntegrityError, match="configured location"):
        build_forecast_document(saved, location={**LOCATION, "lat": 44})
    saved["forecast"]["local_grid"]["sha256"] = "sha256:" + "f" * 64
    with pytest.raises(IntegrityError, match="digest"):
        build_forecast_document(saved, location=LOCATION)
    reseal(saved)
    saved["forecast"]["hours"][20]["valid_time"] = saved["forecast"]["hours"][19]["valid_time"]
    with pytest.raises(IntegrityError, match="sequence"):
        build_forecast_document(saved, location=LOCATION)


def test_wind_averages_vectors_and_complete_calm_has_no_direction(saved):
    for index, hour in enumerate(saved["forecast"]["hours"]):
        hour["surface"]["fields"]["eastward_wind_10m"]["value"] = 3 if index % 2 else -3
        hour["surface"]["fields"]["northward_wind_10m"]["value"] = 0
    reseal(saved)
    document = build_forecast_document(saved, location=LOCATION)
    assert all(day["vector_mean_wind_speed_mps"] == 0 for day in document["days"])
    assert all(day["vector_mean_wind_direction"] is None for day in document["days"])


def test_pdf_two_pages_extractable_deterministic_and_no_private_metadata(saved):
    saved["forecast"]["irrelevant_secret"] = "FAKE_SECRET_MUST_NOT_LEAK"
    document = build_forecast_document(saved, location=LOCATION)
    first = render_forecast_pdf(document)
    assert first == render_forecast_pdf(document)
    assert len(first) < 250_000
    reader = PdfReader(BytesIO(first))
    assert len(reader.pages) == 2
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "Grasston, Minnesota" in text and "5-Day Forecast" in text
    assert text.count("TEST FIXTURE") == 2
    assert "Max hourly PoP" in text and "AI desk:" in text
    assert "Temperature & moisture" in text and "Wind & gusts" in text
    assert all(
        word not in text
        for word in ("FAKE_SECRET", "None", "NaN", "unknown", "sha256:", "/forecast/")
    )
    assert document["issued_forecast_id"] not in text
    assert "/CreationDate" in reader.metadata
    for page in reader.pages:
        content = page.extract_text()
        assert (
            "MESO" in content and "TEST FIXTURE" in content and "Generated by MesoForge" in content
        )
        assert tuple(page.mediabox) == (0, 0, 612, 792)
        assert tuple(page.cropbox) == (0, 0, 612, 792)


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("no_edit", "Reviewed - no changes justified"),
        ("complete", "Reviewed - 1 validated edit"),
        ("provider_timeout", "Fallback - latest validated forecast retained"),
    ],
)
def test_pdf_uses_actual_desk_completion_vocabulary(saved, outcome, expected):
    saved["forecast"]["ai_desk"].update(
        completion_reason=outcome, accepted_recipes=[{"recipe_digest": "fixture"}]
    )
    document = build_forecast_document(saved, location=LOCATION)
    assert document["ai"]["display_status"] == expected


def test_card_text_and_transition_notes_cannot_overlap_neighboring_sections(saved):
    document = build_forecast_document(saved, location=LOCATION)
    document["days"][0]["most_frequent_hourly_condition"] = "Mostly cloudy with a chance of rain."
    render_forecast_pdf(document)  # Three lines fit in their reserved 32-point box.
    document["days"][0]["most_frequent_hourly_condition"] = "Very long condition " * 20
    with pytest.raises(ValueError, match="refuse clipped output"):
        render_forecast_pdf(document)
    document = build_forecast_document(saved, location=LOCATION)
    document["transitions"] = ["A long but bounded transition note " * 8] * 3
    text = "\n".join(
        page.extract_text() for page in PdfReader(BytesIO(render_forecast_pdf(document))).pages
    )
    assert "Some transition notes omitted for space" in text


def test_small_positive_liquid_is_not_printed_as_zero_and_missing_is_visible(saved):
    for hour in saved["forecast"]["hours"]:
        hour["surface"]["fields"]["liquid_equivalent_precipitation_amount_1h"]["value"] = 0.00001
    saved["forecast"]["hours"][30]["surface"]["fields"]["air_temperature_2m"]["value"] = None
    reseal(saved)
    document = build_forecast_document(saved, location=LOCATION)
    text = "\n".join(
        page.extract_text() for page in PdfReader(BytesIO(render_forecast_pdf(document))).pages
    )
    assert "<0.01 in" in text and "Unavailable" in text
    assert "Liquid: 0.00 in" not in text


@pytest.mark.parametrize(
    "reference,counts",
    [
        ("2026-10-08T03:00:00Z", [2, 24, 10]),
        ("2026-10-08T15:00:00Z", [14, 22]),
        ("2026-11-01T03:00:00Z", [2, 25, 9]),
        ("2026-03-08T04:00:00Z", [2, 23, 11]),
    ],
)
def test_36_hour_document_covers_exact_all_hours_and_partial_local_dates(reference, counts):
    saved = five_day_saved(reference, count=36)
    original = deepcopy(saved)
    for index, hour in enumerate(saved["forecast"]["hours"]):
        hour["surface"]["fields"]["liquid_equivalent_precipitation_amount_1h"]["value"] = (
            index / 100
        )
    reseal(saved)
    document = build_forecast_document(saved, location=LOCATION, hours=36)
    assert document["document_policy"] == HOURS_DOCUMENT_POLICY
    assert document["product_title"] == "36-Hour Weather Outlook"
    assert len(document["hours"]) == 36
    assert [row["hours"] for row in document["days"]] == counts
    assert document["days"][0]["partial_local_day"] is True
    assert document["days"][-1]["partial_local_day"] is True
    assert document["summary"]["hours"] == 36
    assert document["summary"]["qpf_kg_m2"] == pytest.approx(sum(range(36)) / 100)
    assert sum(row["qpf_kg_m2"] for row in document["days"]) == pytest.approx(sum(range(36)) / 100)
    assert document["valid_start"] == reference
    assert document["valid_end"] == original["forecast"]["hours"][-1]["valid_time"]
    assert [row["valid_time"] for row in document["hours"]] == [
        row["valid_time"] for row in original["forecast"]["hours"]
    ]
    pdf = render_forecast_pdf(document)
    reader = PdfReader(BytesIO(pdf))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert len(reader.pages) == 2
    assert "36-Hour Weather Outlook" in text and "5-Day Forecast" not in text
    assert "partial day" in text and "36h liquid" in text
    assert "Hourly high / low" in text and "in this interval" in text
    local_start = datetime.fromisoformat(reference).astimezone(ZoneInfo("America/Chicago"))
    detail = reader.pages[1].extract_text()
    assert local_start.strftime("%I %p %Z").lstrip("0") in detail
    assert "Hourly PoP, hour ending" in detail
    for page in reader.pages:
        assert "Generated by MesoForge" in page.extract_text()
        assert "TEST FIXTURE" in page.extract_text()
    assert pdf == render_forecast_pdf(document)


def test_36_hour_document_preserves_missing_amounts_and_real_final_values():
    saved = five_day_saved(count=36)
    hour = saved["forecast"]["hours"][-1]
    hour["surface"]["fields"]["air_temperature_2m"]["value"] = 300.0
    hour["surface"]["fields"]["liquid_equivalent_precipitation_amount_1h"]["value"] = None
    reseal(saved)
    document = build_forecast_document(saved, location=LOCATION, hours=36)
    assert document["summary"]["qpf_kg_m2"] is None
    assert document["summary"]["high_k"] == 300.0
    assert document["hours"][-1]["temperature"] == 300.0
    assert document["summary"]["availability"]["qpf"]["available_hours"] == 35
    assert "Unavailable" in "\n".join(
        page.extract_text() for page in PdfReader(BytesIO(render_forecast_pdf(document))).pages
    )
    with pytest.raises(ForecastCoverageError, match="all 36"):
        build_forecast_document(five_day_saved(count=35), location=LOCATION, hours=36)
    with pytest.raises(ValueError, match="exactly 36"):
        build_forecast_document(saved, location=LOCATION, hours=24)


@pytest.mark.parametrize("hours", [None, 36])
def test_pdf_qpf_missing_hour_is_visibly_distinguished_from_numeric_zero(hours):
    saved = five_day_saved(count=36 if hours else 120)
    for hour in saved["forecast"]["hours"]:
        hour["surface"]["fields"]["liquid_equivalent_precipitation_amount_1h"]["value"] = 0.0
    reseal(saved)
    document = build_forecast_document(saved, location=LOCATION, hours=hours)
    zero_page = PdfReader(BytesIO(render_forecast_pdf(document))).pages[1]
    assert "X = unavailable hour" not in zero_page.extract_text()
    assert document["summary"]["qpf_kg_m2"] == 0.0

    saved["forecast"]["hours"][8]["surface"]["fields"]["liquid_equivalent_precipitation_amount_1h"][
        "value"
    ] = None
    reseal(saved)
    document = build_forecast_document(saved, location=LOCATION, hours=hours)
    missing_page = PdfReader(BytesIO(render_forecast_pdf(document))).pages[1]
    assert "X = unavailable hour; zero = no bar" in missing_page.extract_text()
    assert document["hours"][8]["qpf"] is None
    assert document["summary"]["qpf_kg_m2"] is None
