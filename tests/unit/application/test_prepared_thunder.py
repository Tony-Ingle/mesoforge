"""Thunder preparation retains distinct native events and reuses shared raw inputs."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application import prepared_thunder as prepared
from mesoforge.application.thunder import extract_thunder_contributors
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition, SelectedMessage
from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError, IndexRow
from mesoforge.guidance.sources.thunder import SOURCES

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
        if lead % SOURCES[source_id]["duration_hours"]:
            raise GribIndexError("Fixture has no native thunder event at that endpoint")
        payload = f"{source_id}/{lead}".encode()
        return Phase2LeadAcquisition(
            model="NBM",
            cycle_date=cycle.date(),
            cycle_hour=cycle.hour,
            forecast_hour=lead,
            endpoint="fixture",
            resolved_grib_url=f"https://example.invalid/{source_id}/{lead}",
            resolved_index_url=f"https://example.invalid/{source_id}/{lead}.idx",
            index_payload=b"fixture native TSTM inventory",
            index_attempts=(),
            index_completed_at=cycle,
            selected_messages=(
                SelectedMessage(
                    "thunder_probability", IndexRow(1, 0, "fixture TSTM"), 0, len(payload), payload
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
        assert payload == f"{source_id}/{lead}".encode() and cycle == CYCLE_TIME
        metadata = deepcopy(SOURCES[source_id])
        native = (
            np.zeros((2, 2))
            if lead == 1
            else np.array([[lead, lead + 1], [lead + 2, lead + 3]], dtype=float)
        )
        ds = xr.Dataset(
            {
                "thunder_probability": (("y", "x"), native * 0.01, {"units": "1"}),
                "native_probability": (("y", "x"), native, {"units": metadata["native_unit"]}),
            },
            coords={"x": [-94.0, -93.0], "y": [44.0, 45.0]},
        )
        metadata.update(
            source_id=source_id,
            source_cycle=CYCLE,
            source_lead_hours=lead,
            valid_time=(cycle + timedelta(hours=lead)).isoformat().replace("+00:00", "Z"),
            interval_start=(cycle + timedelta(hours=lead - metadata["duration_hours"]))
            .isoformat()
            .replace("+00:00", "Z"),
            interval_end=(cycle + timedelta(hours=lead)).isoformat().replace("+00:00", "Z"),
            temporal_semantics="interval_probability",
            interval_closure="left_open_right_closed",
            unit="1",
            missing_reasons=[],
        )
        return ds, pyproj.CRS.from_epsg(4326), metadata

    monkeypatch.setattr(prepared, "acquire_thunder_lead", acquire)
    monkeypatch.setattr(prepared, "decode_thunder_lead", decode)
    return root, original, calls


def build(root, output):
    return prepared.prepare_thunder_run(root, output, transport=SimpleNamespace(downloaded_bytes=0))


def test_native_periods_shared_regions_and_exact_offline_replay(tmp_path, monkeypatch, source):
    root, original, calls = source
    original_bytes = (root / "preparation.json").read_bytes()
    first = build(root, tmp_path / "one")
    descriptor = first["thunder_guidance"]
    assert (
        len(calls) == len(set(calls)) == 101
    )  # 36+34+31 inventory requests; exactly 54 native events.
    assert all(first[key] == value for key, value in original.items())
    assert (root / "preparation.json").read_bytes() == original_bytes
    assert {row["source_id"]: row["available_times"] for row in descriptor["sources"]} == {
        "NBM_1H": 36,
        "NBM_3H": 12,
        "NBM_6H": 6,
    }
    views = prepared.load_thunder_guidance(descriptor, target_reference_time=TARGET)
    assert len(views) == 6 and all(view.dataset.sizes["event"] == 36 for view in views)
    for latitude, longitude in [(44.2, -93.8), (44.8, -93.2)]:
        result = extract_thunder_contributors(
            views, latitude=latitude, longitude=longitude, valid_time="2026-09-12T00:00:00Z"
        )
        native = [r for r in result["contributors"] if r["source_id"].startswith("NBM_")]
        assert len(native) == 3 and all(row["status"] == "available" for row in native)
        assert {row["duration_hours"] for row in native} == {1, 3, 6}
        hourly = next(row for row in native if row["source_id"] == "NBM_1H")
        assert result["field"]["value"] == hourly["value"]
        assert all(row["active_weight"] == 0 for row in native if row is not hourly)
    hourly_view = views[0]
    assert np.all(hourly_view.dataset.thunder_probability.values[0] == 0)
    assert hourly_view.manifest["events"][0]["provenance"] == hourly_view.manifest["inputs"][0]
    three = next(view for view in views if view.manifest["source_id"] == "NBM_3H")
    assert np.isnan(three.dataset.thunder_probability.values[0]).all()
    assert np.isfinite(three.dataset.thunder_probability.values[2]).all()

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline replay may not construct a transport or acquire guidance")

    monkeypatch.setattr(prepared, "BoundedHttpTransport", forbidden)
    monkeypatch.setattr(prepared, "acquire_thunder_lead", forbidden)
    second = prepared.prepare_thunder_run(tmp_path / "one", tmp_path / "two", from_raw=True)
    assert second["thunder_guidance"]["downloaded_bytes"] == 0 and len(calls) == 101
    assert all(second[key] == value for key, value in original.items())
    reloaded = prepared.load_thunder_guidance(
        second["thunder_guidance"], target_reference_time=TARGET
    )
    for before, after in zip(views, reloaded, strict=True):
        xr.testing.assert_identical(before.dataset, after.dataset)
        for key in ("inputs", "events", "request_failures"):
            assert before.manifest[key] == after.manifest[key]


def test_missing_hour_remains_missing_not_zero_or_longer_period_substitution(
    tmp_path, monkeypatch, source
):
    root, _, _ = source
    original = prepared.acquire_thunder_lead

    def missing(source_id, cycle, lead, **kwargs):
        if source_id == "NBM_1H" and lead == 6:
            raise FetchError("native one-hour thunder message unavailable")
        return original(source_id, cycle, lead, **kwargs)

    monkeypatch.setattr(prepared, "acquire_thunder_lead", missing)
    result = build(root, tmp_path / "one")
    views = prepared.load_thunder_guidance(result["thunder_guidance"], target_reference_time=TARGET)
    evidence = extract_thunder_contributors(
        views, latitude=44.2, longitude=-93.8, valid_time="2026-09-12T00:00:00Z"
    )
    assert evidence["field"]["value"] is None
    hourly = next(row for row in evidence["contributors"] if row["source_id"] == "NBM_1H")
    assert "one-hour thunder message unavailable" in hourly["missing_reasons"][0]
    assert all(
        row["value"] is not None
        for row in evidence["contributors"]
        if row["source_id"] in ("NBM_3H", "NBM_6H")
    )


def test_corruption_policy_target_and_acquisition_identity_rejected(tmp_path, monkeypatch, source):
    root, _, _ = source
    result = build(root, tmp_path / "one")
    descriptor = result["thunder_guidance"]
    altered = deepcopy(descriptor)
    altered["policy"]["source_id"] = "NBM_6H"
    with pytest.raises(ValueError, match="policy"):
        prepared.load_thunder_guidance(altered, target_reference_time=TARGET)
    with pytest.raises(ValueError, match="target"):
        prepared.load_thunder_guidance(
            descriptor, target_reference_time=TARGET + np.timedelta64(1, "h")
        )
    raw = next((tmp_path / "one" / "NBM_1H" / "raw").glob("*.grib2"))
    raw.write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        prepared.load_thunder_guidance(descriptor, target_reference_time=TARGET)
    with pytest.raises(ValueError):
        prepared.prepare_thunder_run(tmp_path / "one", tmp_path / "two", from_raw=True)
    acquire = prepared.acquire_thunder_lead
    monkeypatch.setattr(
        prepared,
        "acquire_thunder_lead",
        lambda *args, **kwargs: replace(acquire(*args, **kwargs), model="OTHER"),
    )
    with pytest.raises(ValueError, match="identity"):
        build(root, tmp_path / "wrong")


def test_array_unit_checked_even_with_updated_checksum(tmp_path, source):
    root, _, _ = source
    descriptor = build(root, tmp_path / "one")["thunder_guidance"]
    source = descriptor["sources"][0]
    directory = Path(source["directory"])
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    region = manifest["regions"][0]
    path = directory / region["file"]
    with xr.open_dataset(path, engine="h5netcdf") as opened:
        changed = opened.load()
    changed.thunder_probability.attrs["units"] = "kg/m^2"
    changed.to_netcdf(path, engine="h5netcdf")
    region.update(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    manifest_path.write_text(json.dumps(manifest))
    source["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="units"):
        prepared.load_thunder_guidance(descriptor, target_reference_time=TARGET)


def test_no_nbm_cycle_does_not_download_or_invent_probability(tmp_path, source):
    root, original, calls = source
    del original["pop_guidance"]["selected_cycle"]
    (root / "preparation.json").write_text(json.dumps(original))
    descriptor = build(root, tmp_path / "one")["thunder_guidance"]
    assert calls == [] and descriptor["sources"] == []
    assert "No selected cycle" in descriptor["source_status"]["NBM_1H"]["missing_reason"]


def test_injected_retained_message_cannot_write_outside_source_directory(tmp_path):
    record = {
        "index_bytes": 1,
        "index_sha256": hashlib.sha256(b"i").hexdigest(),
        "index_file": "raw/index",
        "messages": [{"field": "thunder_probability", "raw_file": "../escaped.grib2"}],
    }
    with pytest.raises(ValueError, match="destination"):
        prepared._copy_input(tmp_path / "source", (record, {"thunder_probability": b"g"}, b"i"))
    assert not (tmp_path / "escaped.grib2").exists()
    assert not (tmp_path / "source" / "raw" / "index").exists()
