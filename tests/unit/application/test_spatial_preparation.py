"""Coordinate coverage from generated GRIB fixtures; no provider or service access."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import xarray as xr
from fastapi.testclient import TestClient

from mesoforge import api
from mesoforge.application import spatial_preparation
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.application.prepared_temperature import (
    prepare_temperature_guidance,
    rebuild_temperature_guidance,
)
from mesoforge.application.spatial_coverage import CoverageRequiredError, UnsupportedCoordinateError
from mesoforge.catalog.domains import BoundingBox
from tests.unit.application.test_batch_forecast import write_real_shadow_snapshot
from tests.unit.application.test_prepared_temperature import (
    EXTENDED_HORIZONS,
    GFS_CYCLE,
    TARGET,
    FixtureClock,
    FixtureSleeper,
    FixtureTransport,
    phase2_configuration,
)
from tests.unit.test_forecast_api import shadow_configuration

LOCATIONS = [
    {"lat": 45.8, "lon": -93.1},
    {"lat": 44.98, "lon": -93.27},
    {"lat": 45.9, "lon": -93.0},
]
OLD_AREA = BoundingBox(south=45.5, north=46.0, west=-93.5, east=-93.0)


def test_qpf_inputs_change_shared_coverage_identity_without_changing_legacy_identity():
    manifest = {
        "data_kind": "real_prepared_guidance",
        "target_reference_time": "2026-09-11T18:00:00Z",
        "configuration_sha256": "a" * 64,
        "inputs": [],
        "target_horizon_hours": list(range(1, 37)),
    }
    original = spatial_preparation.source_identity(manifest)
    assert original == hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    qpf = {
        **manifest,
        "qpf_fields": True,
        "qpf_inputs": [{"model": "HRRR", "source_lead_hours": 7, "raw_sha256": "b" * 64}],
    }
    assert spatial_preparation.source_identity(qpf) != original
    assert spatial_preparation.source_identity({**qpf, "prepared_area": "another view"}) == (
        spatial_preparation.source_identity(qpf)
    )
    assert spatial_preparation.source_identity({**qpf, "qpf_inputs": []}) != (
        spatial_preparation.source_identity(qpf)
    )


@pytest.fixture(scope="module")
def narrow_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("narrow-spatial-source")
    prepare_temperature_guidance(
        directory,
        configuration=phase2_configuration(),
        target_reference_time=TARGET,
        hrrr_cycle=TARGET,
        gfs_cycle=GFS_CYCLE,
        transport=FixtureTransport(EXTENDED_HORIZONS),
        clock=FixtureClock(),
        sleeper=FixtureSleeper(),
        target_horizon_hours=EXTENDED_HORIZONS,
        area=OLD_AREA,
    )
    return directory


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Mock:
    forbidden = Mock(side_effect=AssertionError("Spatial preparation attempted network access"))
    monkeypatch.setattr("requests.Session", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    # Windows' ASGI event loop uses a local socket pair internally. Block the
    # provider transport itself without preventing that in-process test machinery.
    monkeypatch.setattr(
        "mesoforge.guidance.sources.hrrr_transport.RequestsHrrrHttpTransport.get", forbidden
    )
    monkeypatch.setattr(
        "mesoforge.guidance.sources.hrrr_transport.RequestsHrrrHttpTransport.head", forbidden
    )
    return forbidden


def _inventory(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


def _assert_forecast(
    forecast: dict[str, Any], location: dict[str, float], manifest: dict[str, Any]
) -> None:
    assert forecast["latitude"] == location["lat"]
    assert forecast["longitude"] == location["lon"]
    assert forecast["target_reference_time"] == "2026-08-30T12:00:00Z"
    assert [hour["horizon_hours"] for hour in forecast["hours"]] == list(EXTENDED_HORIZONS)
    for horizon, hour in enumerate(forecast["hours"], start=1):
        # Independent constant-grid calculation: .7*(279+h) + .3*(289+h) = 282+h K.
        assert hour["temperature"]["value"] == pytest.approx(282 + horizon, abs=1e-6)
        assert hour["temperature"]["unit"] == "K"
        assert hour["valid_time"] == (TARGET + timedelta(hours=horizon)).isoformat().replace(
            "+00:00", "Z"
        )
        assert hour["missing_reasons"] == []
        assert len(hour["sources"]) == 2
        for source, model, age, weight in zip(
            hour["sources"], ("HRRR", "GFS"), (0, 6), (0.7, 0.3), strict=True
        ):
            original = next(
                row
                for row in manifest["inputs"]
                if row["model"] == model and row["source_lead_hours"] == horizon + age
            )
            assert source["model"] == model
            assert source["cycle"] == original["cycle"]
            assert source["source_lead_hours"] == horizon + age
            assert source["weight"] == weight
            assert source["raw_sha256"] == original["raw_sha256"]
            assert source["source_url"] == original["source_grib_url"]


@pytest.mark.parametrize("subset", [False, True])
def test_separate_shadow_regions_follow_shared_active_coverage_without_changing_identity(
    narrow_source, tmp_path, monkeypatch, subset
):
    cache = tmp_path / "coverage"
    baseline, original_report = spatial_preparation.ensure_coverage(
        LOCATIONS, narrow_source, cache_directory=cache
    )
    original = {
        (row["lat"], row["lon"]): baseline.forecast(latitude=row["lat"], longitude=row["lon"])
        for row in LOCATIONS
    }
    active_region = original_report["regions"][0]
    shadow_root = tmp_path / "shadow-regions"
    locations = [LOCATIONS[0], LOCATIONS[2]] if subset else LOCATIONS
    snapshot = write_real_shadow_snapshot(
        narrow_source if subset else Path(active_region["directory"]), shadow_root / "nearby"
    )
    (shadow_root / "coverage.json").write_text(
        json.dumps(
            {
                "regions": [
                    {
                        "area": {"south": 34.0, "north": 38.0, "west": -122.0, "east": -118.0},
                        "directory": "far-away-not-opened",
                    },
                    {
                        "area": OLD_AREA.model_dump() if subset else active_region["area"],
                        "directory": "nearby",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    forbidden = Mock(side_effect=AssertionError("Retained active coverage must not be rebuilt"))
    monkeypatch.setattr(spatial_preparation, "rebuild_temperature_guidance", forbidden)
    configuration = shadow_configuration()
    prepared, report = spatial_preparation.ensure_coverage(
        locations,
        cache,
        contributor_configuration=configuration,
        shadow_directories={"SYNTH_SHADOW": shadow_root},
    )
    assert report["source_identity"] == original_report["source_identity"]
    assert report["downloaded_bytes"] == 0
    reloaded = spatial_preparation.load_prepared(
        cache, configuration=configuration, shadow_directories={"SYNTH_SHADOW": shadow_root}
    )
    for row in locations:
        forecast = prepared.forecast(latitude=row["lat"], longitude=row["lon"])
        assert reloaded.forecast(latitude=row["lat"], longitude=row["lon"]) == forecast
        assert forecast["manifest_sha256"] == original[row["lat"], row["lon"]]["manifest_sha256"]
        for old_hour, hour in zip(
            original[row["lat"], row["lon"]]["hours"], forecast["hours"], strict=True
        ):
            assert {
                key: value for key, value in hour.items() if key != "shadow_sources"
            } == old_hour
            shadow = hour["shadow_sources"][0]
            assert shadow["temperature"]["value"] == pytest.approx(299 + hour["horizon_hours"])
            assert shadow["source_metadata"] == snapshot["source_metadata"]
    forbidden.assert_not_called()


def test_nonoverlapping_shadow_region_is_explicit_missingness(narrow_source, tmp_path):
    baseline = PreparedPointForecast.from_directory(narrow_source).forecast(
        latitude=45.8, longitude=-93.1
    )
    root = tmp_path / "shadow"
    shadow = root / "far-away"
    manifest = write_real_shadow_snapshot(narrow_source, shadow)
    path = shadow / "SYNTH_SHADOW.nc"
    with xr.open_dataset(path, engine="h5netcdf") as opened:
        dataset = opened.load().assign_coords(x=opened.x.values + 10.0)
    dataset.to_netcdf(path, engine="h5netcdf")
    manifest["prepared_files"]["SYNTH_SHADOW"]["sha256"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    (shadow / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "coverage.json").write_text(
        json.dumps(
            {
                "regions": [
                    {
                        "area": {"south": 34.0, "north": 38.0, "west": -122.0, "east": -118.0},
                        "directory": "far-away",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    forecast = PreparedPointForecast.from_directory(
        narrow_source,
        configuration=shadow_configuration(),
        shadow_directories={"SYNTH_SHADOW": root},
    ).forecast(latitude=45.8, longitude=-93.1)
    for original, hour in zip(baseline["hours"], forecast["hours"], strict=True):
        assert {key: value for key, value in hour.items() if key != "shadow_sources"} == original
        shadow = hour["shadow_sources"][0]
        assert shadow["temperature"]["value"] is None
        assert "no prepared shadow region" in shadow["missing_reasons"][0]


def test_overlapping_coordinates_share_one_rebuild_with_36_hours_and_original_raw_provenance(
    narrow_source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_network: Mock
) -> None:
    before = _inventory(narrow_source)
    original = json.loads((narrow_source / "manifest.json").read_text())
    with pytest.raises(CoverageRequiredError):
        PreparedPointForecast.from_directory(narrow_source).forecast(
            latitude=44.98, longitude=-93.27
        )
    rebuild = Mock(wraps=spatial_preparation.rebuild_temperature_guidance)
    monkeypatch.setattr(spatial_preparation, "rebuild_temperature_guidance", rebuild)
    prepared, report = spatial_preparation.ensure_coverage(
        LOCATIONS, narrow_source, cache_directory=tmp_path / "coverage"
    )
    rebuild.assert_called_once()
    assert len(report["footprints"]) == 3
    assert report["model_buffer_km"] == 50
    assert report["context_km"] == 150
    assert report["observation_search_km"] == 50
    assert report["downloaded_bytes"] == 0
    assert report["retained_raw_bytes"] == sum(row["raw_bytes"] for row in original["inputs"])
    assert len(report["regions"]) == 1
    region = report["regions"][0]
    assert region["status"] == "prepared_from_retained_raw"
    directory = Path(region["directory"])
    assert region["prepared_bytes"] == sum(
        (directory / f"{model}.nc").stat().st_size for model in ("HRRR", "GFS")
    )
    assert region["prepared_bytes"] > 0
    rebuilt = json.loads((directory / "manifest.json").read_text())
    assert rebuilt["inputs"] == original["inputs"]
    assert rebuilt["prepared_area"] == region["area"]
    assert rebuilt["source_manifest_sha256"] == before["manifest.json"]
    for location in LOCATIONS:
        forecast = prepared.forecast(latitude=location["lat"], longitude=location["lon"])
        _assert_forecast(forecast, location, original)
        assert forecast["manifest_sha256"] == region["manifest_sha256"]
        for hour in forecast["hours"]:
            for source in hour["sources"]:
                assert (
                    source["prepared_sha256"]
                    == rebuilt["prepared_files"][source["model"]]["sha256"]
                )
    assert _inventory(narrow_source) == before
    no_network.assert_not_called()


def test_repeat_reordered_and_subset_collections_reuse_prepared_region_without_rebuild(
    narrow_source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_network: Mock
) -> None:
    cache = tmp_path / "coverage"
    first, initial = spatial_preparation.ensure_coverage(
        LOCATIONS, narrow_source, cache_directory=cache
    )
    expected = {
        (location["lat"], location["lon"]): first.forecast(
            latitude=location["lat"], longitude=location["lon"]
        )
        for location in LOCATIONS
    }
    forbidden = Mock(side_effect=AssertionError("Sufficient guidance was rebuilt"))
    monkeypatch.setattr(spatial_preparation, "rebuild_temperature_guidance", forbidden)
    for locations in (LOCATIONS, list(reversed(LOCATIONS)), [LOCATIONS[1]]):
        prepared, report = spatial_preparation.ensure_coverage(locations, cache)
        assert report["downloaded_bytes"] == 0
        assert len(report["regions"]) == 1
        assert report["regions"][0]["status"] == "reused"
        assert report["regions"][0]["directory"] == initial["regions"][0]["directory"]
        for location in locations:
            assert (
                prepared.forecast(latitude=location["lat"], longitude=location["lon"])
                == (expected[location["lat"], location["lon"]])
            )
    forbidden.assert_not_called()
    no_network.assert_not_called()


def test_http_distinguishes_unprepared_coverage_from_invalid_or_native_domain_coordinates(
    narrow_source: Path, monkeypatch: pytest.MonkeyPatch, no_network: Mock
) -> None:
    forbidden = Mock(side_effect=AssertionError("HTTP attempted spatial preparation"))
    monkeypatch.setattr(spatial_preparation, "ensure_coverage", forbidden)
    monkeypatch.setattr(spatial_preparation, "rebuild_temperature_guidance", forbidden)
    with TestClient(api.create_app(narrow_source)) as client:
        unprepared = client.get("/forecast", params=LOCATIONS[1])
        invalid = client.get("/forecast", params={"lat": 91.0, "lon": -93.1})
        outside = client.get("/forecast", params={"lat": 0.0, "lon": 0.0})
    assert unprepared.status_code == 409
    assert unprepared.json()["code"] == "coverage_required"
    assert "prepare" in unprepared.json()["error"]
    for response in (invalid, outside):
        assert response.status_code == 422
        assert response.json()["code"] == "unsupported_coordinate"
    assert "native model domain" in outside.json()["error"]
    forbidden.assert_not_called()
    no_network.assert_not_called()


def test_collection_loader_deduplicates_shared_files_and_http_reuses_loaded_guidance(
    narrow_source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_network: Mock
) -> None:
    cache = tmp_path / "coverage"
    _, report = spatial_preparation.ensure_coverage(LOCATIONS, narrow_source, cache_directory=cache)
    # An index can refer to one reusable view from more than one planned region.
    report["regions"] *= 2
    (cache / "coverage.json").write_text(json.dumps(report))
    loader = Mock(wraps=PreparedPointForecast.from_directory)
    monkeypatch.setattr(PreparedPointForecast, "from_directory", loader)
    app = api.create_app(cache)
    from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION

    loader.assert_called_once_with(
        Path(report["regions"][0]["directory"]),
        configuration=DEFAULT_CONFIGURATION,
        shadow_directories=None,
    )
    before = _inventory(cache)
    forbidden = Mock(side_effect=AssertionError("HTTP attempted prepared-data I/O"))
    with monkeypatch.context() as request_patch:
        request_patch.setattr(Path, "read_bytes", forbidden)
        request_patch.setattr(Path, "read_text", forbidden)
        request_patch.setattr("xarray.open_dataset", forbidden)
        with TestClient(app) as client:
            responses = [client.get("/forecast", params=location) for location in LOCATIONS]
            repeat = client.get("/forecast", params=LOCATIONS[1])
    assert all(response.status_code == 200 for response in responses)
    assert repeat.content == responses[1].content
    original = json.loads((narrow_source / "manifest.json").read_text())
    for response, location in zip(responses, LOCATIONS, strict=True):
        _assert_forecast(response.json(), location, original)
    forbidden.assert_not_called()
    no_network.assert_not_called()
    assert _inventory(cache) == before


def test_distant_unsupported_region_does_not_block_supported_group_or_poison_repeat(
    narrow_source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_network: Mock
) -> None:
    cache = tmp_path / "coverage"
    unsupported = {"lat": 0.0, "lon": 0.0}
    locations = [LOCATIONS[0], unsupported, LOCATIONS[2]]
    before = _inventory(narrow_source)
    prepared, first = spatial_preparation.ensure_coverage(
        locations, narrow_source, cache_directory=cache
    )
    assert first["downloaded_bytes"] == 0
    with pytest.raises(UnsupportedCoordinateError, match="native model domain"):
        prepared.forecast(latitude=0.0, longitude=0.0)
    original = json.loads((narrow_source / "manifest.json").read_text())
    for location in (LOCATIONS[0], LOCATIONS[2]):
        _assert_forecast(
            prepared.forecast(latitude=location["lat"], longitude=location["lon"]),
            location,
            original,
        )
    forbidden = Mock(side_effect=AssertionError("A completed region or known failure was rebuilt"))
    monkeypatch.setattr(spatial_preparation, "rebuild_temperature_guidance", forbidden)
    repeated, report = spatial_preparation.ensure_coverage(locations, cache)
    assert report["downloaded_bytes"] == 0
    with pytest.raises(UnsupportedCoordinateError, match="native model domain"):
        repeated.forecast(latitude=0.0, longitude=0.0)
    for location in (LOCATIONS[0], LOCATIONS[2]):
        assert repeated.forecast(latitude=location["lat"], longitude=location["lon"]) == (
            prepared.forecast(latitude=location["lat"], longitude=location["lon"])
        )
    assert _inventory(narrow_source) == before
    forbidden.assert_not_called()
    no_network.assert_not_called()


def test_offline_rebuild_without_new_area_preserves_original_spatial_view(
    narrow_source: Path, tmp_path: Path, no_network: Mock
) -> None:
    before = _inventory(narrow_source)
    original = PreparedPointForecast.from_directory(narrow_source)
    rebuilt = rebuild_temperature_guidance(
        narrow_source,
        tmp_path / "rebuilt",
        configuration=phase2_configuration(),
        clock=FixtureClock(),
    )
    assert rebuilt["prepared_area"] == OLD_AREA.model_dump()
    assert rebuilt["downloaded_bytes"] == 0
    prepared = PreparedPointForecast.from_directory(tmp_path / "rebuilt")
    for model in ("HRRR", "GFS"):
        assert prepared._guidance[model].equals(original._guidance[model])
    for location in (LOCATIONS[0], LOCATIONS[2]):
        old_forecast = original.forecast(latitude=location["lat"], longitude=location["lon"])
        new_forecast = prepared.forecast(latitude=location["lat"], longitude=location["lon"])
        assert [hour["temperature"] for hour in new_forecast["hours"]] == [
            hour["temperature"] for hour in old_forecast["hours"]
        ]
    assert _inventory(narrow_source) == before
    no_network.assert_not_called()


def test_coordinate_cli_rebuilds_resolved_raw_source_without_netcdf(
    narrow_source, tmp_path, no_network, capsys
):
    from mesoforge.application.prepared_temperature import main

    cache = tmp_path / "index"
    spatial_preparation.ensure_coverage(LOCATIONS, narrow_source, cache_directory=cache)
    for model in ("HRRR", "GFS"):
        (narrow_source / f"{model}.nc").unlink()
    destination = tmp_path / "rebuilt"
    main(
        [
            "--from-raw",
            str(cache),
            "--output-dir",
            str(destination),
            "--lat",
            "44.98",
            "--lon",
            "-93.27",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert report["downloaded_bytes"] == 0
    forecast = spatial_preparation.load_prepared(destination).forecast(
        latitude=44.98, longitude=-93.27
    )
    assert len(forecast["hours"]) == 36
    for hour in forecast["hours"]:
        assert hour["temperature"]["value"] == pytest.approx(282 + hour["horizon_hours"])
    no_network.assert_not_called()
