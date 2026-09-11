"""Focused generic probability endpoint and strict event-comparison behavior."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application.probability_contributors import (
    ProbabilityView,
    extract_probability_contributors,
)

VALID = "2026-09-11T18:00:00Z"


def _event(*, hours=6, threshold=0.254, comparison="gt", support=None):
    end = datetime.fromisoformat(VALID)
    return {
        "event_id": f"native-{hours}h-{threshold}-{comparison}",
        "interval_start": (end - timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
        "interval_end": VALID,
        "interval_closure": "left_open_right_closed",
        "threshold": {"value": threshold, "unit": "kg/m^2", "comparison": comparison},
        "spatial_support": {"kind": "grid_point"} if support is None else support,
        "source_cycle": "2026-09-11T00:00:00Z",
        "source_lead_hours": 18,
        "provenance": {"raw_sha256": "a" * 64, "url": "https://example.invalid/native"},
    }


def _view(source="NATIVE_A", *, fraction=0.5, events=None):
    events = [_event()] if events is None else events
    dataset = xr.Dataset(
        {
            "probability": (
                ("event", "y", "x"),
                np.full((len(events), 2, 2), fraction),
                {
                    "units": "1",
                    "temporal_semantics": "probability",
                },
            )
        },
        coords={"x": [-94.0, -93.0], "y": [44.0, 45.0]},
    )
    return ProbabilityView(
        dataset,
        pyproj.CRS.from_epsg(4326),
        {
            "source_id": source,
            "events": events,
            "manifest_sha256": "b" * 64,
            "metadata": {
                "provider": "fixture",
                "population": {"method": "provider_native", "count": None},
            },
        },
    )


def _active():
    return {**_event(hours=1), "value": 0.4, "unit": "1", "weights": {"NBM": 1.0}}


def _run(views, *, active=None, valid=VALID):
    return extract_probability_contributors(
        views,
        latitude=44.5,
        longitude=-93.5,
        valid_time=valid,
        active=_active() if active is None else active,
    )


def test_arbitrary_shadow_sources_keep_active_unchanged_and_compare_only_native_same_period():
    active = _active()
    original = deepcopy(active)
    views = [
        _view("NBM_NATIVE6", fraction=0.4),
        _view("GEFS_NATIVE6", fraction=0.7),
        _view("ANOTHER_NATIVE24", fraction=0.6, events=[_event(hours=24)]),
    ]
    before = [v.dataset.copy(deep=True) for v in views]
    result = _run(views, active=active)
    assert len(result["contributors"]) == 3
    assert [row["value"] for row in result["contributors"]] == [0.4, 0.7, 0.6]
    assert all(
        row["active_weight"] == 0 and row["role"] == "shadow" for row in result["contributors"]
    )
    paired = [row for row in result["comparisons"] if row["status"] == "comparable"]
    assert len(paired) == 1
    assert paired[0]["left"]["source_id"] == "NBM_NATIVE6"
    assert paired[0]["right"]["source_id"] == "GEFS_NATIVE6"
    assert paired[0]["delta"] == pytest.approx(-0.3)
    assert all(row["delta"] is None for row in result["comparisons"] if row not in paired)
    assert active == original
    for old, new in zip(before, views, strict=True):
        xr.testing.assert_identical(old, new.dataset)
    assert _run(views, active=active) == result


def test_independent_bilinear_probability_and_hourly_active_comparison():
    view = _view(events=[_event(hours=1)])
    view.dataset.probability.values[0] = [[0.1, 0.3], [0.5, 0.7]]
    result = _run([view])
    native = result["contributors"][0]
    assert native["value"] == pytest.approx(0.4)
    assert native["spatial_extraction"]["weights"] == [0.25] * 4
    assert native["provenance"]["raw_sha256"] == "a" * 64
    assert result["comparisons"][0]["status"] == "comparable"
    assert result["comparisons"][0]["delta"] == pytest.approx(0)
    assert native["source_metadata"]["population"]["count"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"threshold": {"value": 2.54, "unit": "kg/m^2", "comparison": "gt"}},
        {"threshold": {"value": 0.254, "unit": "kg/m^2", "comparison": "ge"}},
        {"interval_start": "2026-09-11T06:00:00Z"},
        {"interval_closure": "closed"},
        {"spatial_support": {"kind": "neighborhood", "radius_km": 40, "operator": "any"}},
        {"spatial_support": {"kind": "unknown"}},
        {"spatial_support": {"kind": "neighborhood"}},
    ],
)
def test_native_values_remain_available_but_incompatible_events_have_no_disagreement(change):
    event = _event()
    event.update(change)
    result = _run([_view("REFERENCE"), _view("SHADOW", events=[event])])
    assert all(row["status"] == "available" for row in result["contributors"])
    pair = result["comparisons"][-1]
    assert pair["status"] == "incompatible"
    assert pair["delta"] is None and pair["reasons"]


def test_missing_native_endpoints_are_not_filled_or_split():
    result = _run([_view()], valid="2026-09-11T17:00:00Z")
    row = result["contributors"][0]
    assert row["value"] is None and row["status"] == "unavailable"
    assert row["available_native_intervals"][0]["interval_start"] == "2026-09-11T12:00:00Z"
    assert row["available_native_intervals"][0]["interval_end"] == VALID
    assert "no temporal filling" in row["missing_reasons"][0]


@pytest.mark.parametrize("value", [0.0, 1.0, np.nan, -0.1, 1.1])
def test_zero_one_and_invalid_corners_are_explicit(value):
    view = _view(fraction=0.4)
    view.dataset.probability.values[0, 0, 0] = value
    result = _run([view])
    row = result["contributors"][0]
    if np.isfinite(value) and 0 <= value <= 1:
        assert row["value"] == pytest.approx((value + 1.2) / 4)
    else:
        assert row["value"] is None
        assert row["missing_reasons"]
    json.dumps(result, allow_nan=False)


def test_multiple_events_per_source_keep_native_identity_and_json_safe_metadata():
    events = [_event(), _event(threshold=2.54)]
    events[1]["threshold"]["value"] = np.float64(2.54)
    events[0]["provenance"]["acquired_at"] = datetime(2026, 9, 11, 1, tzinfo=UTC)
    view = _view(events=events)
    result = _run([view])
    assert len(result["contributors"]) == 2
    assert result["contributors"][0]["event_id"] != result["contributors"][1]["event_id"]
    assert result["contributors"][0]["provenance"]["acquired_at"] == "2026-09-11T01:00:00+00:00"
    json.dumps(result, allow_nan=False)


def test_repeated_regional_views_select_one_covering_source_without_duplicate_contributors():
    west = _view("REGIONAL", fraction=0.2)
    west.dataset.coords["x"] = [-120, -119]
    target = _view("REGIONAL", fraction=0.6)
    result = _run([west, target])
    assert len(result["contributors"]) == 1
    assert result["contributors"][0]["value"] == 0.6


def test_unknown_active_support_is_not_inferred():
    active = _active()
    active.pop("spatial_support")
    result = _run([_view(events=[_event(hours=1)])], active=active)
    assert result["comparisons"][0]["status"] == "incompatible"
    assert "Unknown event spatial support" in result["comparisons"][0]["reasons"][0]


@pytest.mark.parametrize("changed", [False, True])
def test_prepared_source_metadata_and_native_percent_provenance_are_checked(changed):
    view = _view()
    view.manifest["source_metadata"] = {
        "provider": "NCEP",
        "product": "native_probability",
        "member_population": {"method": "provider_native", "expected_members": None},
    }
    view.manifest["prepared_file"] = {"sha256": "e" * 64}
    view.dataset["native_probability"] = (
        ("event", "y", "x"),
        view.dataset.probability.values * 100,
        {"units": "%"},
    )
    if changed:
        view.dataset.native_probability.values[0, 0, 0] += 1
    row = _run([view])["contributors"][0]
    assert row["source_metadata"] == view.manifest["source_metadata"]
    assert row["prepared_sha256"] == "e" * 64
    if changed:
        assert row["value"] is None
        assert "Native percentages disagree" in row["missing_reasons"][0]
    else:
        assert row["value"] == 0.5
        assert row["spatial_extraction"]["native_percent_at_source_corners"] == [50.0] * 4
