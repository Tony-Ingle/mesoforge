"""Shared native snowfall preparation, compatible parent differences and raw replay."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application import prepared_snowfall as prepared
from mesoforge.application.snowfall_forecast import extract_snowfall_contributors
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition, SelectedMessage
from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import IndexRow
from mesoforge.guidance.sources.snowfall import SOURCES

CYCLE = datetime(2026, 9, 11, tzinfo=UTC)
TARGET = np.datetime64("2026-09-11T00:00:00", "ns")


def native(model, lead, *, amount=None):
    cumulative = model == "IFS"
    unit, factor = ("m of water equivalent", 1000.0) if cumulative else ("kg m**-2", 1.0)
    value = lead * 0.002 if cumulative else (0 if lead == 1 else {"HRRR": 2, "RAP": 3}[model])
    values = np.broadcast_to(
        np.asarray(value if amount is None else amount, dtype=float), (4, 4)
    ).copy()
    ds = xr.Dataset(
        {
            "native_amount": (("y", "x"), values, {"units": unit}),
            "amount": (("y", "x"), values * factor, {"units": "kg/m^2"}),
        },
        coords={"y": [43.0, 44.0, 45.0, 46.0], "x": [-95.0, -94.0, -93.0, -92.0]},
    )
    event = {
        **deepcopy(SOURCES[model]),
        "source_cycle": "2026-09-11T00:00:00Z",
        "source_lead_hours": lead,
        "valid_time": (CYCLE + timedelta(hours=lead)).isoformat().replace("+00:00", "Z"),
        "interval_start": (CYCLE + timedelta(hours=0 if cumulative else lead - 1))
        .isoformat()
        .replace("+00:00", "Z"),
        "interval_end": (CYCLE + timedelta(hours=lead)).isoformat().replace("+00:00", "Z"),
        "interval_closure": "left_open_right_closed",
        "temporal_semantics": "accumulation",
        "native_unit": unit,
        "unit_factor_to_kg_m2": factor,
        "spatial_support": "native_model_grid",
        "version": {"model_version": "fixture-v1"},
        "missing_reasons": [],
    }
    return ds, event


@pytest.fixture
def source(tmp_path, monkeypatch):
    root, control = tmp_path / "original", tmp_path / "control"
    root.mkdir()
    control.mkdir()
    (control / "coverage.json").write_text(
        json.dumps(
            {
                "regions": [
                    {"area": {"south": 44, "north": 45, "west": -94, "east": -93}},
                    {"area": {"south": 44.6, "north": 45.4, "west": -93.4, "east": -92.6}},
                ]
            }
        )
    )
    original = {
        "directory": str(control),
        "shadow_directories": {},
        "ptype_guidance": {"original": "must remain unchanged"},
        "pop_guidance": {"original": "must remain unchanged"},
        "current_model_set": {
            "selection": {
                "surface_fields": True,
                "target_reference_time": "2026-09-11T00:00:00Z",
                "selected_cycles": {
                    model: "2026-09-11T00:00:00Z" for model in ("HRRR", "GFS", "RAP", "IFS")
                },
            }
        },
    }
    (root / "preparation.json").write_text(json.dumps(original))
    calls = []

    def acquire(model, cycle, lead, **kwargs):
        calls.append((model, lead))
        payload = f"{model}/{lead}".encode()
        return Phase2LeadAcquisition(
            model=model,
            cycle_date=cycle.date(),
            cycle_hour=cycle.hour,
            forecast_hour=lead,
            endpoint="fixture",
            resolved_grib_url=f"https://example.invalid/{model}/{lead}",
            resolved_index_url=f"https://example.invalid/{model}/{lead}.idx",
            index_payload=b"fixture native snowfall index",
            index_attempts=(),
            index_completed_at=cycle,
            selected_messages=(
                SelectedMessage(
                    "snowfall_water_equivalent", IndexRow(1, 0, "fixture"), 0, len(payload), payload
                ),
            ),
            grib_attempts=(),
            grib_completed_at=cycle,
            full_object_etag="fixture",
            full_object_last_modified="Fri, 11 Sep 2026 00:00:00 GMT",
            full_object_content_length=len(payload),
            index_available_at=cycle,
            grib_available_at=cycle,
            index_last_modified=None,
        )

    def decode(payload, model, cycle, lead):
        assert payload == f"{model}/{lead}".encode() and cycle == CYCLE
        ds, event = native(model, lead)
        return ds, pyproj.CRS.from_epsg(4326), event

    monkeypatch.setattr(prepared, "acquire_snowfall_lead", acquire)
    monkeypatch.setattr(prepared, "decode_snowfall_lead", decode)
    return root, original, calls


def build(root, output, **kwargs):
    return prepared.prepare_snowfall_run(
        root, output, transport=SimpleNamespace(downloaded_bytes=0), **kwargs
    )


def test_shared_regions_use_one_acquisition_and_native_periods_replay_exactly(
    tmp_path, monkeypatch, source
):
    root, original, calls = source
    before = (root / "preparation.json").read_bytes()
    first = build(root, tmp_path / "one")
    descriptor = first["snowfall_guidance"]
    assert len(calls) == len(set(calls)) == 70  # HRRR36, RAP21, IFS13 cumulative parents.
    assert {r["model"]: r["available_intervals"] for r in descriptor["sources"]} == {
        "HRRR": 36,
        "RAP": 21,
        "IFS": 12,
    }
    assert all(first[key] == value for key, value in original.items())
    assert (root / "preparation.json").read_bytes() == before
    views = prepared.load_snowfall_guidance(descriptor, target_reference_time=TARGET)
    assert len(views) == 6  # Two automatically prepared regional views per source.
    for lat, lon in [(44.2, -93.8), (45.2, -92.8)]:
        result = extract_snowfall_contributors(
            views,
            latitude=lat,
            longitude=lon,
            valid_time="2026-09-11T03:00:00Z",
            source_status=descriptor["source_status"],
        )
        rows = {row["model"]: row for row in result["contributors"]}
        assert rows["HRRR"]["value"] == pytest.approx(2, abs=1e-12)
        assert rows["RAP"]["value"] == pytest.approx(3, abs=1e-12)
        assert rows["IFS"]["value"] == pytest.approx(6, abs=1e-12)
        assert rows["IFS"]["interval_start"] == "2026-09-11T00:00:00Z"
        assert rows["HRRR"]["interval_start"] == "2026-09-11T02:00:00Z"
        assert len(rows["IFS"]["provenance"]["parents"]) == 2
        assert rows["GFS"]["value"] is None and rows["NBM"]["value"] is None
        assert result["field"]["status"] == "policy_unavailable"

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline replay must not use any provider")

    monkeypatch.setattr(prepared, "BoundedHttpTransport", forbidden)
    monkeypatch.setattr(prepared, "acquire_snowfall_lead", forbidden)
    replay = prepared.prepare_snowfall_run(tmp_path / "one", tmp_path / "two", from_raw=True)
    assert replay["snowfall_guidance"]["downloaded_bytes"] == 0 and len(calls) == 70
    reloaded = prepared.load_snowfall_guidance(
        replay["snowfall_guidance"], target_reference_time=TARGET
    )
    for a, b in zip(views, reloaded, strict=True):
        xr.testing.assert_identical(a.dataset, b.dataset)
        assert a.manifest["events"] == b.manifest["events"]
        assert a.manifest["inputs"] == b.manifest["inputs"]
        assert a.manifest["request_failures"] == b.manifest["request_failures"]
    with pytest.raises(ValueError, match="new directory"):
        build(root, tmp_path / "one")


def test_missing_cumulative_parent_and_unsupported_native_leads_remain_missing(
    tmp_path, monkeypatch, source
):
    root, _, _ = source
    acquire = prepared.acquire_snowfall_lead

    def missing_parent(model, cycle, lead, **kwargs):
        if model == "IFS" and lead == 3:
            raise FetchError("fixture missing native parent")
        return acquire(model, cycle, lead, **kwargs)

    monkeypatch.setattr(prepared, "acquire_snowfall_lead", missing_parent)
    result = build(root, tmp_path / "one")
    views = prepared.load_snowfall_guidance(
        result["snowfall_guidance"], target_reference_time=TARGET
    )
    ifs = next(view for view in views if view.manifest["model"] == "IFS")
    assert (
        ifs.manifest["events"][0]["missing_reasons"]
        and ifs.manifest["events"][1]["missing_reasons"]
    )
    assert np.isnan(ifs.dataset.amount.values[:2]).all()
    np.testing.assert_allclose(ifs.dataset.amount.values[2:], 6, rtol=0, atol=1e-12)
    rap = next(view for view in views if view.manifest["model"] == "RAP")
    assert np.isnan(rap.dataset.amount.values[21:]).all()
    assert all(event["missing_reasons"] for event in rap.manifest["events"][21:])
    replay = prepared.prepare_snowfall_run(tmp_path / "one", tmp_path / "two", from_raw=True)
    second = prepared.load_snowfall_guidance(
        replay["snowfall_guidance"], target_reference_time=TARGET
    )
    for a, b in zip(views, second, strict=True):
        xr.testing.assert_identical(a.dataset, b.dataset)
        assert a.manifest["events"] == b.manifest["events"]


def test_raw_corruption_wrong_target_and_changed_policy_are_rejected(tmp_path, source):
    root, _, _ = source
    result = build(root, tmp_path / "one")
    changed = deepcopy(result["snowfall_guidance"])
    changed["policy"]["weights"] = {"HRRR": 1}
    with pytest.raises(ValueError, match="policy"):
        prepared.load_snowfall_guidance(changed, target_reference_time=TARGET)
    with pytest.raises(ValueError, match="target"):
        prepared.load_snowfall_guidance(
            result["snowfall_guidance"], target_reference_time=TARGET + np.timedelta64(1, "h")
        )
    raw = next((tmp_path / "one" / "HRRR" / "raw").glob("*.grib2"))
    raw.write_bytes(b"changed")
    with pytest.raises(ValueError):
        prepared.load_snowfall_guidance(result["snowfall_guidance"], target_reference_time=TARGET)
    with pytest.raises(ValueError):
        prepared.prepare_snowfall_run(tmp_path / "one", tmp_path / "two", from_raw=True)


def test_acquisition_source_identity_is_not_silently_relabelled(tmp_path, monkeypatch, source):
    root, _, _ = source
    acquire = prepared.acquire_snowfall_lead

    def wrong_model(*args, **kwargs):
        return replace(acquire(*args, **kwargs), model="GFS")

    monkeypatch.setattr(prepared, "acquire_snowfall_lead", wrong_model)
    with pytest.raises(ValueError, match="identity"):
        build(root, tmp_path / "one")


def test_native_hour_amount_preserves_zero_and_invalid_cells_without_clipping():
    ds, event = native("HRRR", 1, amount=[[0, 2, -0.01, np.nan]] * 4)
    result, normalized = prepared._normalized_interval(ds, event)
    np.testing.assert_equal(result.amount.values[0], [0, 2, np.nan, np.nan])
    xr.testing.assert_identical(result.native_end_amount.rename("native_amount"), ds.native_amount)
    assert normalized["interval_start"] == event["interval_start"]
    assert normalized["interval_end"] == event["interval_end"]
    assert result.amount.attrs["units"] == "kg/m^2"


def test_native_cumulative_difference_conserves_water_and_keeps_parents():
    zero, zero_event = native("IFS", 0)
    three, three_event = native("IFS", 3)
    six, six_event = native("IFS", 6)
    first, event = prepared._normalized_interval(three, three_event, zero, zero_event)
    second, _ = prepared._normalized_interval(six, six_event, three, three_event)
    np.testing.assert_allclose(
        first.amount + second.amount, six.native_amount * 1000, rtol=0, atol=1e-12
    )
    assert event["interval_start"] == zero_event["interval_end"]
    assert event["interval_end"] == three_event["interval_end"]
    xr.testing.assert_identical(
        first.native_start_amount.rename("native_amount"), zero.native_amount
    )
    xr.testing.assert_identical(
        first.native_end_amount.rename("native_amount"), three.native_amount
    )
    decreasing, decrease_event = native("IFS", 6, amount=[[0.006, 0.005, np.nan, 0.008]] * 4)
    invalid, _ = prepared._normalized_interval(decreasing, decrease_event, three, three_event)
    np.testing.assert_allclose(
        invalid.amount.values[0], [0, np.nan, np.nan, 2], rtol=0, atol=1e-12, equal_nan=True
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("source_cycle", "2026-09-10T18:00:00Z"),
        ("interval_start", "2026-09-11T01:00:00Z"),
        ("native_quantity", "snowpack_water_storage"),
        ("native_unit", "m"),
        ("unit_factor_to_kg_m2", 1),
        ("spatial_support", "different_support"),
        ("version", {"model_version": "another-version"}),
    ],
)
def test_cumulative_parent_identity_or_semantic_mismatch_is_rejected(key, value):
    start, start_event = native("IFS", 3)
    end, end_event = native("IFS", 6)
    start_event[key] = value
    with pytest.raises(ValueError, match="incompatible"):
        prepared._normalized_interval(end, end_event, start, start_event)


def test_cumulative_parent_grid_mismatch_is_rejected():
    start, start_event = native("IFS", 3)
    end, end_event = native("IFS", 6)
    start.coords["x"] = [-94.9, -93.9, -92.9, -91.9]
    with pytest.raises(ValueError, match="grid"):
        prepared._normalized_interval(end, end_event, start, start_event)
