"""Native snowfall and independent profile/SWE corner calculations remain separate."""

import json
from copy import deepcopy
from datetime import datetime, timedelta

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application.snowfall_amount_forecast import (
    AmountView,
    extract_snowfall_amount_contributors,
)
from tests.unit.application.test_snowfall_forecast import snow_view

CYCLE = "2026-09-11T12:00:00Z"
VALID = "2026-09-11T15:00:00Z"


def amount_view(
    model="HRRR",
    amount=0.01,
    *,
    valid=VALID,
    hours=1,
    profile_temperature=271.16,
    native_ratio=12.0,
):
    end = datetime.fromisoformat(valid)
    lead = (end - datetime.fromisoformat(CYCLE)).total_seconds() / 3600
    values = np.broadcast_to(np.asarray(amount, dtype=float), (1, 2, 2)).copy()
    data = xr.Dataset(
        {
            "amount": (("event", "y", "x"), values, {"units": "m"}),
            "native_end_amount": (("event", "y", "x"), values.copy(), {"units": "m"}),
        },
        coords={"event": [0], "x": [-94.0, -93.0], "y": [44.0, 45.0]},
    )
    event = {
        "source_cycle": CYCLE,
        "source_lead_hours": lead,
        "valid_time": valid,
        "interval_start": (end - timedelta(hours=hours)).isoformat(),
        "interval_end": valid,
        "interval_closure": "left_open_right_closed",
        "temporal_semantics": "accumulation",
        "native_quantity": "new_snowfall_amount",
        "hydrometeor_scope": "snow_and_sleet" if model == "NBM" else "snow",
        "native_unit": "m",
        "unit_factor_to_m": 1.0,
        "normalization": {"method": "native_exact_interval_amount", "unit_factor_to_m": 1.0},
        "native_parameter": "ASNOW",
        "raw_grib_unit": "unknown",
        "missing_reasons": [],
        "provider": "NOAA",
        "product": "retained native new-snow product",
        "provenance": {
            "parents": [{"raw_sha256": "a" * 64, "source_url": "https://example.invalid/raw"}]
        },
    }
    instant = {
        "source_cycle": CYCLE,
        "source_lead_hours": lead,
        "valid_time": valid,
        "temporal_semantics": "instantaneous",
        "interval_start": None,
        "interval_end": None,
        "missing_reasons": [],
        "status": "available",
        "provenance": {"raw_sha256": "d" * 64},
    }
    if model == "RAP":
        data = data.assign_coords(level=np.arange(500, 1001, 25))
        data["temperature_profile"] = (
            ("event", "level", "y", "x"),
            np.broadcast_to(np.asarray(profile_temperature, dtype=float), (1, 21, 2, 2)).copy(),
            {"units": "K"},
        )
        data["temperature_2m"] = (("event", "y", "x"), np.full((1, 2, 2), 260.0), {"units": "K"})
        data["surface_pressure"] = (
            ("event", "y", "x"),
            np.full((1, 2, 2), 101000.0),
            {"units": "Pa"},
        )
        event["profile"] = {
            **deepcopy(instant),
            "complete": True,
            "level_hpa": list(range(500, 1001, 25)),
            "temperature_unit": "K",
            "surface_pressure_unit": "Pa",
        }
    if model == "NBM":
        data["native_slr"] = (
            ("event", "y", "x"),
            np.broadcast_to(np.asarray(native_ratio, dtype=float), (1, 2, 2)).copy(),
            {"units": "1"},
        )
        event["native_slr"] = {**deepcopy(instant), "unit": "1", "native_unit": "kg kg**-1"}
    return AmountView(
        data,
        pyproj.CRS.from_epsg(4326),
        {
            "model": model,
            "source_cycle": CYCLE,
            "events": [event],
            "manifest_sha256": "b" * 64,
            "prepared_file": {"sha256": "c" * 64},
            "source_metadata": {"provider": "NOAA", "native_parameter": "ASNOW"},
        },
    )


def run(views, *, swe_views=None, valid=VALID, source_status=None):
    return extract_snowfall_amount_contributors(
        views,
        swe_views=[] if swe_views is None else swe_views,
        latitude=44.5,
        longitude=-93.5,
        valid_time=valid,
        source_status=source_status,
    )


def test_native_amount_ratios_and_kuchera_corner_products_remain_separate():
    rap = amount_view("RAP", profile_temperature=[[261.16, 271.16], [273.16, 275.16]])
    water = snow_view("RAP", amount=[[1.0, 2.0], [3.0, 4.0]])
    result = run([amount_view(), rap, amount_view("NBM", native_ratio=15)], swe_views=[water])
    native = {row["model"]: row for row in result["native_contributors"]}
    derived = result["derived_contributors"][0]
    assert native["RAP"]["value"] == 0.01
    assert result["native_slr"][0]["value"] == 15
    # Ratios 22,12,8,4 times corner SWE 1,2,3,4 -> 22,24,24,16 mm, average21.5mm.
    assert derived["value"] == pytest.approx(0.0215, abs=1e-12)
    assert derived["diagnostic_ratio"] == pytest.approx(11.5, abs=1e-12)
    assert derived["value"] != pytest.approx(11.5 * 2.5 / 1000, abs=1e-12)
    evidence = derived["spatial_extraction"]
    np.testing.assert_allclose(
        evidence["amount_m_at_source_corners"], [0.022, 0.024, 0.024, 0.016], rtol=0, atol=1e-12
    )
    assert evidence["swe_kg_m2_at_source_corners"] == [1, 2, 3, 4]
    assert len(evidence["temperature_profile_k_at_source_corners"]) == 21
    assert "snowfall_water_equivalent" in derived["provenance"]
    assert derived["profile_metadata"]["provenance"]["raw_sha256"] == "d" * 64
    assert result["field"]["value"] is None and result["field"]["weights"] == {}
    assert result["field"]["status"] == "policy_unavailable"
    comparison = next(row for row in result["comparisons"] if row["models"] == ["RAP", "RAP"])
    assert comparison["methods"] == ["native", "kuchera"]
    assert comparison["difference_left_minus_right"] == pytest.approx(-0.0115, abs=1e-12)
    assert all(
        row["active_weight"] == 0
        for row in [
            *result["native_contributors"],
            *result["derived_contributors"],
            *result["native_slr"],
        ]
    )


def test_native_cumulative_amount_parents_units_and_zero_are_preserved():
    view = amount_view(amount=[[0, 0.01], [0.02, 0.03]])
    view.manifest["events"][0]["normalization"]["method"] = "native_cumulative_end_minus_start"
    view.dataset["native_start_amount"] = (
        ("event", "y", "x"),
        np.full((1, 2, 2), 0.005),
        {"units": "m"},
    )
    view.dataset.native_end_amount.values[:] += 0.005
    result = run([view])["native_contributors"][0]
    assert result["value"] == 0.015
    assert result["spatial_extraction"]["native_start_amount_at_source_corners"] == [0.005] * 4
    assert result["raw_grib_unit"] == "unknown" and result["native_unit"] == "m"
    assert run([amount_view(amount=0)])["native_contributors"][0]["value"] == 0
    view.dataset.native_end_amount.values[0, 0, 0] += 0.001
    invalid = run([view])["native_contributors"][0]
    assert invalid["value"] is None and "reproduce" in invalid["missing_reasons"][0]


def test_exact_first_hour_does_not_require_a_fabricated_zero_lead_parent_after_concat():
    first = amount_view(amount=0.001, valid="2026-09-11T13:00:00Z")
    second = amount_view(amount=0.002, valid="2026-09-11T14:00:00Z")
    second.dataset.native_end_amount.values[:] = 0.003
    second.dataset["native_start_amount"] = (
        ("event", "y", "x"),
        np.full((1, 2, 2), 0.001),
        {"units": "m"},
    )
    second.manifest["events"][0]["normalization"]["method"] = "native_cumulative_end_minus_start"
    combined = AmountView(
        xr.concat([first.dataset, second.dataset], dim="event").assign_coords(event=[0, 1]),
        first.crs,
        {**first.manifest, "events": [first.manifest["events"][0], second.manifest["events"][0]]},
    )
    assert np.isnan(combined.dataset.native_start_amount.values[0]).all()
    one = run([combined], valid="2026-09-11T13:00:00Z")["native_contributors"][0]
    two = run([combined], valid="2026-09-11T14:00:00Z")["native_contributors"][0]
    assert one["value"] == 0.001 and two["value"] == 0.002
    assert "native_start_amount_at_source_corners" not in one["spatial_extraction"]
    assert one["value"] + two["value"] == pytest.approx(0.003, abs=1e-12)


def test_native_amounts_different_intervals_are_not_forced_into_hourly_comparison():
    result = run([amount_view(), amount_view("NBM", hours=3)])
    comparison = next(row for row in result["comparisons"] if row["models"] == ["HRRR", "NBM"])
    assert (
        comparison["status"] == "incompatible" and comparison["difference_left_minus_right"] is None
    )
    assert "intervals" in comparison["missing_reasons"][0]
    assert (
        run([amount_view()], valid="2026-09-11T16:00:00Z")["native_contributors"][0]["value"]
        is None
    )


def test_snow_and_snow_plus_sleet_are_not_numerically_compared_as_identical_amounts():
    result = run([amount_view(), amount_view("NBM"), amount_view("RAP")])
    pairs = {
        tuple(row["models"]): row
        for row in result["comparisons"]
        if row["methods"] == ["native", "native"]
    }
    assert pairs["HRRR", "RAP"]["status"] == "comparable"
    assert pairs["HRRR", "NBM"]["status"] == "incompatible"
    assert pairs["HRRR", "NBM"]["difference_left_minus_right"] is None
    assert pairs["HRRR", "NBM"]["hydrometeor_scopes"] == ["snow", "snow_and_sleet"]
    assert "hydrometeor" in pairs["HRRR", "NBM"]["missing_reasons"][0]
    assert all(
        "profile" not in row and "native_slr" not in row for row in result["native_contributors"]
    )


@pytest.mark.parametrize("amount", [np.nan, -0.01])
def test_invalid_native_amount_does_not_become_zero(amount):
    result = run([amount_view(amount=[[amount, 0.01], [0.01, 0.01]])])
    assert result["native_contributors"][0]["value"] is None
    assert result["native_contributors"][0]["missing_reasons"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("native_quantity", "snowpack_depth"),
        ("native_unit", "in"),
        ("unit_factor_to_m", 0.01),
        ("source_lead_hours", 4),
        ("temporal_semantics", "instantaneous"),
    ],
)
def test_native_source_quantity_unit_and_time_mismatch_remains_unavailable(key, value):
    view = amount_view()
    view.manifest["events"][0][key] = value
    source = run([view])["native_contributors"][0]
    assert source["value"] is None and source["missing_reasons"]


def test_instant_nbm_ratio_does_not_require_or_modify_native_amount():
    view = amount_view("NBM", amount=np.nan, native_ratio=0)
    result = run([view])
    assert result["native_contributors"][-1]["value"] is None
    assert result["native_slr"][0]["value"] == 0
    assert result["native_slr"][0]["unit"] == "1"
    assert result["native_slr"][0]["interval_start"] is None
    view.manifest["events"][0]["native_slr"]["source_lead_hours"] = 2
    assert run([view])["native_slr"][0]["value"] is None


def test_complete_profile_masking_and_offline_repeated_extraction_are_exact():
    view = amount_view("RAP", profile_temperature=261.16)
    view.dataset.surface_pressure.values[:] = 85000
    view.dataset.temperature_profile.values[:, view.dataset.level.values > 850] = np.nan
    water = snow_view("RAP", amount=2)
    datasets = [view.dataset.copy(deep=True), water.dataset.copy(deep=True)]
    manifests = [deepcopy(view.manifest), deepcopy(water.manifest)]
    one = run([view], swe_views=[water])
    two = run([view], swe_views=[water])
    assert one == two and json.dumps(one, allow_nan=False) == json.dumps(two, allow_nan=False)
    derived = one["derived_contributors"][0]
    assert derived["value"] == pytest.approx(0.044, abs=1e-12)
    assert all(
        value is None
        for profile in derived["spatial_extraction"]["temperature_profile_k_at_source_corners"][15:]
        for value in profile
    )
    for dataset, manifest, current in zip(datasets, manifests, [view, water], strict=True):
        xr.testing.assert_identical(dataset, current.dataset)
        assert manifest == current.manifest


@pytest.mark.parametrize(
    "failure", ["profile_level", "profile_value", "t2m", "pressure", "metadata", "time"]
)
def test_missing_profile_or_wrong_profile_time_cannot_use_surface_only_fallback(failure):
    view = amount_view("RAP")
    if failure == "profile_level":
        view = AmountView(view.dataset.isel(level=slice(1, None)), view.crs, view.manifest)
    elif failure == "profile_value":
        view.dataset.temperature_profile.values[0, 0, 0, 0] = np.nan
    elif failure == "t2m":
        view.dataset.temperature_2m.values[0, 0, 0] = np.nan
    elif failure == "pressure":
        view.dataset.surface_pressure.values[0, 0, 0] = np.nan
    elif failure == "metadata":
        view.manifest["events"][0]["profile"]["complete"] = False
    else:
        view.manifest["events"][0]["profile"]["valid_time"] = "2026-09-11T14:00:00Z"
    result = run([view], swe_views=[snow_view("RAP", amount=0)])
    assert result["derived_contributors"][0]["value"] is None
    assert result["derived_contributors"][0]["missing_reasons"]
    assert result["native_contributors"][2]["value"] == 0.01


def test_nonpositive_ratio_is_unavailable_for_positive_water_but_zero_water_stays_zero():
    view = amount_view("RAP", profile_temperature=278.16)
    positive = run([view], swe_views=[snow_view("RAP", amount=1)])["derived_contributors"][0]
    assert positive["value"] is None and "Nonpositive" in positive["missing_reasons"][0]
    assert positive["spatial_extraction"]["ratio_at_source_corners"] == pytest.approx(
        [-2] * 4, abs=1e-12
    )
    assert (
        positive["spatial_extraction"]["ratio_status_at_source_corners"]
        == ["ineligible_nonpositive_ratio"] * 4
    )
    zero = run([view], swe_views=[snow_view("RAP", amount=0)])["derived_contributors"][0]
    assert zero["value"] == 0 and zero["spatial_extraction"][
        "zero_swe_nonpositive_ratio_corners"
    ] == [0, 1, 2, 3]
    assert (
        zero["spatial_extraction"]["ratio_status_at_source_corners"]
        == ["inapplicable_zero_swe"] * 4
    )
    assert (
        zero["diagnostic_ratio_status"]
        == "raw_formula_only_contains_nonpositive_inapplicable_ratios"
    )


@pytest.mark.parametrize(
    "failure",
    ["missing", "different_cycle", "three_hour", "different_axes", "duplicate", "negative"],
)
def test_kuchera_requires_unique_same_native_grid_cycle_and_hour_snow_water(failure):
    view = amount_view("RAP")
    water = snow_view("RAP")
    waters = [water]
    if failure == "missing":
        waters = []
    elif failure == "different_cycle":
        water.manifest["events"][0]["source_cycle"] = "2026-09-11T11:00:00Z"
    elif failure == "three_hour":
        waters = [snow_view("RAP", hours=3)]
    elif failure == "different_axes":
        water.dataset.coords["x"] = [-94.1, -93.1]
    elif failure == "duplicate":
        waters.append(water)
    else:
        water.dataset.amount.values[0, 0, 0] = -0.1
    result = run([view], swe_views=waters)["derived_contributors"][0]
    assert result["value"] is None and result["missing_reasons"]


def test_unsupported_sources_preserve_native_product_reason_and_no_snowpack_difference():
    result = run(
        [], source_status={"GFS": {"missing_reason": "SNOD is snowpack, not new snowfall"}}
    )
    assert result["native_contributors"][1]["missing_reasons"] == [
        "SNOD is snowpack, not new snowfall"
    ]
    assert result["field"]["value"] is None
