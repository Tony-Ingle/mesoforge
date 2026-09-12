"""Shared retention, exact source/time identity and offline p-type replay."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application import prepared_precipitation_type as prepared
from mesoforge.application.precipitation_type import TYPES, extract_precipitation_type
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition, SelectedMessage
from mesoforge.guidance.index_parsing import IndexRow
from mesoforge.guidance.sources.precipitation_type import SOURCES

CYCLE = datetime(2026, 9, 11, tzinfo=UTC)


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "original"
    root.mkdir()
    control = tmp_path / "control"
    control.mkdir()
    (control / "manifest.json").write_text(
        json.dumps({"prepared_area": {"south": 44, "north": 45, "west": -94, "east": -93}})
    )
    original = {
        "directory": str(control),
        "shadow_directories": {},
        "pop_guidance": {"selected_cycle": "2026-09-11T00:00:00Z"},
        "current_model_set": {
            "selection": {
                "surface_fields": True,
                "target_reference_time": "2026-09-11T00:00:00Z",
                "selected_cycles": {
                    m: "2026-09-11T00:00:00Z" for m in ("HRRR", "GFS", "RAP", "IFS")
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
            index_payload=b"fixture native index",
            index_attempts=(),
            index_completed_at=cycle,
            selected_messages=(
                SelectedMessage("fixture", IndexRow(1, 0, "fixture"), 0, len(payload), payload),
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

    def decode(payloads, model, cycle, lead):
        assert payloads["fixture"] == f"{model}/{lead}".encode()
        metadata = deepcopy(SOURCES[model])
        fields = (
            {"native_code": 1}
            if model == "IFS"
            else {name: (100 if model == "NBM" else 1) if name == "rain" else 0 for name in TYPES}
        )
        ds = xr.Dataset(
            {
                name: (
                    ("y", "x"),
                    np.full((4, 4), value, dtype=float),
                    {"units": metadata["native_unit"]},
                )
                for name, value in fields.items()
            },
            coords={"y": [43.5, 44.0, 45.0, 45.5], "x": [-94.5, -94.0, -93.0, -92.5]},
        )
        event = {
            **metadata,
            "valid_time": (cycle + timedelta(hours=lead)).isoformat().replace("+00:00", "Z"),
            "source_cycle": cycle.isoformat().replace("+00:00", "Z"),
            "source_lead_hours": lead,
            "temporal_semantics": "instantaneous",
            "interval_start": None,
            "interval_end": None,
        }
        return ds, pyproj.CRS.from_epsg(4326), event

    monkeypatch.setattr(prepared, "acquire_type_lead", acquire)
    monkeypatch.setattr(prepared, "decode_type_lead", decode)
    return root, original, calls


def test_five_sources_shared_and_native_gaps_preserved_with_exact_offline_replay(
    tmp_path, monkeypatch, source
):
    root, original, calls = source
    first = prepared.prepare_type_run(root, tmp_path / "one")
    assert len(calls) == len(set(calls)) == 141  # 00Z HRRR36 +GFS36 +RAP21 +IFS12 +NBM36
    assert {r["model"]: r["available_hours"] for r in first["ptype_guidance"]["sources"]} == {
        "HRRR": 36,
        "GFS": 36,
        "RAP": 21,
        "IFS": 12,
        "NBM": 36,
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("No provider use during replay")

    monkeypatch.setattr(prepared, "BoundedHttpTransport", forbidden)
    monkeypatch.setattr(prepared, "acquire_type_lead", forbidden)
    second = prepared.prepare_type_run(tmp_path / "one", tmp_path / "two", from_raw=True)
    assert second["ptype_guidance"]["downloaded_bytes"] == 0
    for key in ("directory", "shadow_directories", "pop_guidance", "current_model_set"):
        assert first[key] == second[key] == original[key]
    views = []
    for result in (first, second):
        views.append(
            prepared.load_type_guidance(
                result["ptype_guidance"],
                target_reference_time=np.datetime64("2026-09-11T00:00:00", "ns"),
            )
        )
    assert len(views[0]) == len(views[1]) == 5
    for a, b in zip(*views, strict=True):
        xr.testing.assert_identical(a.dataset, b.dataset)
        assert (
            a.manifest["events"] == b.manifest["events"]
            and a.manifest["inputs"] == b.manifest["inputs"]
        )
    points = [
        extract_precipitation_type(
            views[1], latitude=lat, longitude=lon, valid_time="2026-09-11T03:00:00Z"
        )
        for lat, lon in [(44.2, -93.8), (44.8, -93.2)]
    ]
    assert all(p["field"]["value"] == "rain" for p in points)
    assert len(calls) == 141
    missing = extract_precipitation_type(
        views[1], latitude=44.2, longitude=-93.8, valid_time="2026-09-11T20:00:00Z"
    )
    assert missing["field"]["value"] == "rain"  # Native IFS gap cannot change HRRR/GFS.
    assert missing["contributors"][3]["missing_reasons"]
    with pytest.raises(ValueError, match="new directory"):
        prepared.prepare_type_run(root, tmp_path / "one")


def test_retained_payload_corruption_and_policy_changes_are_rejected(tmp_path, source):
    root, _, _ = source
    result = prepared.prepare_type_run(root, tmp_path / "one")
    descriptor = deepcopy(result["ptype_guidance"])
    descriptor["policy"]["required_sources"] = ["HRRR"]
    with pytest.raises(ValueError, match="policy"):
        prepared.load_type_guidance(descriptor, target_reference_time=np.datetime64("2026-09-11"))
    raw = next((tmp_path / "one" / "HRRR" / "raw").glob("*.grib2"))
    raw.write_bytes(b"changed")
    with pytest.raises(ValueError):
        prepared.prepare_type_run(tmp_path / "one", tmp_path / "replay", from_raw=True)
