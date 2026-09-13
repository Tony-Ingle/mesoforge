"""Native ice amounts retain separate quantities, parents and shared offline inputs."""

import json
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application import prepared_ice as prepared
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition, SelectedMessage
from mesoforge.guidance.index_parsing import GribIndexError, IndexRow
from mesoforge.guidance.sources.ice import SOURCES

CYCLE = "2026-09-11T18:00:00Z"
CYCLE_TIME = datetime.fromisoformat(CYCLE)
TARGET = np.datetime64(CYCLE.removesuffix("Z"), "ns")


@pytest.fixture
def source(tmp_path, monkeypatch):
    root, control = tmp_path / "original", tmp_path / "control"
    root.mkdir()
    control.mkdir()
    (control / "coverage.json").write_text(
        json.dumps(
            {
                "regions": [
                    {"area": {"south": 44.1, "north": 44.5, "west": -93.9, "east": -93.5}},
                    {"area": {"south": 44.5, "north": 44.9, "west": -93.5, "east": -93.1}},
                ]
            }
        )
    )
    original = {
        "directory": str(control),
        "shadow_directories": {"RAP": "retained-shadow"},
        "snowfall_guidance": {"original": "SWE unchanged"},
        "snowfall_amount_guidance": {"original": "snowfall/SLR/Kuchera unchanged"},
        "ptype_guidance": {"original": "p-type unchanged"},
        "probability_sources": [{"original": "PoP shadows unchanged"}],
        "pop_guidance": {"selected_cycle": CYCLE, "original": "active PoP unchanged"},
        "cloud_guidance": {"original": "cloud unchanged"},
        "visibility_guidance": {"original": "visibility unchanged"},
        "thunder_guidance": {"original": "thunder unchanged"},
        "current_model_set": {
            "selection": {
                "surface_fields": True,
                "target_reference_time": CYCLE,
                "selected_cycles": {model: CYCLE for model in ("HRRR", "GFS", "RAP", "IFS")},
            }
        },
    }
    (root / "preparation.json").write_text(json.dumps(original))
    calls = []

    def acquire(source_id, cycle, lead, **kwargs):
        calls.append((source_id, lead))
        if SOURCES[source_id]["duration_hours"] and lead % SOURCES[source_id]["duration_hours"]:
            raise GribIndexError("Fixture has no native ice interval at that endpoint")
        payload = f"{source_id}/{lead}".encode()
        return Phase2LeadAcquisition(
            model=SOURCES[source_id]["model"],
            cycle_date=cycle.date(),
            cycle_hour=cycle.hour,
            forecast_hour=lead,
            endpoint="fixture",
            resolved_grib_url=f"https://example.invalid/{source_id}/{lead}",
            resolved_index_url=f"https://example.invalid/{source_id}/{lead}.idx",
            index_payload=b"fixture native accumulation inventory",
            index_attempts=(),
            index_completed_at=cycle,
            selected_messages=(
                SelectedMessage(
                    "ice_amount", IndexRow(1, 0, "fixture accumulation"), 0, len(payload), payload
                ),
            ),
            grib_attempts=(),
            grib_completed_at=cycle,
            full_object_etag="fixture",
            full_object_last_modified="Fri, 11 Sep 2026 18:00:00 GMT",
            full_object_content_length=len(payload),
            index_available_at=cycle,
            grib_available_at=cycle,
            index_last_modified=None,
        )

    def decode(payload, source_id, cycle, lead):
        assert payload == f"{source_id}/{lead}".encode()
        metadata = deepcopy(SOURCES[source_id])
        native = (
            np.zeros((2, 2))
            if lead == 1
            else np.array([[lead, lead + 1], [lead + 2, lead + 3]], dtype=float)
        )
        ds = xr.Dataset(
            {
                "amount": (("y", "x"), native, {"units": "kg/m^2"}),
                "native_amount": (("y", "x"), native, {"units": metadata["native_unit"]}),
            },
            coords={"x": [-94.0, -93.0], "y": [44.0, 45.0]},
        )
        metadata.update(
            source_id=source_id,
            source_cycle=cycle.isoformat().replace("+00:00", "Z"),
            source_lead_hours=lead,
            valid_time=(cycle + timedelta(hours=lead)).isoformat().replace("+00:00", "Z"),
            interval_start=(cycle + timedelta(hours=lead - (metadata["duration_hours"] or lead)))
            .isoformat()
            .replace("+00:00", "Z"),
            interval_end=(cycle + timedelta(hours=lead)).isoformat().replace("+00:00", "Z"),
            temporal_semantics="accumulation",
            duration_hours=metadata["duration_hours"] or lead,
            spatial_support="native_model_grid",
            version={"adapter_contract": "fixture"},
            interval_closure="left_open_right_closed",
            unit="kg/m^2",
            missing_reasons=[],
        )
        return ds, pyproj.CRS.from_epsg(4326), metadata

    monkeypatch.setattr(prepared, "acquire_ice_lead", acquire)
    monkeypatch.setattr(prepared, "decode_ice_lead", decode)
    return root, original, calls


def build(root, output):
    return prepared.prepare_ice_run(root, output, transport=SimpleNamespace(downloaded_bytes=0))


def test_distinct_intervals_shared_regions_and_exact_offline_replay(tmp_path, monkeypatch, source):
    root, original, calls = source
    first = build(root, tmp_path / "one")
    descriptor = first["ice_guidance"]
    assert len(calls) == len(set(calls)) == 124
    assert all(first[key] == value for key, value in original.items())
    assert {r["source_id"]: r["available_times"] for r in descriptor["sources"]} == {
        "NBM_FICEAC_1H": 36,
        "NBM_FICEAC_6H": 6,
        "HRRR_FRZR": 36,
        "RAP_FRZR": 21,
    }
    views = prepared.load_ice_guidance(descriptor, target_reference_time=TARGET)
    assert len(views) == 8 and all(v.dataset.sizes["event"] == 36 for v in views)
    nbm = next(v for v in views if v.manifest["source_id"] == "NBM_FICEAC_6H")
    assert np.isnan(nbm.dataset.amount[0]).all() and np.isfinite(nbm.dataset.amount[5]).all()
    frzr = next(v for v in views if v.manifest["source_id"] == "HRRR_FRZR")
    assert np.all(frzr.dataset.amount[0] == 0)  # native f001 needs no invented f000
    assert np.all(frzr.dataset.amount[5] == 1)  # independently: (6+offset)-(5+offset)
    event = frzr.manifest["events"][5]
    assert event["interval_start"] == "2026-09-11T23:00:00Z"
    assert event["interval_end"] == "2026-09-12T00:00:00Z" and event["duration_hours"] == 1
    assert [p["lead"] for p in event["provenance"]["parents"]] == [5, 6]
    assert event["quantity_kind"] != nbm.manifest["events"][5]["quantity_kind"]
    assert event["unit"] == nbm.manifest["events"][5]["unit"] == "kg/m^2"

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline ice replay must not construct a transport or acquire")

    monkeypatch.setattr(prepared, "BoundedHttpTransport", forbidden)
    monkeypatch.setattr(prepared, "acquire_ice_lead", forbidden)
    second = prepared.prepare_ice_run(tmp_path / "one", tmp_path / "two", from_raw=True)
    assert second["ice_guidance"]["downloaded_bytes"] == 0 and len(calls) == 124
    loaded = prepared.load_ice_guidance(second["ice_guidance"], target_reference_time=TARGET)
    for before, after in zip(views, loaded, strict=True):
        xr.testing.assert_identical(before.dataset, after.dataset)
        for key in ("events", "inputs", "request_failures"):
            assert before.manifest[key] == after.manifest[key]


def test_missing_cumulative_parent_is_missing_not_zero(tmp_path, monkeypatch, source):
    from mesoforge.guidance.http_fetch import FetchError

    root, _, _ = source
    acquire = prepared.acquire_ice_lead

    def missing(source_id, cycle, lead, **kwargs):
        if source_id == "HRRR_FRZR" and lead == 5:
            raise FetchError("missing parent")
        return acquire(source_id, cycle, lead, **kwargs)

    monkeypatch.setattr(prepared, "acquire_ice_lead", missing)
    first = build(root, tmp_path / "one")
    views = prepared.load_ice_guidance(first["ice_guidance"], target_reference_time=TARGET)
    hrrr = next(v for v in views if v.manifest["source_id"] == "HRRR_FRZR")
    assert np.isnan(hrrr.dataset.amount[4:6]).all()
    assert hrrr.manifest["events"][5]["missing_reasons"]
    assert np.all(hrrr.dataset.amount[6] == 1)


def test_offset_cycles_acquire_parent_once_and_align_by_valid_time(tmp_path, source):
    from mesoforge.application.ice import extract_ice_contributors

    root, original, calls = source
    original["current_model_set"]["selection"]["selected_cycles"].update(
        HRRR="2026-09-11T12:00:00Z", RAP="2026-09-11T15:00:00Z"
    )
    (root / "preparation.json").write_text(json.dumps(original))
    result = build(root, tmp_path / "offset")
    assert calls.count(("HRRR_FRZR", 6)) == calls.count(("RAP_FRZR", 3)) == 1
    views = prepared.load_ice_guidance(result["ice_guidance"], target_reference_time=TARGET)
    rows = extract_ice_contributors(
        views, latitude=44.2, longitude=-93.8, valid_time="2026-09-11T19:00:00Z"
    )["contributors"]
    for model, lead in (("HRRR", 7), ("RAP", 4)):
        row = next(r for r in rows if r["source_id"] == model + "_FRZR")
        assert row["value"] == 1 and row["source_lead_hours"] == lead
        assert row["interval_start"] == CYCLE and row["duration_hours"] == 1
        assert [p["lead"] for p in row["provenance"]["parents"]] == [lead - 1, lead]


@pytest.mark.parametrize("change", ["quantity_kind", "source_cycle", "interval_end", "native_unit"])
def test_incompatible_cumulative_parents_rejected(change):
    end, start, metadata, parent = interval_fixture()
    parent[change] = "incompatible"
    with pytest.raises(ValueError, match="parents differ"):
        prepared._normalized_interval(end, metadata, start, parent, "2026-09-11T19:00:00Z")


def interval_fixture():
    end = xr.Dataset(
        {"native_amount": (("y", "x"), [[0.0, 1.0, 3.0]], {"units": "kg m**-2"})},
        coords={"x": [1, 2, 3], "y": [1]},
    )
    start = end.copy(deep=True)
    start.native_amount.values[:] = [[0.0, 2.0, 1.0]]
    metadata = {
        **deepcopy(SOURCES["HRRR_FRZR"]),
        "source_cycle": CYCLE,
        "interval_start": CYCLE,
        "interval_end": "2026-09-11T20:00:00Z",
        "interval_closure": "left_open_right_closed",
        "version": {},
        "spatial_support": "native_model_grid",
    }
    parent = {**metadata, "interval_end": "2026-09-11T19:00:00Z"}
    return end, start, metadata, parent


def test_cumulative_units_conservation_and_negative_remains_missing():
    end, start, metadata, parent = interval_fixture()
    result, event = prepared._normalized_interval(
        end, metadata, start, parent, parent["interval_end"]
    )
    assert result.amount.attrs["units"] == "kg/m^2"
    assert result.amount.values[0, 0] == 0 and np.isnan(result.amount.values[0, 1])
    assert result.amount.values[0, 2] == 2  # no liquid-to-ice or arbitrary density conversion
    assert float(result.amount[0, 2] + start.native_amount[0, 2]) == float(end.native_amount[0, 2])
    assert event["normalization"]["method"] == "native_cumulative_end_minus_start_then_unit_factor"


def test_corrupt_retained_input_rejected(tmp_path, source):
    root, _, _ = source
    first = build(root, tmp_path / "one")
    raw = next((tmp_path / "one" / "HRRR_FRZR" / "raw").glob("*.grib2"))
    raw.write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        prepared.load_ice_guidance(first["ice_guidance"], target_reference_time=TARGET)
    with pytest.raises(ValueError):
        prepared.prepare_ice_run(tmp_path / "one", tmp_path / "two", from_raw=True)
