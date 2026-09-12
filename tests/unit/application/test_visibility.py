"""Native visibility samples retain restrictions, source differences and missingness."""

from copy import deepcopy
from datetime import datetime

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application.visibility import VisibilityView, extract_visibility_contributors

CYCLE = "2026-09-11T12:00:00Z"
VALID = "2026-09-11T15:00:00Z"


def visibility_view(model="HRRR", amount=10000.0, *, end=VALID, native_unit="m", factor=1.0):
    values = np.broadcast_to(np.asarray(amount, dtype=float), (1, 2, 2)).copy()
    event = {
        "source_cycle": CYCLE,
        "source_lead_hours": (
            datetime.fromisoformat(end) - datetime.fromisoformat(CYCLE)
        ).total_seconds()
        / 3600,
        "valid_time": end,
        "interval_start": None,
        "interval_end": None,
        "temporal_semantics": "instantaneous",
        "visibility_definition": "horizontal_visibility",
        "vertical_extent": "surface",
        "spatial_support": "native_model_grid",
        "unit": "m",
        "native_unit": native_unit,
        "native_factor_to_m": factor,
        "missing_reasons": [],
        "provenance": {"raw_sha256": "a" * 64, "url": "https://example.test/visibility"},
        "provider": "retained-test-provider",
        "product": "native-surface-VIS",
        "definition_note": "Native diagnostic; obscuration cause not encoded",
        "censoring": {"status": "unknown", "upper_cap_m": None},
        "model_version": "test-version",
    }
    ds = xr.Dataset(
        {
            "visibility": (("event", "y", "x"), values, {"units": "m"}),
            "native_visibility": (("event", "y", "x"), values / factor, {"units": native_unit}),
        },
        coords={"event": [0], "x": [-94.0, -93.0], "y": [44.0, 45.0]},
    )
    return VisibilityView(
        ds,
        pyproj.CRS.from_epsg(4326),
        {
            "model": model,
            "events": [event],
            "manifest_sha256": "b" * 64,
            "prepared_file": {"sha256": "c" * 64},
        },
    )


def run(views, *, valid=VALID, latitude=44.25, longitude=-93.75, **kwargs):
    return extract_visibility_contributors(
        views, latitude=latitude, longitude=longitude, valid_time=valid, **kwargs
    )


def row(view, **kwargs):
    return next(
        item
        for item in run([view], **kwargs)["contributors"]
        if item["model"] == view.manifest["model"]
    )


def test_nearest_native_visibility_preserves_source_value_restriction_and_provenance():
    view = visibility_view(amount=[[100, 10000], [20000, 30000]])
    source = row(view)
    assert source["value"] == source["native_value"] == 100.0
    assert source["display_miles"] == pytest.approx(0.0621371192237334, rel=1e-14)
    sample = source["spatial_extraction"]
    assert sample["method"] == "nearest_native_grid_cell"
    assert sample["source_y"] == sample["source_x"] == [0] and sample["weights"] == [1.0]
    assert sample["native_coordinate_distance"] == pytest.approx(np.sqrt(0.125), rel=1e-15)
    assert sample["native_coordinate_unit"] == "degree"
    assert (
        sample["visibility_m_at_source_cell"] == sample["native_visibility_at_source_cell"] == 100
    )
    assert source["provenance"] == view.manifest["events"][0]["provenance"]
    assert source["manifest_sha256"] == "b" * 64 and source["prepared_file"]["sha256"] == "c" * 64
    assert source["source_cycle"] == CYCLE and source["source_lead_hours"] == 3
    assert source["censoring"] == {"status": "unknown", "upper_cap_m": None}
    assert source["model_version"] == "test-version"
    assert "weather_condition" not in source and "fog" not in source


def test_units_and_large_uncensored_values_remain_separate_from_display_miles():
    source = row(visibility_view("NBM", amount=1609.344, native_unit="km", factor=1000))
    assert source["value"] == 1609.344 and source["native_value"] == 1.609344
    assert source["display_miles"] == 1.0 and source["native_unit"] == "km"
    high = row(visibility_view(amount=100001.12345))
    assert high["value"] == 100001.12345
    assert high["censoring"]["upper_cap_m"] is None


def test_disagreement_keeps_native_values_separate_without_an_active_blend():
    result = run([visibility_view(amount=100), visibility_view("GFS", amount=20000)])
    comparison = result["comparisons"][0]
    assert comparison["models"] == ["HRRR", "GFS"] and comparison["status"] == "comparable"
    assert comparison["difference_left_minus_right"] == -19900 and comparison["unit"] == "m"
    assert result["field"]["value"] is None and result["field"]["weights"] == {}
    assert result["field"]["status"] == "policy_unavailable"
    assert all(
        item["role"] == "shadow" and item["active_weight"] == 0 for item in result["contributors"]
    )


def test_zero_missing_unsupported_and_absent_native_valid_time_remain_distinct():
    zero = row(visibility_view(amount=0))
    assert zero["value"] == 0 and zero["status"] == "available"
    gap = row(visibility_view(), valid="2026-09-11T14:00:00Z")
    assert gap["value"] is None and "no temporal interpolation" in gap["missing_reasons"][0]
    result = run(
        [],
        source_status={
            "RAP": {"status": "evidence", "missing_reasons": ["Selected message not retained"]},
            "IFS": {"supported": False, "status": "unsupported", "missing_reason": "No open VIS"},
        },
    )
    sources = {item["model"]: item for item in result["contributors"]}
    assert sources["RAP"]["status"] == "unavailable"
    assert sources["RAP"]["missing_reasons"] == ["Selected message not retained"]
    assert sources["IFS"]["status"] == "unsupported" and sources["IFS"]["value"] is None
    assert sources["IFS"]["missing_reasons"] == ["No open VIS"]


@pytest.mark.parametrize("value", [-0.00001, np.nan, np.inf])
def test_bad_selected_native_cell_is_not_replaced_by_another_neighbor(value):
    source = row(visibility_view(amount=[[value, 20000], [30000, 40000]]))
    assert source["value"] is None and source["display_miles"] is None
    assert source["missing_reasons"]


def test_missing_nonselected_cells_do_not_invalidate_a_good_nearest_native_value():
    source = row(visibility_view(amount=[[250.5, np.nan], [np.nan, np.nan]]))
    assert source["value"] == 250.5 and source["missing_reasons"] == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("visibility_definition", "vertical_visibility"),
        ("vertical_extent", "cloud_layer"),
        ("spatial_support", "neighborhood_probability"),
        ("temporal_semantics", "average"),
        ("interval_start", CYCLE),
        ("interval_end", VALID),
        ("source_lead_hours", 4),
        ("unit", "1"),
        ("native_unit", "percent"),
        ("native_factor_to_m", 1000),
    ],
)
def test_incompatible_definitions_support_times_and_units_are_explicit(key, value):
    view = visibility_view("GFS")
    view.manifest["events"][0][key] = value
    result = run([visibility_view(), view])
    assert result["contributors"][1]["value"] is None
    assert result["comparisons"][0]["status"] == "incompatible"


def test_native_array_units_conversion_and_event_missingness_are_checked():
    view = visibility_view()
    view.dataset.native_visibility.values[0, 0, 0] += 1
    assert "reproduce" in row(view)["missing_reasons"][0]
    view = visibility_view()
    view.dataset.visibility.attrs["units"] = "km"
    assert "units" in row(view)["missing_reasons"][0]
    view = visibility_view()
    view.manifest["events"][0]["missing_reasons"] = ["Provider field unavailable"]
    assert row(view)["missing_reasons"] == ["Provider field unavailable"]
    view = visibility_view()
    view.manifest["events"].append(deepcopy(view.manifest["events"][0]))
    assert "unique" in row(view)["missing_reasons"][0]


def test_ties_shared_regional_reuse_and_offline_replay_preserve_native_spatial_variation(
    monkeypatch,
):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("No provider calls during visibility extraction")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    outside = visibility_view(amount=99)
    outside.dataset.coords["x"] = [-101, -100]
    view = visibility_view(amount=[[100, 10000], [20000, 30000]])
    before_ds, before_manifest = view.dataset.copy(deep=True), deepcopy(view.manifest)
    first = run([outside, view], latitude=44.5, longitude=-93.5)
    assert first["contributors"][0]["value"] == 100  # Both ties choose lower stored index.
    assert first == run([outside, view], latitude=44.5, longitude=-93.5)
    second = run([view], latitude=44.75, longitude=-93.25)
    assert second["contributors"][0]["value"] == 30000
    assert second["contributors"][0]["provenance"] == first["contributors"][0]["provenance"]
    xr.testing.assert_identical(before_ds, view.dataset)
    assert view.manifest == before_manifest
    assert "coverage" in row(outside)["missing_reasons"][0]
    descending = VisibilityView(view.dataset.isel(y=slice(None, None, -1)), view.crs, view.manifest)
    assert row(descending, latitude=44.5, longitude=-93.5)["value"] == 20000
