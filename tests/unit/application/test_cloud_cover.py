"""Cloud evidence keeps units, native times, provenance and disagreement separate."""

from copy import deepcopy
from datetime import datetime

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application.cloud_cover import CloudView, extract_cloud_contributors

CYCLE = "2026-09-11T12:00:00Z"
VALID = "2026-09-11T15:00:00Z"


def cloud_view(model="HRRR", amount=20.0, *, end=VALID, native_unit="percent", factor=1.0):
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
        "cloud_definition": "total_cloud_cover",
        "vertical_extent": "entire_atmosphere",
        "unit": "percent",
        "native_unit": native_unit,
        "native_factor_to_percent": factor,
        "missing_reasons": [],
        "provenance": {"raw_sha256": "a" * 64, "url": "https://example.test/cloud"},
        "provider": "retained-test-provider",
        "product": "native-total-cloud-cover",
        "spatial_support": "native model grid-cell total cloud fraction",
        "model_version": "test-version",
    }
    ds = xr.Dataset(
        {
            "cloud_cover": (("event", "y", "x"), values, {"units": "percent"}),
            "native_cloud_cover": (("event", "y", "x"), values / factor, {"units": native_unit}),
        },
        coords={"event": [0], "x": [-94.0, -93.0], "y": [44.0, 45.0]},
    )
    return CloudView(
        ds,
        pyproj.CRS.from_epsg(4326),
        {
            "model": model,
            "events": [event],
            "manifest_sha256": "b" * 64,
            "prepared_file": {"sha256": "c" * 64},
        },
    )


def run(views, *, valid=VALID, **kwargs):
    return extract_cloud_contributors(
        views, latitude=44.5, longitude=-93.5, valid_time=valid, **kwargs
    )


def row(view, *, valid=VALID):
    return next(
        item
        for item in run([view], valid=valid)["contributors"]
        if item["model"] == view.manifest["model"]
    )


def test_native_spatial_interpolation_keeps_corner_and_source_provenance():
    view = cloud_view(amount=[[0, 20], [60, 100]])
    source = row(view)
    assert source["value"] == 45.0 and source["native_value"] == 45.0
    assert source["sky_category"] == "partly_cloudy"
    assert source["unit"] == "percent"
    assert source["spatial_extraction"]["weights"] == [0.25] * 4
    assert source["spatial_extraction"]["cloud_percent_at_source_corners"] == [0, 20, 60, 100]
    assert source["spatial_extraction"]["native_cloud_cover_at_source_corners"] == [0, 20, 60, 100]
    assert source["provenance"] == view.manifest["events"][0]["provenance"]
    assert source["manifest_sha256"] == "b" * 64
    assert source["prepared_file"]["sha256"] == "c" * 64
    assert source["source_cycle"] == CYCLE and source["source_lead_hours"] == 3.0
    assert source["model_version"] == "test-version"


def test_native_fraction_and_percentage_are_individually_preserved():
    source = row(cloud_view("IFS", amount=[[10, 20], [30, 40]], native_unit="1", factor=100))
    assert source["value"] == 25.0 and source["native_value"] == 0.25
    assert source["native_unit"] == "1" and source["sky_category"] == "mostly_clear"
    assert source["spatial_extraction"]["native_cloud_cover_at_source_corners"] == [
        0.1,
        0.2,
        0.3,
        0.4,
    ]


def test_contributor_disagreement_does_not_create_a_baseline_or_change_weights():
    result = run([cloud_view(amount=10), cloud_view("GFS", amount=90)])
    comparison = result["comparisons"][0]
    assert comparison["models"] == ["HRRR", "GFS"]
    assert comparison["status"] == "comparable"
    assert comparison["difference_left_minus_right"] == -80.0
    assert comparison["unit"] == "percentage_point"
    assert "field" not in result
    assert result["active_policy"] == "no-approved-cloud-blend-policy"
    assert all(
        item["role"] == "shadow" and item["active_weight"] == 0 for item in result["contributors"]
    )


def test_zero_cloud_and_native_time_gaps_remain_distinct():
    clear = row(cloud_view(amount=0))
    assert clear["value"] == 0 and clear["sky_category"] == "clear"
    gap = row(cloud_view("IFS"), valid="2026-09-11T14:00:00Z")
    assert gap["value"] is None and gap["sky_category"] is None
    assert "no temporal interpolation" in gap["missing_reasons"][0]
    assert gap["manifest_sha256"] == "b" * 64
    result = run(
        [], source_status={"RAP": {"status": "unsupported", "missing_reason": "missing run"}}
    )
    missing = next(item for item in result["contributors"] if item["model"] == "RAP")
    assert missing["status"] == "unavailable" and missing["missing_reasons"] == ["missing run"]


def test_source_capability_evidence_does_not_claim_missing_runtime_values_are_available():
    from mesoforge.guidance.sources.cloud import SOURCES

    statuses = deepcopy(SOURCES)
    statuses["NBM"]["missing_reasons"] = ["Native total-cloud message was not retained"]
    result = run([], source_status=statuses)
    assert all(source["status"] == "unavailable" for source in result["contributors"])
    assert all(source["value"] is None for source in result["contributors"])
    nbm = next(source for source in result["contributors"] if source["model"] == "NBM")
    assert nbm["missing_reasons"] == ["Native total-cloud message was not retained"]
    assert statuses["NBM"]["status"] == "evidence"


def test_constant_overcast_corners_remain_exactly_100_without_range_clipping():
    from mesoforge.alignment.spatial import bilinear_interpolate

    # Reproduces the weights at a real Minneapolis context cell: ordinary summation
    # overshoots 100% by one ULP even though every native corner is exactly 100%.
    longitude, latitude = 0.4059723029162075, 0.4055669877544279
    view = cloud_view(amount=100)
    view.dataset.coords["x"] = [0.0, 1.0]
    view.dataset.coords["y"] = [0.0, 1.0]
    ordinary = bilinear_interpolate(
        field=view.dataset.cloud_cover.values[0],
        x=view.dataset.x.values,
        y=view.dataset.y.values,
        station_x=longitude,
        station_y=latitude,
    )
    assert ordinary.value == 100.00000000000001
    source = extract_cloud_contributors(
        [view],
        latitude=latitude,
        longitude=longitude,
        valid_time=VALID,
    )["contributors"][0]
    assert source["value"] == source["native_value"] == 100.0
    assert source["status"] == "available" and source["sky_category"] == "cloudy"
    assert source["spatial_extraction"]["calculation"] == "nested_linear_interpolation.v1"
    view.dataset.cloud_cover.values[0, 0, 0] = 100.00001
    rejected = extract_cloud_contributors(
        [view],
        latitude=latitude,
        longitude=longitude,
        valid_time=VALID,
    )["contributors"][0]
    assert rejected["value"] is None and rejected["missing_reasons"]


@pytest.mark.parametrize("value", [-1e-5, 100.00001, np.nan, np.inf])
def test_any_invalid_native_corner_is_unavailable_even_when_interpolated_mean_is_valid(value):
    view = cloud_view(amount=[[value, 25], [25, 25]])
    source = row(view)
    assert source["value"] is None and source["sky_category"] is None
    assert source["missing_reasons"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("cloud_definition", "opaque_cloud_cover"),
        ("vertical_extent", "low_cloud_layer"),
        ("temporal_semantics", "average"),
        ("interval_start", CYCLE),
        ("interval_end", VALID),
        ("source_lead_hours", 4),
        ("unit", "1"),
        ("native_unit", "okta"),
        ("native_factor_to_percent", 100),
    ],
)
def test_other_cloud_definitions_layers_times_or_units_are_not_silently_mixed(key, value):
    view = cloud_view("GFS")
    view.manifest["events"][0][key] = value
    result = run([cloud_view(), view])
    source = result["contributors"][1]
    assert source["value"] is None and source["missing_reasons"]
    assert result["comparisons"][0]["status"] == "incompatible"


def test_fraction_mismatch_missing_native_fields_and_bad_dataset_units_fail_closed():
    view = cloud_view("IFS", native_unit="1", factor=100)
    view.dataset.native_cloud_cover.values[0, 0, 0] = 0.3
    assert "reproduce" in row(view)["missing_reasons"][0]
    view = cloud_view()
    del view.dataset["native_cloud_cover"]
    assert row(view)["value"] is None
    view = cloud_view()
    view.dataset.cloud_cover.attrs["units"] = "1"
    assert "units" in row(view)["missing_reasons"][0]


def test_event_missingness_and_duplicate_valid_times_are_not_filled():
    view = cloud_view()
    view.manifest["events"][0]["missing_reasons"] = ["Provider message not retained"]
    assert row(view)["missing_reasons"] == ["Provider message not retained"]
    view = cloud_view()
    view.manifest["events"].append(deepcopy(view.manifest["events"][0]))
    assert "unique" in row(view)["missing_reasons"][0]


def test_regional_reuse_spatial_variation_and_offline_replay_are_read_only(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Cloud replay cannot call a provider")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    outside = cloud_view(amount=99)
    outside.dataset.coords["x"] = [-101, -100]
    view = cloud_view(amount=[[0, 20], [60, 100]])
    view = CloudView(view.dataset.isel(y=slice(None, None, -1)), view.crs, view.manifest)
    before_ds, before_manifest = view.dataset.copy(deep=True), deepcopy(view.manifest)
    first = run([outside, view])
    assert first == run([outside, view])
    assert first["contributors"][0]["value"] == 45
    second = extract_cloud_contributors([view], latitude=44.25, longitude=-93.75, valid_time=VALID)
    # Bilinear 0*.5625 + 20*.1875 + 60*.1875 + 100*.0625 = 21.25.
    assert second["contributors"][0]["value"] == 21.25
    assert second["contributors"][0]["sky_category"] == "mostly_clear"
    xr.testing.assert_identical(before_ds, view.dataset)
    assert view.manifest == before_manifest
    assert "coverage" in row(outside)["missing_reasons"][0]
