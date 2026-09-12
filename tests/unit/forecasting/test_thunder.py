"""Native thunder events remain separate from rainfall and deterministic diagnostics."""

from copy import deepcopy

import numpy as np
import pytest

from mesoforge.forecasting.thunder import (
    thunder_event_comparison_reasons,
    thunder_fraction,
    thunder_percent,
    validate_thunder_event,
)
from tests.unit.application.test_thunder import thunder_view


def test_probability_units_preserve_zero_one_and_unrounded_values():
    assert thunder_fraction(0, "percent") == 0
    assert thunder_fraction(100, "%") == 1
    assert thunder_fraction(12.3456789, "percent") == pytest.approx(0.123456789, rel=1e-15)
    assert thunder_percent(0.123456789) == pytest.approx(12.3456789, rel=1e-15)
    assert thunder_fraction(0.125) == 0.125


@pytest.mark.parametrize("value", [-1e-9, 1.0000001, np.nan, np.inf])
def test_invalid_probabilities_are_never_clipped(value):
    with pytest.raises(ValueError, match="no clipping"):
        thunder_fraction(value)


@pytest.mark.parametrize("unit", ["J/kg", "dBZ", "kg/m^2", "flash/km^2/hour"])
def test_deterministic_diagnostics_cannot_be_relabelled_as_probabilities(unit):
    with pytest.raises(ValueError, match="Unsupported"):
        thunder_fraction(0.1, unit)


def test_native_one_three_and_six_hour_intervals_remain_distinct_events():
    events = [thunder_view(f"NBM_{hours}H").manifest["events"][0] for hours in (1, 3, 6)]
    assert [
        int((validate_thunder_event(event)[1] - validate_thunder_event(event)[0]).total_seconds())
        for event in events
    ] == [3600, 10800, 21600]
    reasons = thunder_event_comparison_reasons(events[0], events[1])
    assert any("intervals differ" in reason for reason in reasons)
    assert any("spatial equivalence is unproven" in reason for reason in reasons)
    assert events[0]["event_definition"]["physical_threshold"] is None
    assert events[0]["spatial_support"]["radius_km"] is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("duration_hours", 6),
        ("source_lead_hours", 7),
        ("interval_closure", "closed"),
        ("temporal_semantics", "instantaneous"),
        ("interval_start", "2026-09-11T18:00:00Z"),
        ("valid_time", "2026-09-11T19:00:00Z"),
    ],
)
def test_incompatible_native_time_metadata_is_rejected(key, value):
    event = thunder_view().manifest["events"][0]
    event[key] = value
    with pytest.raises(ValueError, match="interval"):
        validate_thunder_event(event)


def test_unencoded_physical_threshold_or_footprint_cannot_be_fabricated():
    event = thunder_view().manifest["events"][0]
    event["event_definition"]["physical_threshold"] = {"value": 1, "unit": "flash"}
    with pytest.raises(ValueError, match="threshold"):
        validate_thunder_event(event)
    event = thunder_view().manifest["events"][0]
    event["spatial_support"]["radius_km"] = 40
    with pytest.raises(ValueError, match="footprint"):
        validate_thunder_event(event)


def test_even_matching_intervals_require_proven_spatial_event_equivalence():
    event = thunder_view().manifest["events"][0]
    assert thunder_event_comparison_reasons(event, deepcopy(event))
    event["spatial_support"]["geometry_status"] = "known"
    assert thunder_event_comparison_reasons(event, deepcopy(event))  # A label is not a geometry.
    event["spatial_support"] = {
        "kind": "neighborhood",
        "geometry_status": "known",
        "radius_km": 10,
        "operator": "any_event_in_radius",
    }
    assert thunder_event_comparison_reasons(event, deepcopy(event)) == []
    other = deepcopy(event)
    other["spatial_support"]["radius_km"] = 25
    assert any(
        "supports differ" in reason for reason in thunder_event_comparison_reasons(event, other)
    )
