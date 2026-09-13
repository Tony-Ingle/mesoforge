"""Cloud preparation shares retained source messages and replays exact native evidence."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import xarray as xr

from mesoforge.application import prepared_cloud as prepared
from mesoforge.application.cloud_cover import extract_cloud_contributors
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition, SelectedMessage
from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import IndexRow
from mesoforge.guidance.sources.cloud import SOURCES
from tests.unit.application.test_cloud_cover import CYCLE, cloud_view

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
        "snowfall_guidance": {"original": "SWE untouched"},
        "snowfall_amount_guidance": {"original": "native/SLR/Kuchera untouched"},
        "ptype_guidance": {"original": "p-type untouched"},
        "probability_sources": [{"original": "probability sources untouched"}],
        "pop_guidance": {"selected_cycle": CYCLE, "original": "PoP untouched"},
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
            index_payload=b"fixture native total cloud index",
            index_attempts=(),
            index_completed_at=cycle,
            selected_messages=(
                SelectedMessage(
                    "total_cloud_cover",
                    IndexRow(1, 0, "fixture TCDC"),
                    0,
                    len(payload),
                    payload,
                ),
            ),
            grib_attempts=(),
            grib_completed_at=cycle,
            full_object_etag="fixture",
            full_object_last_modified="Fri, 11 Sep 2026 12:00:00 GMT",
            full_object_content_length=len(payload),
            index_available_at=cycle,
            grib_available_at=cycle,
            index_last_modified=None,
        )

    def decode(payload, model, cycle, lead):
        assert payload == f"{model}/{lead}".encode() and cycle == CYCLE_TIME
        valid = (cycle + timedelta(hours=lead)).isoformat().replace("+00:00", "Z")
        values = 0 if model == "HRRR" and lead == 1 else [[lead, lead + 1], [lead + 2, lead + 3]]
        metadata = SOURCES[model]
        view = cloud_view(
            model,
            values,
            end=valid,
            native_unit=metadata["native_unit"],
            factor=metadata["native_factor_to_percent"],
        )
        return view.dataset.isel(event=0, drop=True), view.crs, view.manifest["events"][0]

    monkeypatch.setattr(prepared, "acquire_cloud_lead", acquire)
    monkeypatch.setattr(prepared, "decode_cloud_lead", decode)
    return root, original, calls


def build(root, output):
    return prepared.prepare_cloud_run(root, output, transport=SimpleNamespace(downloaded_bytes=0))


def test_shared_regions_native_hours_and_complete_offline_replay(tmp_path, monkeypatch, source):
    root, original, calls = source
    original_bytes = (root / "preparation.json").read_bytes()
    first = build(root, tmp_path / "one")
    descriptor = first["cloud_guidance"]
    assert len(calls) == len(set(calls)) == 141  # H36/G36/R21/N36/I12, shared across both regions.
    assert all(lead % 3 == 0 for model, lead in calls if model == "IFS")
    assert all(first[key] == value for key, value in original.items())
    assert (root / "preparation.json").read_bytes() == original_bytes
    assert {row["model"]: row["available_times"] for row in descriptor["sources"]} == {
        "HRRR": 36,
        "GFS": 36,
        "RAP": 21,
        "NBM": 36,
        "IFS": 12,
    }
    views = prepared.load_cloud_guidance(descriptor, target_reference_time=TARGET)
    assert len(views) == 10 and all(view.dataset.sizes["event"] == 36 for view in views)
    for latitude, longitude in [(44.2, -93.8), (44.8, -93.2)]:
        result = extract_cloud_contributors(
            views,
            latitude=latitude,
            longitude=longitude,
            valid_time="2026-09-11T15:00:00Z",
        )
        assert all(row["status"] == "available" for row in result["contributors"])
        assert all(
            row["active_weight"] == (1.0 if row["model"] == "NBM" else 0.0)
            for row in result["contributors"]
        )
        assert result["field"]["weights"] == {"NBM": 1.0}
        assert result["field"]["cloud_percentage"] is not None
    ifs = next(view for view in views if view.manifest["model"] == "IFS")
    assert np.isnan(ifs.dataset.cloud_cover.values[:2]).all()
    assert np.isfinite(ifs.dataset.cloud_cover.values[2]).all()
    assert all(ifs.manifest["events"][i]["missing_reasons"] for i in (0, 1, 3, 4))
    hrrr = next(view for view in views if view.manifest["model"] == "HRRR")
    assert np.all(hrrr.dataset.cloud_cover.values[0] == 0)
    assert np.all(hrrr.dataset.cloud_cover.values[1] > 0)
    assert hrrr.manifest["events"][0]["provenance"] == hrrr.manifest["inputs"][0]

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Offline cloud replay must not construct transport or acquire guidance"
        )

    monkeypatch.setattr(prepared, "BoundedHttpTransport", forbidden)
    monkeypatch.setattr(prepared, "acquire_cloud_lead", forbidden)
    replay = prepared.prepare_cloud_run(tmp_path / "one", tmp_path / "two", from_raw=True)
    assert replay["cloud_guidance"]["downloaded_bytes"] == 0 and len(calls) == 141
    assert all(replay[key] == value for key, value in original.items())
    reloaded = prepared.load_cloud_guidance(replay["cloud_guidance"], target_reference_time=TARGET)
    for before, after in zip(views, reloaded, strict=True):
        xr.testing.assert_identical(before.dataset, after.dataset)
        assert before.manifest["inputs"] == after.manifest["inputs"]
        assert before.manifest["events"] == after.manifest["events"]
        assert before.manifest["request_failures"] == after.manifest["request_failures"]


def test_historical_preparation_contract_loads_and_replays_without_rewriting_it(
    tmp_path, monkeypatch, source
):
    root, _, _ = source
    # Produce the exact historical contract, then use the current reader/preparer.
    with monkeypatch.context() as historical:
        historical.setattr(prepared, "POLICY", prepared.LEGACY_POLICY)
        old = build(root, tmp_path / "historical")
    before = {path: path.read_bytes() for path in (tmp_path / "historical").rglob("manifest.json")}
    views = prepared.load_cloud_guidance(old["cloud_guidance"], target_reference_time=TARGET)
    assert views
    replay = prepared.prepare_cloud_run(
        tmp_path / "historical", tmp_path / "current", from_raw=True
    )
    assert replay["cloud_guidance"]["downloaded_bytes"] == 0
    assert replay["cloud_guidance"]["policy"] == prepared.POLICY
    current = prepared.load_cloud_guidance(replay["cloud_guidance"], target_reference_time=TARGET)
    for previous, rebuilt in zip(views, current, strict=True):
        xr.testing.assert_identical(previous.dataset, rebuilt.dataset)
        assert previous.manifest["events"] == rebuilt.manifest["events"]
    assert all(path.read_bytes() == payload for path, payload in before.items())


def test_missing_provider_field_keeps_hour_axis_and_does_not_fabricate_clear_sky(
    tmp_path,
    monkeypatch,
    source,
):
    root, _, _ = source
    acquire = prepared.acquire_cloud_lead

    def missing(model, cycle, lead, **kwargs):
        if model == "HRRR" and lead == 2:
            raise FetchError("fixture total-cloud message unavailable")
        return acquire(model, cycle, lead, **kwargs)

    monkeypatch.setattr(prepared, "acquire_cloud_lead", missing)
    result = build(root, tmp_path / "one")
    views = prepared.load_cloud_guidance(result["cloud_guidance"], target_reference_time=TARGET)
    hrrr = next(view for view in views if view.manifest["model"] == "HRRR")
    assert np.all(hrrr.dataset.cloud_cover.values[0] == 0)
    assert np.isnan(hrrr.dataset.cloud_cover.values[1]).all()
    assert np.all(hrrr.dataset.cloud_cover.values[2] > 0)
    assert hrrr.manifest["events"][1]["source_lead_hours"] == 2
    assert "message unavailable" in hrrr.manifest["events"][1]["missing_reasons"][0]


def test_corrupt_raw_changed_policy_and_wrong_target_cannot_be_replayed(tmp_path, source):
    root, _, _ = source
    result = build(root, tmp_path / "one")
    descriptor = result["cloud_guidance"]
    policy = deepcopy(descriptor)
    policy["policy"]["weights"] = {"HRRR": 1}
    with pytest.raises(ValueError, match="policy"):
        prepared.load_cloud_guidance(policy, target_reference_time=TARGET)
    with pytest.raises(ValueError, match="target"):
        prepared.load_cloud_guidance(
            descriptor, target_reference_time=TARGET + np.timedelta64(1, "h")
        )
    raw = next((tmp_path / "one" / "HRRR" / "raw").glob("*.grib2"))
    raw.write_bytes(b"corrupted raw cloud")
    with pytest.raises(ValueError):
        prepared.load_cloud_guidance(descriptor, target_reference_time=TARGET)
    with pytest.raises(ValueError):
        prepared.prepare_cloud_run(tmp_path / "one", tmp_path / "two", from_raw=True)


def test_acquisition_identity_cannot_be_relabelled(tmp_path, monkeypatch, source):
    root, _, _ = source
    acquire = prepared.acquire_cloud_lead
    monkeypatch.setattr(
        prepared,
        "acquire_cloud_lead",
        lambda *args, **kwargs: replace(acquire(*args, **kwargs), model="OTHER"),
    )
    with pytest.raises(ValueError, match="identity"):
        build(root, tmp_path / "one")


def test_loaded_array_unit_is_checked_even_when_content_hash_matches(tmp_path, source):
    root, _, _ = source
    result = build(root, tmp_path / "one")
    descriptor = result["cloud_guidance"]
    source = descriptor["sources"][0]
    directory = Path(source["directory"])
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    region = manifest["regions"][0]
    path = directory / region["file"]
    with xr.open_dataset(path, engine="h5netcdf") as opened:
        changed = opened.load()
    changed.cloud_cover.attrs["units"] = "1"
    changed.to_netcdf(path, engine="h5netcdf")
    region.update(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    manifest_path.write_text(json.dumps(manifest))
    source["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="units"):
        prepared.load_cloud_guidance(descriptor, target_reference_time=TARGET)


def test_missing_selected_cycle_has_no_acquisition_and_is_explicit(tmp_path, source):
    root, original, calls = source
    del original["current_model_set"]["selection"]["selected_cycles"]["IFS"]
    (root / "preparation.json").write_text(json.dumps(original))
    result = build(root, tmp_path / "one")
    assert all(model != "IFS" for model, _ in calls)
    status = result["cloud_guidance"]["source_status"]["IFS"]
    assert "No selected cycle" in status["missing_reason"]
    assert all(row["model"] != "IFS" for row in result["cloud_guidance"]["sources"])


def test_entire_source_failure_preserves_request_reasons_on_readback(tmp_path, monkeypatch, source):
    root, _, _ = source
    acquire = prepared.acquire_cloud_lead

    def missing(model, cycle, lead, **kwargs):
        if model == "HRRR":
            raise FetchError("native inventory has no total-cloud message")
        return acquire(model, cycle, lead, **kwargs)

    monkeypatch.setattr(prepared, "acquire_cloud_lead", missing)
    result = build(root, tmp_path / "one")
    descriptor = result["cloud_guidance"]
    views = prepared.load_cloud_guidance(descriptor, target_reference_time=TARGET)
    assert all(view.manifest["model"] != "HRRR" for view in views)
    evidence = extract_cloud_contributors(
        views,
        latitude=44.2,
        longitude=-93.8,
        valid_time="2026-09-11T13:00:00Z",
        source_status=descriptor["source_status"],
    )
    row = next(row for row in evidence["contributors"] if row["model"] == "HRRR")
    assert row["status"] == "unavailable" and row["value"] is None
    assert "no total-cloud message" in row["missing_reasons"][0]
    assert len(row["request_failures"]) == 36
