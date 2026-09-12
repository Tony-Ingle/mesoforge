"""Independent interval, unit and spatial checks for native snowfall evidence."""

from copy import deepcopy
from datetime import datetime, timedelta

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application.snowfall_forecast import (
    CLOSURE,
    QUANTITY,
    UNIT,
    SnowView,
    aggregate_snowfall_intervals,
    extract_snowfall_contributors,
)

CYCLE = "2026-09-11T12:00:00Z"
VALID = "2026-09-11T15:00:00Z"


def snow_view(model="HRRR", amount=2.0, *, hours=1, end=VALID, native_unit=UNIT, factor=1):
    values = np.broadcast_to(np.asarray(amount, dtype=float), (1, 2, 2)).copy()
    timestamp = datetime.fromisoformat(end)
    event = {
        "source_cycle": CYCLE,
        "source_lead_hours": (timestamp - datetime.fromisoformat(CYCLE)).total_seconds() / 3600,
        "valid_time": end,
        "interval_start": (timestamp - timedelta(hours=hours)).isoformat(),
        "interval_end": end,
        "interval_closure": CLOSURE,
        "temporal_semantics": "accumulation",
        "native_quantity": QUANTITY,
        "native_unit": native_unit,
        "normalization": {"unit_factor_to_kg_m2": factor, "method": "native_accumulation"},
        "missing_reasons": [],
        "provenance": {"parents": [{"raw_sha256": "a" * 64, "url": "https://example.test/raw"}]},
        "provider": "retained-test-provider",
        "product": "native-snow-water-equivalent",
    }
    dataset = xr.Dataset(
        {
            "amount": (("event", "y", "x"), values, {"units": UNIT}),
            "native_end_amount": (("event", "y", "x"), values / factor, {"units": native_unit}),
        },
        coords={"event": [0], "x": [-94.0, -93.0], "y": [44.0, 45.0]},
    )
    return SnowView(
        dataset,
        pyproj.CRS.from_epsg(4326),
        {
            "model": model,
            "events": [event],
            "manifest_sha256": "b" * 64,
            "prepared_file": {"sha256": "c" * 64},
        },
    )


def run(views, *, valid=VALID, **kwargs):
    return extract_snowfall_contributors(
        views, latitude=44.5, longitude=-93.5, valid_time=valid, **kwargs
    )


def row(view, *, valid=VALID):
    return next(
        item
        for item in run([view], valid=valid)["contributors"]
        if item["model"] == view.manifest["model"]
    )


def test_bilinear_native_water_amount_preserves_corners_provenance_and_policy_gap():
    view = snow_view(amount=[[0, 2], [4, 6]])
    dataset_before = view.dataset.copy(deep=True)
    manifest_before = deepcopy(view.manifest)
    result = run([view])
    source = result["contributors"][0]
    assert source["value"] == 3.0
    assert source["unit"] == UNIT and source["native_quantity"] == QUANTITY
    assert source["spatial_extraction"]["weights"] == [0.25] * 4
    assert source["spatial_extraction"]["amount_kg_m2_at_source_corners"] == [0, 2, 4, 6]
    assert source["provenance"] == manifest_before["events"][0]["provenance"]
    assert source["manifest_sha256"] == "b" * 64
    assert source["prepared_file"]["sha256"] == "c" * 64
    assert result["field"]["value"] is None
    assert result["field"]["status"] == "policy_unavailable" and result["field"]["weights"] == {}
    assert all(
        item["active_weight"] == 0 and item["role"] == "shadow" for item in result["contributors"]
    )
    assert result == run([view])
    xr.testing.assert_identical(dataset_before, view.dataset)
    assert view.manifest == manifest_before


@pytest.mark.parametrize("native_unit", ["m", "m of water equivalent"])
def test_native_metres_of_water_and_cumulative_parents_reproduce_kg_m2(native_unit):
    view = snow_view("IFS", amount=[[1, 2], [3, 4]], hours=3, native_unit=native_unit, factor=1000)
    start = np.full((1, 2, 2), 0.007)
    view.dataset["native_start_amount"] = (("event", "y", "x"), start, {"units": native_unit})
    view.dataset.native_end_amount.values[:] += start
    view.manifest["events"][0]["normalization"]["method"] = (
        "difference_cumulative_native_accumulations"
    )
    source = row(view)
    assert source["value"] == 2.5 and source["native_unit"] == native_unit
    assert source["interval_start"] == "2026-09-11T12:00:00+00:00"
    assert source["spatial_extraction"]["native_start_amount_at_source_corners"] == [0.007] * 4
    view.dataset.native_end_amount.values[0, 0, 0] += 0.001
    rejected = row(view)
    assert rejected["value"] is None and "reproduce" in rejected["missing_reasons"][0]


def test_top_level_unit_factor_is_supported_and_no_ten_to_one_conversion_occurs():
    view = snow_view("IFS", amount=1, hours=3, native_unit="m", factor=1000)
    event = view.manifest["events"][0]
    event["unit_factor_to_kg_m2"] = event.pop("normalization")["unit_factor_to_kg_m2"]
    source = row(view)
    assert source["value"] == 1.0
    assert source["spatial_extraction"]["native_end_amount_at_source_corners"] == [0.001] * 4
    assert "snow_depth" not in str(source)


@pytest.mark.parametrize("amount", [np.nan, -0.01, np.inf])
def test_missing_negative_and_nonfinite_native_corners_fail_closed(amount):
    view = snow_view(amount=[[amount, 2], [4, 6]])
    source = row(view)
    assert source["value"] is None and source["status"] == "unavailable"
    assert source["missing_reasons"]


def test_zero_snowfall_remains_zero_while_unsupported_models_remain_missing():
    result = run(
        [snow_view(amount=0)],
        source_status={
            "GFS": {"status": "unsupported", "missing_reasons": ["Snow storage is not snowfall"]}
        },
    )
    assert result["contributors"][0]["value"] == 0
    assert result["contributors"][0]["status"] == "available"
    gfs = result["contributors"][1]
    assert gfs["value"] is None and gfs["status"] == "unsupported"
    assert gfs["missing_reasons"] == ["Snow storage is not snowfall"]
    assert result["contributors"][-1]["value"] is None


def test_source_capability_reason_explains_unsupported_depth_or_snowpack_product():
    result = run(
        [],
        source_status={
            "NBM": {
                "supported": False,
                "missing_reason": "Native ASNOW is snowfall depth, not water equivalent",
            }
        },
    )
    row = result["contributors"][-1]
    assert row["value"] is None
    assert row["missing_reasons"] == ["Native ASNOW is snowfall depth, not water equivalent"]


def test_hourly_and_three_hourly_native_amounts_are_not_forced_into_one_interval():
    result = run([snow_view(), snow_view("RAP", amount=3), snow_view("IFS", amount=9, hours=3)])
    comparisons = {tuple(item["models"]): item for item in result["comparisons"]}
    paired = comparisons["HRRR", "RAP"]
    assert paired["status"] == "comparable" and paired["difference_left_minus_right"] == -1
    incompatible = comparisons["HRRR", "IFS"]
    assert (
        incompatible["status"] == "incompatible"
        and incompatible["difference_left_minus_right"] is None
    )
    assert "windows" in incompatible["missing_reasons"][0]
    no_endpoint = row(snow_view("IFS", amount=9, hours=3), valid="2026-09-11T14:00:00Z")
    assert no_endpoint["value"] is None and "no splitting" in no_endpoint["missing_reasons"][0]
    assert no_endpoint["native_intervals"][0]["interval_end"] == VALID


def test_native_time_gap_preserves_source_cycle_product_unit_and_retained_identity():
    view = snow_view("IFS", hours=3, native_unit="m of water equivalent", factor=1000)
    view.manifest.update(
        source_cycle=CYCLE,
        source_metadata={
            "provider": "ECMWF",
            "product": "official deterministic IFS",
            "native_parameter": "sf",
            "temporal_support": "cumulative_native_3h",
            "attribution": "ECMWF",
        },
    )
    missing = row(view, valid="2026-09-11T14:00:00Z")
    assert missing["value"] is None and missing["source_cycle"] == CYCLE
    assert missing["provider"] == "ECMWF" and missing["product"] == "official deterministic IFS"
    assert missing["native_unit"] == "m of water equivalent" and missing["native_parameter"] == "sf"
    assert missing["attribution"] == "ECMWF"
    assert missing["manifest_sha256"] == "b" * 64
    assert missing["prepared_file"]["sha256"] == "c" * 64


@pytest.mark.parametrize(
    "key,value",
    [
        ("native_quantity", "snow_water_storage"),
        ("native_quantity", None),
        ("temporal_semantics", "instantaneous"),
        ("interval_closure", "closed_closed"),
        ("source_lead_hours", 4),
        ("interval_start", VALID),
        ("valid_time", "2026-09-11T16:00:00Z"),
    ],
)
def test_incompatible_quantity_and_temporal_metadata_remains_explicit(key, value):
    view = snow_view()
    view.manifest["events"][0][key] = value
    source = row(view)
    assert source["value"] is None and source["missing_reasons"]


def test_native_missingness_bad_units_and_outside_grid_cannot_be_filled():
    view = snow_view()
    view.manifest["events"][0]["missing_reasons"] = ["Missing predecessor accumulation"]
    assert row(view)["missing_reasons"] == ["Missing predecessor accumulation"]
    view = snow_view()
    view.dataset.amount.attrs["units"] = "m"
    assert "units" in row(view)["missing_reasons"][0]
    view = snow_view()
    view.dataset.native_end_amount.attrs["units"] = "inches"
    assert "units" in row(view)["missing_reasons"][0]
    view = snow_view()
    view.dataset.coords["x"] = [-101, -100]
    assert "coverage" in row(view)["missing_reasons"][0]


@pytest.mark.parametrize("unit,factor", [("m", 10), ("inches", 25.4), ("kg/m^2", float("nan"))])
def test_unknown_or_incorrect_native_unit_conversion_is_not_accepted(unit, factor):
    view = snow_view()
    event = view.manifest["events"][0]
    event["native_unit"] = unit
    event["normalization"]["unit_factor_to_kg_m2"] = factor
    source = row(view)
    assert source["value"] is None and "units/conversion" in source["missing_reasons"][0]


def test_shared_regional_views_and_additional_contributors_are_reused_without_mutation():
    outside = snow_view("EXPERIMENTAL", amount=99)
    outside.dataset.coords["x"] = [-101, -100]
    retained = snow_view("EXPERIMENTAL", amount=[[0, 2], [4, 6]])
    # Existing bilinear science supports descending axes, with values reordered too.
    retained = SnowView(
        retained.dataset.isel(y=slice(None, None, -1)), retained.crs, retained.manifest
    )
    result = run([outside, retained])
    source = result["contributors"][-1]
    assert source["model"] == "EXPERIMENTAL" and source["value"] == 3
    second = extract_snowfall_contributors(
        [outside, retained], latitude=44.25, longitude=-93.75, valid_time=VALID
    )["contributors"][-1]
    assert second["value"] == 1.5
    assert second["provenance"] == source["provenance"]


def test_duplicate_intervals_do_not_choose_a_source_arbitrarily():
    view = snow_view()
    view.manifest["events"].append(deepcopy(view.manifest["events"][0]))
    assert "unique" in row(view)["missing_reasons"][0]


def components():
    return [
        row(snow_view(amount=value, end=end), valid=end)
        for value, end in (
            (1.0, "2026-09-11T13:00:00Z"),
            (0.0, "2026-09-11T14:00:00Z"),
            (2.5, "2026-09-11T15:00:00Z"),
        )
    ]


def aggregate(rows):
    return aggregate_snowfall_intervals(rows, start_valid_time=CYCLE, end_valid_time=VALID)


def test_contiguous_same_cycle_intervals_conserve_including_zero_and_retain_components():
    rows = components()
    original = deepcopy(rows)
    result = aggregate(list(reversed(rows)))
    assert result["value"] == 3.5 and result["status"] == "available"
    assert result["source_cycle"] == CYCLE and result["model"] == "HRRR"
    assert result["components"] == rows
    assert rows == original
    assert aggregate([row(snow_view("IFS", amount=3.5, hours=3))])["value"] == 3.5


def test_accumulation_must_not_mix_points_even_if_models_and_times_align():
    rows = components()
    rows[1]["extraction_coordinate"]["latitude"] = 44.75
    result = aggregate(rows)
    assert result["value"] is None and "coordinates" in result["missing_reasons"][0]


@pytest.mark.parametrize(
    "mutation", ["gap", "overlap", "missing", "model", "cycle", "quantity", "unit"]
)
def test_aggregation_rejects_incomplete_incompatible_and_mixed_sources(mutation):
    rows = components()
    if mutation == "gap":
        rows.pop(1)
    elif mutation == "overlap":
        rows.append(deepcopy(rows[0]))
    elif mutation == "missing":
        rows[1].update(value=None, status="unavailable")
    else:
        key, value = {
            "model": ("model", "RAP"),
            "cycle": ("source_cycle", "2026-09-11T06:00:00Z"),
            "quantity": ("native_quantity", "snow_storage"),
            "unit": ("unit", "m"),
        }[mutation]
        rows[1][key] = value
    result = aggregate(rows)
    assert result["value"] is None and result["status"] == "unavailable"
    assert result["missing_reasons"] and result["components"]


def test_empty_period_and_timezone_requirements_are_explicit():
    assert aggregate([])["missing_reasons"] == ["No native snowfall intervals were provided"]
    with pytest.raises(ValueError, match="timezone"):
        run([], valid="2026-09-11T15:00:00")
    with pytest.raises(ValueError, match="precede"):
        aggregate_snowfall_intervals([], start_valid_time=VALID, end_valid_time=CYCLE)
