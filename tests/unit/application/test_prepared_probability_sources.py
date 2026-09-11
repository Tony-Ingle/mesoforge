"""Native-probability retention/replay at the existing provider-decoder boundary."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application import prepared_probability_sources as prepared
from mesoforge.application.probability_contributors import extract_probability_contributors
from mesoforge.guidance.sources.probabilistic import PRODUCTS

CYCLE = datetime(2026, 9, 11, tzinfo=UTC)
TARGET = np.datetime64("2026-09-11T00:00:00", "ns")


def _record(source="GEFS_6H", *, start=0, end=6, native=25):
    raw = str(native).encode()
    inventory = b"bounded native probability inventory fixture"
    evidence = {
        "request": {
            "source_id": source,
            "cycle": "2026-09-11T00:00:00Z",
            "start_hour": start,
            "end_hour": end,
        },
        "source_grib_url": f"https://example.invalid/{source}/f{end}",
        "source_index_url": f"https://example.invalid/{source}/f{end}.idx",
        "byte_start": 123,
        "byte_end": 123 + len(raw),
        "etag": f"fixture-{source}-{end}",
        "full_object_content_length": 10000,
        "grib_retrieved_at": "2026-09-11T01:00:00Z",
        "index_retrieved_at": "2026-09-11T00:59:00Z",
        "grib_available_at": "2026-09-11T00:50:00Z",
    }
    return evidence, raw, inventory


@pytest.fixture
def decoder(monkeypatch):
    """Only decoding is replaced; real retention, crop, NetCDF, hashes and replay run."""
    calls = []

    def decode(payload, source_id, cycle, start_hour, end_hour):
        calls.append((source_id, start_hour, end_hour))
        native = np.full((8, 9), float(payload))
        if payload == b"101":
            native[:] = 25
            native[0, 0] = 101
        values = np.where(
            np.isfinite(native) & (native >= 0) & (native <= 100), native / 100, np.nan
        )
        dataset = xr.Dataset(
            {
                "probability": (
                    ("y", "x"),
                    values,
                    {"units": "1", "temporal_semantics": "probability"},
                ),
                "native_probability": (("y", "x"), native, {"units": "%"}),
            },
            coords={"x": np.arange(-98, -89, dtype=float), "y": np.arange(42, 50, dtype=float)},
        )
        event = {
            key: deepcopy(value) for key, value in PRODUCTS[source_id].items() if key != "expected"
        }
        event.update(
            interval_start=(cycle + timedelta(hours=start_hour)).isoformat().replace("+00:00", "Z"),
            interval_end=(cycle + timedelta(hours=end_hour)).isoformat().replace("+00:00", "Z"),
            interval_closure="left_open_right_closed",
            decode_fixture_notice="Synthetic native percentage array at decoder boundary",
        )
        return dataset, pyproj.CRS.from_epsg(4326), event

    monkeypatch.setattr(prepared, "decode_product", decode)
    return calls


@pytest.fixture
def original(tmp_path):
    control = tmp_path / "control"
    control.mkdir()
    (control / "manifest.json").write_text(
        json.dumps(
            {
                "prepared_area": {"south": 42.0, "north": 49.0, "west": -98.0, "east": -90.0},
            }
        )
    )
    value = {
        "directory": str(control),
        "current_model_set": {
            "selection": {
                "target_reference_time": "2026-09-11T00:00:00Z",
                "surface_fields": True,
                "unchanged_four_model_evidence": "fixture",
            }
        },
        "pop_guidance": {"status": "prepared", "directory": "unchanged-active-NBM"},
        "shadow_directories": {"RAP": "unchanged-RAP", "IFS": "unchanged-IFS"},
        "retained_raw_bytes": 123456,
    }
    root = tmp_path / "prepared-run"
    root.mkdir()
    (root / "preparation.json").write_text(json.dumps(value))
    return root, value


def test_sources_retained_once_for_multiple_regions_and_event_provenance_survives(
    tmp_path, decoder, original
):
    _, source = original
    coverage = {
        "regions": [
            {"area": {"south": 44.0, "north": 45.0, "west": -96.0, "east": -95.0}},
            {"area": {"south": 46.0, "north": 47.0, "west": -93.0, "east": -92.0}},
        ]
    }
    (Path(source["directory"]) / "coverage.json").write_text(json.dumps(coverage))
    records = [_record(), _record(start=6, end=12, native=75), _record("NBM_6H", native=50)]
    descriptors = prepared.prepare_retained_sources(source, tmp_path / "retained", records)
    assert len(decoder) == 3  # Each native message decoded once, independent of regional count.
    assert len(list((tmp_path / "retained").rglob("*.grib2"))) == 3
    views = prepared.load_probability_sources(descriptors, target_reference_time=TARGET)
    assert len(views) == 4
    gefs = [view for view in views if view.manifest["source_id"] == "GEFS_6H"]
    assert len(gefs) == 2
    assert gefs[0].manifest["inputs"] == gefs[1].manifest["inputs"]
    for view in gefs:
        np.testing.assert_array_equal(view.dataset.probability[0], 0.25)
        np.testing.assert_array_equal(view.dataset.probability[1], 0.75)
        provenance = view.manifest["events"][0]["provenance"]
        assert provenance["grib_available_at"] != provenance["grib_retrieved_at"]
        assert provenance["raw_sha256"] == hashlib.sha256(records[0][1]).hexdigest()
        assert provenance["source_grib_url"] == records[0][0]["source_grib_url"]
        assert view.manifest["role"] == "shadow" and view.manifest["active_weight"] == 0


def test_raw_only_replay_no_provider_calls_preserves_active_sources_and_exact_fields(
    tmp_path, monkeypatch, decoder, original
):
    root, original_data = original
    original_bytes = (root / "preparation.json").read_bytes()
    first = prepared.prepare_probability_sources(
        root, tmp_path / "first", retained_records=[_record(), _record("NBM_6H", native=50)]
    )
    views = prepared.load_probability_sources(
        first["probability_sources"], target_reference_time=TARGET
    )
    for descriptor in first["probability_sources"]:
        for path in (Path(descriptor["directory"]) / "regions").glob("*.nc"):
            path.unlink()  # Rebuilding must only require raw inputs and manifests.

    def forbidden(*args, **kwargs):
        raise AssertionError("Raw replay must not create transport or acquire anything")

    monkeypatch.setattr(prepared, "BoundedHttpTransport", forbidden)
    monkeypatch.setattr(prepared, "acquire_product", forbidden)
    replay = prepared.prepare_probability_sources(
        tmp_path / "first", tmp_path / "replay", from_raw=True
    )
    rebuilt = prepared.load_probability_sources(
        replay["probability_sources"], target_reference_time=TARGET
    )
    for old, new in zip(views, rebuilt, strict=True):
        xr.testing.assert_identical(old.dataset, new.dataset)
        assert old.manifest["inputs"] == new.manifest["inputs"]
        assert old.manifest["events"] == new.manifest["events"]
    for key in ("directory", "current_model_set", "pop_guidance", "shadow_directories"):
        assert replay[key] == original_data[key]
    assert replay["probability_source_run"]["downloaded_bytes"] == 0
    assert (root / "preparation.json").read_bytes() == original_bytes


@pytest.mark.parametrize("artifact", ["raw", "index", "prepared", "manifest"])
def test_loader_rejects_corrupted_retained_artifacts(tmp_path, decoder, original, artifact):
    _, source = original
    descriptor = prepared.prepare_retained_sources(source, tmp_path / "retained", [_record()])[0]
    directory = Path(descriptor["directory"])
    manifest = json.loads((directory / "manifest.json").read_bytes())
    file = {
        "raw": manifest["inputs"][0]["raw_file"],
        "index": manifest["inputs"][0]["index_file"],
        "prepared": manifest["regions"][0]["file"],
        "manifest": "manifest.json",
    }[artifact]
    path = directory / file
    path.write_bytes(path.read_bytes() + b"corruption")
    with pytest.raises(ValueError, match="checksum"):
        prepared.load_probability_sources([descriptor], target_reference_time=TARGET)


def test_invalid_native_percentage_remains_local_while_valid_point_can_extract(
    tmp_path, decoder, original
):
    _, source = original
    descriptor = prepared.prepare_retained_sources(
        source, tmp_path / "retained", [_record(native=101)]
    )
    views = prepared.load_probability_sources(descriptor, target_reference_time=TARGET)
    assert np.isnan(views[0].dataset.probability.values[0, 0, 0])
    assert views[0].dataset.native_probability.values[0, 0, 0] == 101
    result = extract_probability_contributors(
        views,
        latitude=44.5,
        longitude=-93.5,
        valid_time="2026-09-11T06:00:00Z",
        active={},
    )
    assert result["contributors"][0]["value"] == 0.25


def test_out_of_window_duplicate_requests_and_nonzero_shadow_weight_are_rejected(
    tmp_path, decoder, original
):
    _, source = original
    with pytest.raises(ValueError, match="36-hour"):
        prepared.prepare_retained_sources(source, tmp_path / "outside", [_record(start=30, end=42)])
    with pytest.raises(ValueError, match="Duplicate"):
        prepared.prepare_retained_sources(source, tmp_path / "duplicate", [_record(), _record()])
    assert not decoder
    descriptor = prepared.prepare_retained_sources(source, tmp_path / "retained", [_record()])[0]
    descriptor["active_weight"] = 0.1
    with pytest.raises(ValueError, match="zero-weight"):
        prepared.load_probability_sources([descriptor], target_reference_time=TARGET)
