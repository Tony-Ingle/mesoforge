"""Native ice and liquid intervals remain distinct, reproducible nearest-cell evidence."""

from copy import deepcopy
from datetime import datetime, timedelta

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application.ice import IceView, aggregate_ice_intervals, extract_ice_contributors
from mesoforge.forecasting.ice import FLAT_ICE, FRZR
from mesoforge.guidance.sources.ice import SOURCES

CYCLE = "2026-09-11T12:00:00Z"
VALID = "2026-09-11T18:00:00Z"


def ice_view(
    source_id="NBM_FICEAC_1H",
    amount=1.0,
    *,
    end=VALID,
    hours=None,
    native_unit="kg m**-2",
    factor=1.0,
    parent_amount=2.0,
):
    """One event, 2x2 native grid; FRZR preserves both cumulative parent samples."""
    source = deepcopy(SOURCES[source_id])
    duration = hours if hours is not None else (source["duration_hours"] or 1)
    valid = datetime.fromisoformat(end)
    start = (valid - timedelta(hours=duration)).isoformat()
    values = np.broadcast_to(np.asarray(amount, dtype=float), (1, 2, 2)).copy()
    cumulative = source["quantity_kind"] == "freezing_rain_liquid_equivalent"
    difference = cumulative and datetime.fromisoformat(start) > datetime.fromisoformat(CYCLE)
    event = {
        **source,
        "source_cycle": CYCLE,
        "source_lead_hours": (valid - datetime.fromisoformat(CYCLE)).total_seconds() / 3600,
        "valid_time": end,
        "interval_start": start,
        "interval_end": end,
        "interval_closure": "left_open_right_closed",
        "temporal_semantics": "accumulation",
        "duration_hours": duration,
        "unit": "kg/m^2",
        "native_unit": native_unit,
        "native_factor_to_canonical": factor,
        "spatial_support": "native_model_grid",
        "normalization": {
            "method": "native_cumulative_end_minus_start_then_unit_factor"
            if difference
            else "native_interval_amount_times_unit_factor",
            "unit_factor_to_kg_m2": factor,
        },
        "version": {"adapter_contract": "native_ice_and_freezing_rain_amount_v1"},
        "missing_reasons": [],
        "grib_keys": {"parameterNumber": 228 if not cumulative else 225},
    }
    if difference:
        event["native_interval_start"] = CYCLE
    parent = np.broadcast_to(np.asarray(parent_amount, dtype=float), values.shape).copy()
    native_end = values / factor + (parent if difference else 0.0)
    data = {
        "amount": (("event", "y", "x"), values, {"units": "kg/m^2"}),
        "native_end_amount": (("event", "y", "x"), native_end, {"units": native_unit}),
    }
    if difference:
        data["native_start_amount"] = (("event", "y", "x"), parent, {"units": native_unit})
    native_events = []
    for bound in [start, end] if difference else [end]:
        native = deepcopy(event)
        native.update(
            interval_start=CYCLE if cumulative else start,
            interval_end=bound,
            valid_time=bound,
            source_lead_hours=(
                datetime.fromisoformat(bound) - datetime.fromisoformat(CYCLE)
            ).total_seconds()
            / 3600,
        )
        native_events.append(
            {"raw_sha256": "a" * 64, "native_event": native, "url": "https://example.test/ice"}
        )
    event["provenance"] = {"parents": native_events}
    ds = xr.Dataset(data, coords={"event": [0], "x": [-94.0, -93.0], "y": [44.0, 45.0]})
    return IceView(
        ds,
        pyproj.CRS.from_epsg(4326),
        {
            "source_id": source_id,
            "model": source["model"],
            "events": [event],
            "manifest_sha256": "b" * 64,
            "prepared_file": {"sha256": "c" * 64},
        },
    )


def run(views, *, valid=VALID, latitude=44.25, longitude=-93.75, **kwargs):
    return extract_ice_contributors(
        views, latitude=latitude, longitude=longitude, valid_time=valid, **kwargs
    )


def row(result, source_id="NBM_FICEAC_1H"):
    return next(value for value in result["contributors"] if value["source_id"] == source_id)


def test_distinct_native_ice_and_liquid_evidence_have_no_active_blend_or_thickness_conversion():
    result = run([ice_view(amount=0.123456789), ice_view("HRRR_FRZR", 0.75)])
    for field in (FLAT_ICE, FRZR):
        assert result["fields"][field]["value"] is None
        assert result["fields"][field]["status"] == "policy_unavailable"
        assert result["fields"][field]["weights"] == {}
    assert row(result)["value"] == 0.123456789
    assert row(result, "HRRR_FRZR")["value"] == 0.75
    assert all(
        item["active_weight"] == 0 and item["role"] == "shadow" for item in result["contributors"]
    )
    assert row(result)["quantity_kind"] != row(result, "HRRR_FRZR")["quantity_kind"]
    assert all("display_inches" not in item for item in result["contributors"])


def test_nearest_cell_retains_original_native_parents_and_provenance_without_smoothing():
    view = ice_view("HRRR_FRZR", [[0.75, 2.0], [3.0, 4.0]], parent_amount=5)
    result = run([view])
    source = row(result, "HRRR_FRZR")
    assert source["value"] == source["native_value"] == 0.75
    sample = source["spatial_extraction"]
    assert sample["native_start_amount"] == 5 and sample["native_end_amount"] == 5.75
    assert sample["source_x"] == sample["source_y"] == [0]
    assert sample["method"] == "nearest_native_grid_cell" and sample["weights"] == [1.0]
    assert source["provenance"] == view.manifest["events"][0]["provenance"]
    assert source["source_cycle"] == CYCLE and source["source_lead_hours"] == 6
    assert source["manifest_sha256"] == "b" * 64 and source["prepared_file"]["sha256"] == "c" * 64
    assert source["interval_start"] == "2026-09-11T17:00:00+00:00"
    assert source["native_interval_start"] == CYCLE


def test_native_six_hour_ice_is_not_silently_mixed_with_hourly_or_liquid():
    result = run([ice_view(), ice_view("NBM_FICEAC_6H", 4), ice_view("HRRR_FRZR", 2)])
    source = row(result, "NBM_FICEAC_6H")
    assert source["value"] == 4 and source["duration_hours"] == 6
    assert datetime.fromisoformat(source["interval_start"]) == datetime.fromisoformat(CYCLE)
    assert all(item["status"] == "incompatible" for item in result["comparisons"])
    assert any(
        "quantities" in reason
        for item in result["comparisons"]
        for reason in item["missing_reasons"]
    )


def test_same_hour_liquid_contributors_preserve_disagreement_without_new_weights():
    result = run([ice_view("HRRR_FRZR", 0.5), ice_view("RAP_FRZR", 1.25)])
    comparison = next(
        item for item in result["comparisons"] if item["source_ids"] == ["HRRR_FRZR", "RAP_FRZR"]
    )
    assert comparison["status"] == "comparable"
    assert comparison["difference_left_minus_right"] == -0.75
    assert result["fields"][FRZR]["value"] is None


def test_zero_missing_unsupported_and_unacquired_are_explicitly_distinct():
    zero = row(run([ice_view(amount=0)]))
    assert zero["value"] == 0 and zero["status"] == "available"
    missing = row(run([ice_view()], valid="2026-09-11T19:00:00Z"))
    assert missing["value"] is None and "temporal filling" in missing["missing_reasons"][0]
    result = run(
        [],
        source_status={
            "NBM_FICEAC_1H": {"status": "evidence", "missing_reasons": ["Not acquired"]}
        },
    )
    assert row(result)["status"] == "unavailable" and row(result)["missing_reasons"] == [
        "Not acquired"
    ]
    assert row(result, "IFS")["status"] == "unsupported" and row(result, "IFS")["value"] is None


@pytest.mark.parametrize("value", [-0.001, np.nan, np.inf])
def test_invalid_nearest_amount_stays_missing_without_clipping_or_neighbor_filling(value):
    assert row(run([ice_view(amount=[[value, 1], [2, 3]])]))["value"] is None


def test_missing_neighbor_preserves_valid_nearest_sample_and_ties_are_stable():
    result = run([ice_view(amount=[[1, np.nan], [3, 4]])], latitude=44.5, longitude=-93.5)
    assert row(result)["value"] == 1
    assert row(result)["spatial_extraction"]["source_x"] == [0]
    assert row(run([ice_view()], latitude=47))["value"] is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("quantity_kind", "ice_thickness"),
        ("unit", "m"),
        ("native_unit", "mm"),
        ("native_factor_to_canonical", 1000),
        ("source_lead_hours", 7),
        ("temporal_semantics", "instantaneous"),
        ("interval_closure", "both_closed"),
        ("duration_hours", 2),
        ("accretion_geometry", "radial_ice"),
    ],
)
def test_incompatible_native_quantity_units_geometry_or_time_remain_missing(key, value):
    view = ice_view()
    view.manifest["events"][0][key] = value
    result = row(run([view]))
    assert result["value"] is None and result["missing_reasons"]


def test_corrupted_or_missing_cumulative_parent_never_becomes_a_liquid_increment():
    view = ice_view("HRRR_FRZR", 0.5)
    view.dataset.native_start_amount.values[:] = 4
    assert row(run([view]), "HRRR_FRZR")["value"] is None
    view = ice_view("HRRR_FRZR", 0.5)
    view.dataset.native_start_amount.values[:] = np.nan
    assert row(run([view]), "HRRR_FRZR")["value"] is None
    view = ice_view("HRRR_FRZR", 0.5)
    view.dataset.amount.values[:] = 1
    assert "reproduce" in row(run([view]), "HRRR_FRZR")["missing_reasons"][0]


def test_complete_hourly_partition_conserves_cumulative_liquid_and_retains_each_parent():
    rows = [
        row(
            run([ice_view("HRRR_FRZR", amount, end=end, parent_amount=parent)], valid=end),
            "HRRR_FRZR",
        )
        for amount, parent, end in [(0.25, 2, "2026-09-11T17:00:00Z"), (0.75, 2.25, VALID)]
    ]
    result = aggregate_ice_intervals(
        rows, start_valid_time="2026-09-11T16:00:00Z", end_valid_time=VALID
    )
    assert result["status"] == "available" and result["value"] == 3 - 2
    assert (
        result["components"] == rows
        and result["quantity_kind"] == "freezing_rain_liquid_equivalent"
    )
    for bad in ([rows[0]], [rows[0], rows[0]], [rows[0], row(run([ice_view()]))]):
        unavailable = aggregate_ice_intervals(
            bad, start_valid_time="2026-09-11T16:00:00Z", end_valid_time=VALID
        )
        assert unavailable["value"] is None and unavailable["missing_reasons"]


def test_repeat_and_serialized_offline_replay_preserve_exact_evidence_and_parents(
    tmp_path, monkeypatch
):
    import socket

    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **kw: pytest.fail("Provider call forbidden")
    )
    view = ice_view("HRRR_FRZR", [[0.25, 0.5], [0.75, 1]])
    before = deepcopy(view.manifest)
    first = run([view])
    path = tmp_path / "ice.nc"
    view.dataset.to_netcdf(path, engine="h5netcdf")
    with xr.open_dataset(path, engine="h5netcdf") as opened:
        replay = IceView(opened.load(), view.crs, deepcopy(view.manifest))
    assert run([replay]) == first == run([view])
    assert view.manifest == before
