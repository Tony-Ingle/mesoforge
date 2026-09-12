"""Native thunder periods, nearest-cell provenance and temporary hourly passthrough."""

from copy import deepcopy
from datetime import datetime, timedelta

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application.thunder import ThunderView, extract_thunder_contributors
from mesoforge.guidance.sources.thunder import SOURCES

CYCLE = "2026-09-11T12:00:00Z"
VALID = "2026-09-11T18:00:00Z"


def thunder_view(
    source_id="NBM_1H", amount=0.2, *, end=VALID, hours=None, native_unit="percent", factor=0.01
):
    duration = hours if hours is not None else {"NBM_1H": 1, "NBM_3H": 3, "NBM_6H": 6}[source_id]
    valid = datetime.fromisoformat(end)
    values = np.broadcast_to(np.asarray(amount, dtype=float), (1, 2, 2)).copy()
    event = {
        **deepcopy(SOURCES.get(source_id, SOURCES["NBM_1H"])),
        "source_id": source_id,
        "model": "NBM",
        "source_cycle": CYCLE,
        "source_lead_hours": (valid - datetime.fromisoformat(CYCLE)).total_seconds() / 3600,
        "valid_time": end,
        "interval_start": (valid - timedelta(hours=duration)).isoformat(),
        "interval_end": end,
        "interval_closure": "left_open_right_closed",
        "temporal_semantics": "interval_probability",
        "duration_hours": duration,
        "grib_threshold": {"probabilityType": None, "scaledValueOfUpperLimit": None},
        "unit": "1",
        "native_unit": native_unit,
        "native_factor_to_fraction": factor,
        "missing_reasons": [],
        "provenance": {"raw_sha256": "a" * 64, "url": "https://example.test/thunder"},
    }
    ds = xr.Dataset(
        {
            "thunder_probability": (("event", "y", "x"), values, {"units": "1"}),
            "native_probability": (("event", "y", "x"), values / factor, {"units": native_unit}),
        },
        coords={"event": [0], "x": [-94.0, -93.0], "y": [44.0, 45.0]},
    )
    return ThunderView(
        ds,
        pyproj.CRS.from_epsg(4326),
        {
            "source_id": source_id,
            "model": "NBM",
            "events": [event],
            "manifest_sha256": "b" * 64,
            "prepared_file": {"sha256": "c" * 64},
        },
    )


def run(views, *, valid=VALID, latitude=44.25, longitude=-93.75, **kwargs):
    return extract_thunder_contributors(
        views, latitude=latitude, longitude=longitude, valid_time=valid, **kwargs
    )


def test_native_hourly_baseline_keeps_published_probability_event_and_full_provenance():
    view = thunder_view(amount=[[0.123456789, 0.4], [0.5, 0.6]])
    result = run([view])
    field, source = result["field"], result["contributors"][0]
    assert field["value"] == source["value"] == 0.123456789
    assert source["display_percent"] == pytest.approx(12.3456789, rel=1e-15)
    assert source["native_value"] == pytest.approx(12.3456789, rel=1e-15)
    assert field["weights"] == {"NBM": 1.0} and field["policy"]["temporary"]
    assert source["active_weight"] == 1 and source["role"] == "active"
    assert field["source_id"] == "NBM_1H"
    for key in ("event_definition", "spatial_support", "grib_threshold", "provenance"):
        assert field[key] == source[key] == view.manifest["events"][0][key]
    assert field["manifest_sha256"] == "b" * 64 and field["prepared_file"]["sha256"] == "c" * 64
    assert field["source_cycle"] == CYCLE and field["source_lead_hours"] == 6
    sample = source["spatial_extraction"]
    assert sample["source_x"] == sample["source_y"] == [0] and sample["weights"] == [1.0]
    assert sample["method"] == "nearest_native_grid_cell"
    assert sample["native_coordinate_unit"] == "degree"
    assert field["event_definition"]["physical_threshold"] is None
    assert field["spatial_support"]["radius_km"] is None


def test_three_and_six_hour_native_events_are_shadows_not_hourly_conversions():
    result = run([thunder_view(), thunder_view("NBM_3H", 0.6), thunder_view("NBM_6H", 0.8)])
    assert result["field"]["value"] == 0.2
    assert [source["value"] for source in result["contributors"]] == [0.2, 0.6, 0.8]
    assert [source["active_weight"] for source in result["contributors"]] == [1.0, 0.0, 0.0]
    assert all(item["status"] == "incompatible" for item in result["comparisons"])
    assert all(item["difference_left_minus_right"] is None for item in result["comparisons"])
    assert any(
        "intervals differ" in reason for reason in result["comparisons"][0]["missing_reasons"]
    )
    missing_hourly = run([thunder_view("NBM_3H", 0.6), thunder_view("NBM_6H", 0.8)])
    assert missing_hourly["field"]["value"] is None and missing_hourly["field"]["weights"] == {}


def test_zero_missing_invalid_and_unsupported_do_not_become_the_same_value():
    zero = run([thunder_view(amount=0)])
    assert zero["field"]["value"] == 0 and zero["field"]["status"] == "available"
    gap = run([thunder_view()], valid="2026-09-11T19:00:00Z")
    assert (
        gap["field"]["value"] is None and "temporal filling" in gap["field"]["missing_reasons"][0]
    )
    result = run(
        [],
        source_status={
            "NBM_1H": {"status": "evidence", "missing_reasons": ["Native event not acquired"]},
            "HRRR": {
                "model": "HRRR",
                "supported": False,
                "missing_reason": "CAPE is not probability",
            },
        },
    )
    assert result["field"]["status"] == "unavailable" and result["field"]["value"] is None
    assert result["field"]["missing_reasons"] == ["Native event not acquired"]
    source = result["contributors"][-1]
    assert source["status"] == "unsupported" and source["active_weight"] == 0


@pytest.mark.parametrize("value", [-0.00001, 1.00001, np.nan, np.inf])
def test_invalid_selected_probability_is_not_clipped_or_filled_from_neighbors(value):
    result = run([thunder_view(amount=[[value, 0.2], [0.3, 0.4]])])
    assert result["field"]["value"] is None and result["field"]["weights"] == {}
    assert result["contributors"][0]["display_percent"] is None
    assert result["contributors"][0]["missing_reasons"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("native_factor_to_fraction", 1),
        ("unit", "percent"),
        ("native_unit", "J/kg"),
        ("temporal_semantics", "instantaneous"),
        ("source_lead_hours", 7),
    ],
)
def test_incompatible_native_units_event_or_time_cannot_be_promoted(key, value):
    view = thunder_view()
    view.manifest["events"][0][key] = value
    result = run([view])
    assert result["field"]["value"] is None and result["field"]["weights"] == {}
    assert result["contributors"][0]["active_weight"] == 0


def test_hourly_source_cannot_be_relabelled_to_a_different_duration_or_model():
    view = thunder_view(hours=3)
    assert "duration" in run([view])["field"]["missing_reasons"][0]
    view = thunder_view()
    view.manifest["events"][0]["model"] = "HRRR"
    result = run([view])
    assert result["field"]["value"] is None and result["field"]["weights"] == {}
    assert result["contributors"][0]["active_weight"] == 0


def test_an_altered_nbm_event_or_spatial_support_cannot_replace_the_approved_native_baseline():
    view = thunder_view()
    view.manifest["events"][0]["spatial_support"] = {
        "kind": "neighborhood",
        "geometry_status": "known",
        "radius_km": 40,
        "operator": "any_event_in_radius",
    }
    result = run([view])
    assert result["contributors"][0]["value"] == 0.2  # Still separately traceable evidence.
    assert result["contributors"][0]["active_weight"] == 0
    assert result["field"]["value"] is None and result["field"]["weights"] == {}
    assert "exact native NBM" in result["field"]["missing_reasons"][0]
    view = thunder_view()
    view.manifest["events"][0]["event_definition"] = {
        "id": "different_event",
        "parameter": "TSTM",
        "physical_threshold": 1,
        "threshold_status": "documented_count",
    }
    assert run([view])["field"]["value"] is None


def test_generic_native_lightning_probability_is_preserved_without_becoming_nbm_thunder():
    view = thunder_view("FUTURE_LIGHTNING", 0.3, hours=1)
    event = view.manifest["events"][0]
    event.update(model="FUTURE", probability_method="native_published_probability")
    event["event_definition"] = {
        "id": "documented_total_lightning_probability",
        "parameter": "LTNG",
        "physical_threshold": {"count": 1, "unit": "flash", "comparison": "ge"},
        "threshold_status": "provider_documented",
    }
    result = run([view])
    assert result["contributors"][-1]["value"] == 0.3
    assert result["contributors"][-1]["active_weight"] == 0
    assert result["field"]["value"] is None
    event["probability_method"] = "deterministic_lightning_diagnostic"
    assert run([view])["contributors"][-1]["value"] is None


def test_native_array_conversion_missingness_and_duplicate_events_fail_closed():
    view = thunder_view()
    view.dataset.native_probability.values[0, 0, 0] = 90
    assert "reproduce" in run([view])["field"]["missing_reasons"][0]
    view = thunder_view()
    view.manifest["events"][0]["missing_reasons"] = ["Native message unavailable"]
    assert run([view])["field"]["missing_reasons"] == ["Native message unavailable"]
    view = thunder_view()
    view.manifest["events"].append(deepcopy(view.manifest["events"][0]))
    assert "unique" in run([view])["field"]["missing_reasons"][0]


def test_native_grid_point_ties_shared_reuse_and_offline_replay_do_not_mutate_inputs(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Native thunder replay must not call providers")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    outside = thunder_view(amount=0.9)
    outside.dataset.coords["x"] = [-101, -100]
    view = thunder_view(amount=[[0.1, 0.2], [0.3, 0.4]])
    before_ds, before_manifest = view.dataset.copy(deep=True), deepcopy(view.manifest)
    first = run([outside, view], latitude=44.5, longitude=-93.5)
    assert first["field"]["value"] == 0.1
    assert first == run([outside, view], latitude=44.5, longitude=-93.5)
    second = run([view], latitude=44.75, longitude=-93.25)
    assert second["field"]["value"] == 0.4
    assert first["field"]["provenance"] == second["field"]["provenance"]
    xr.testing.assert_identical(view.dataset, before_ds)
    assert view.manifest == before_manifest
    assert "coverage" in run([outside])["field"]["missing_reasons"][0]
