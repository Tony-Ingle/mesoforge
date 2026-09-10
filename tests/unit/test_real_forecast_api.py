"""Offline HTTP checks through GRIB decoding, preparation, extraction, and blending."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import numpy as np
import pyproj
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from mesoforge import api
from mesoforge.application.point_forecast import prepare_demo_files
from mesoforge.application.prepared_temperature import prepare_temperature_guidance
from mesoforge.guidance.sources.hrrr_transport import RequestsHrrrHttpTransport
from tests.fixtures import gfs_grib, hrrr_grib
from tests.unit.application.test_prepared_temperature import (
    GFS_CYCLE,
    TARGET,
    FixtureClock,
    FixtureSleeper,
    FixtureTransport,
    phase2_configuration,
    prepare_fixture_guidance,
)

NOTICE = "Real HRRR/GFS guidance from fixed prepared inputs; not a current live forecast."


@pytest.fixture()
def prepared_dir(tmp_path: Path) -> Path:
    # These offline tests use generated GRIB fixtures. Provider-backed evidence is
    # demonstrated separately; a real-preparation label is not proof of origin alone.
    prepare_fixture_guidance(tmp_path)
    return tmp_path


def _manifest(directory: Path) -> dict[str, Any]:
    return json.loads((directory / "manifest.json").read_text())


def _update_prepared_hash(directory: Path, model: str) -> None:
    # Used only when deliberately constructing a new fixture snapshot (missing
    # lead or invalid metadata). Corruption tests never update the checksum.
    manifest = _manifest(directory)
    manifest["prepared_files"][model]["sha256"] = hashlib.sha256(
        (directory / f"{model}.nc").read_bytes()
    ).hexdigest()
    (directory / "manifest.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize("latitude, longitude", [(45.8, -93.1), (45.625, -93.375)])
def test_grib_inputs_produce_independent_blend_with_source_times_and_evidence(
    prepared_dir: Path, latitude: float, longitude: float
) -> None:
    with TestClient(api.create_app(prepared_dir)) as client:
        response = client.get("/forecast", params={"lat": latitude, "lon": longitude})
    assert response.status_code == 200
    payload = response.json()
    assert payload["data_kind"] == "real_prepared_guidance"
    assert payload["notice"] == NOTICE
    assert payload["latitude"] == latitude
    assert payload["longitude"] == longitude
    assert payload["target_reference_time"] == "2026-08-30T12:00:00Z"
    assert (
        payload["manifest_sha256"]
        == hashlib.sha256((prepared_dir / "manifest.json").read_bytes()).hexdigest()
    )
    # Independent arithmetic: .7*280 + .3*290 = 283 K. Both contributors
    # increase by exactly 1 K/hour. Wrong HRRR projection would yield no coverage.
    assert [hour["temperature"]["value"] for hour in payload["hours"]] == pytest.approx(
        [283.0, 284.0, 285.0], abs=1e-6
    )
    manifest = _manifest(prepared_dir)
    for horizon, hour in enumerate(payload["hours"], start=1):
        assert hour["horizon_hours"] == horizon
        assert hour["valid_time"] == f"2026-08-30T{12 + horizon}:00:00Z"
        assert hour["temperature"]["unit"] == "K"
        assert hour["missing_reasons"] == []
        assert [source["model"] for source in hour["sources"]] == ["HRRR", "GFS"]
        assert [source["weight"] for source in hour["sources"]] == [0.7, 0.3]
        for source in hour["sources"]:
            age = 0 if source["model"] == "HRRR" else 6
            assert source["cycle"] == f"2026-08-30T{12 - age:02}:00:00Z"
            assert source["source_lead_hours"] == horizon + age
            evidence = next(
                entry
                for entry in manifest["inputs"]
                if entry["model"] == source["model"] and entry["source_lead_hours"] == horizon + age
            )
            assert source["raw_sha256"] == evidence["raw_sha256"]
            assert source["source_url"] == evidence["source_grib_url"]
            assert (
                source["prepared_sha256"] == manifest["prepared_files"][source["model"]]["sha256"]
            )


@pytest.mark.parametrize("missing_model", ["HRRR", "GFS"])
def test_missing_real_model_returns_three_explicit_nulls_without_changing_weights(
    prepared_dir: Path, missing_model: str
) -> None:
    (prepared_dir / f"{missing_model}.nc").unlink()
    with TestClient(api.create_app(prepared_dir)) as client:
        response = client.get("/forecast", params={"lat": 45.8, "lon": -93.1})
    assert response.status_code == 200
    assert response.json()["data_kind"] == "real_prepared_guidance"
    assert len(response.json()["hours"]) == 3
    for hour in response.json()["hours"]:
        assert hour["temperature"] == {"value": None, "unit": "K"}
        assert hour["missing_reasons"] == [f"{missing_model}: prepared guidance file is missing"]
        assert [source["weight"] for source in hour["sources"]] == [0.7, 0.3]


def test_native_grid_gradients_interpolate_at_the_requested_geographic_point(
    tmp_path: Path,
) -> None:
    # Construct the fixture's Lambert projection independently from its documented
    # GRIB constants. Linear native-grid fields have exact bilinear expectations.
    native = pyproj.CRS.from_proj4(
        "+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5 +R=6371229 +units=m +no_defs"
    )
    project = pyproj.Transformer.from_crs("EPSG:4326", native, always_xy=True)
    origin_x, origin_y = project.transform(-94.3, 44.9)
    offset_x, offset_y = np.meshgrid(
        np.arange(hrrr_grib.NX) * 3000.0, np.arange(hrrr_grib.NY) * 3000.0
    )
    hrrr_field = 270 + 0.0001 * offset_x + 0.0002 * offset_y
    longitude, latitude = np.meshgrid(
        -95.5 + np.arange(gfs_grib.NX) * 0.25, 47.25 - np.arange(gfs_grib.NY) * 0.25
    )
    gfs_field = 280 + 2 * (latitude - 45.5) + 4 * (longitude + 93.5)
    transport = FixtureTransport()
    for hour in (1, 2, 3):
        transport.payloads["HRRR", hour] = hrrr_grib.make_temperature_message(
            forecast_hour=hour,
            values_k=hrrr_field + hour - 1,
            cycle_date="20260830",
            cycle_hour=12,
        )
        transport.payloads["GFS", hour] = gfs_grib.make_instantaneous_message(
            canonical_variable_id="air_temperature_2m",
            forecast_hour=hour + 6,
            values=gfs_field + hour - 1,
            cycle_date="20260830",
            cycle_hour=6,
        )
    prepare_temperature_guidance(
        tmp_path,
        configuration=phase2_configuration(),
        target_reference_time=TARGET,
        hrrr_cycle=TARGET,
        gfs_cycle=GFS_CYCLE,
        transport=transport,
        clock=FixtureClock(),
        sleeper=FixtureSleeper(),
    )
    with TestClient(api.create_app(tmp_path)) as client:
        for lat, lon in ((45.8, -93.1), (45.625, -93.375)):
            point_x, point_y = project.transform(lon, lat)
            hrrr_expected = 270 + 0.0001 * (point_x - origin_x) + 0.0002 * (point_y - origin_y)
            gfs_expected = 280 + 2 * (lat - 45.5) + 4 * (lon + 93.5)
            expected = 0.7 * hrrr_expected + 0.3 * gfs_expected
            response = client.get("/forecast", params={"lat": lat, "lon": lon})
            assert response.status_code == 200
            assert [hour["temperature"]["value"] for hour in response.json()["hours"]] == (
                pytest.approx([expected, expected + 1, expected + 2], abs=1e-5)
            )


@pytest.mark.parametrize("missing_model", ["HRRR", "GFS"])
def test_missing_lead_nulls_only_matching_valid_hour(
    prepared_dir: Path, missing_model: str
) -> None:
    path = prepared_dir / f"{missing_model}.nc"
    with xr.open_dataset(path, engine="h5netcdf") as opened:
        dataset = opened.load()
    dataset.isel(source_lead_time=[0, 2]).to_netcdf(path, engine="h5netcdf")
    _update_prepared_hash(prepared_dir, missing_model)
    with TestClient(api.create_app(prepared_dir)) as client:
        response = client.get("/forecast", params={"lat": 45.8, "lon": -93.1})
    assert response.status_code == 200
    first, missing, last = response.json()["hours"]
    assert first["temperature"]["value"] == pytest.approx(283.0, abs=1e-6)
    assert last["temperature"]["value"] == pytest.approx(285.0, abs=1e-6)
    assert first["missing_reasons"] == last["missing_reasons"] == []
    assert missing["valid_time"] == "2026-08-30T14:00:00Z"
    assert missing["temperature"] == {"value": None, "unit": "K"}
    assert missing["missing_reasons"] == [f"{missing_model}: no guidance for this valid time"]
    assert [source["weight"] for source in missing["sources"]] == [0.7, 0.3]


@pytest.mark.parametrize(
    "path,params,status",
    [
        ("/forecast", {"lat": 45.499, "lon": -93.1}, 422),
        ("/forecast", {"lat": 45.8, "lon": -92.999}, 422),
        ("/forecast", {"lat": "nan", "lon": -93.1}, 422),
        ("/forecast", {}, 422),
        ("/not-a-route", {}, 404),
    ],
)
def test_error_responses_identify_loaded_real_guidance(
    prepared_dir: Path, path: str, params: dict[str, Any], status: int
) -> None:
    with TestClient(api.create_app(prepared_dir)) as client:
        response = client.get(path, params=params)
    assert response.status_code == status
    assert response.json()["data_kind"] == "real_prepared_guidance"
    assert response.json()["notice"] == NOTICE


def test_repeated_requests_at_two_coordinates_read_loaded_guidance_only(
    prepared_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = {
        path.relative_to(prepared_dir): path.read_bytes()
        for path in prepared_dir.rglob("*")
        if path.is_file()
    }
    app = api.create_app(prepared_dir)
    forbidden = Mock(side_effect=AssertionError("Request attempted input I/O"))
    monkeypatch.setattr(xr, "open_dataset", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(RequestsHrrrHttpTransport, "get", forbidden)
    monkeypatch.setattr(RequestsHrrrHttpTransport, "head", forbidden)
    with TestClient(app) as client:
        first = client.get("/forecast", params={"lat": 45.8, "lon": -93.1})
        repeated = client.get("/forecast", params={"lat": 45.8, "lon": -93.1})
        other = client.get("/forecast", params={"lat": 45.625, "lon": -93.375})
    assert first.status_code == repeated.status_code == other.status_code == 200
    assert first.content == repeated.content
    assert other.json()["latitude"] == 45.625
    assert [hour["temperature"]["value"] for hour in other.json()["hours"]] == pytest.approx(
        [283, 284, 285], abs=1e-6
    )
    forbidden.assert_not_called()
    monkeypatch.undo()
    assert {
        path.relative_to(prepared_dir): path.read_bytes()
        for path in prepared_dir.rglob("*")
        if path.is_file()
    } == before


@pytest.mark.parametrize("evidence", ["prepared", "raw", "index"])
def test_modified_input_is_rejected_at_startup(prepared_dir: Path, evidence: str) -> None:
    manifest = _manifest(prepared_dir)
    path = prepared_dir / (
        "HRRR.nc" if evidence == "prepared" else manifest["inputs"][0][f"{evidence}_file"]
    )
    path.write_bytes(path.read_bytes() + b"corrupted")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        api.create_app(prepared_dir)


def test_real_inputs_without_manifest_cannot_start(prepared_dir: Path) -> None:
    (prepared_dir / "manifest.json").unlink()
    with pytest.raises(ValueError, match="manifest"):
        api.create_app(prepared_dir)


def test_real_and_synthetic_models_cannot_be_mixed(prepared_dir: Path, tmp_path: Path) -> None:
    synthetic = tmp_path / "synthetic"
    prepare_demo_files(synthetic)
    (prepared_dir / "GFS.nc").write_bytes((synthetic / "GFS.nc").read_bytes())
    _update_prepared_hash(prepared_dir, "GFS")
    with pytest.raises(ValueError, match="mix real and synthetic"):
        api.create_app(prepared_dir)


def test_real_hrrr_without_projection_cannot_start(prepared_dir: Path) -> None:
    path = prepared_dir / "HRRR.nc"
    with xr.open_dataset(path, engine="h5netcdf") as opened:
        dataset = opened.load()
    del dataset.attrs["crs_wkt2"]
    dataset.to_netcdf(path, engine="h5netcdf")
    _update_prepared_hash(prepared_dir, "HRRR")
    with pytest.raises(ValueError, match="crs_wkt2"):
        api.create_app(prepared_dir)


def test_manifest_cycle_must_match_prepared_source_cycle(prepared_dir: Path) -> None:
    manifest = _manifest(prepared_dir)
    manifest["inputs"][0]["cycle"] = "2026-08-30T00:00:00Z"
    (prepared_dir / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        api.create_app(prepared_dir)


def test_explicit_empty_data_directory_never_fabricates_synthetic_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock()
    monkeypatch.setattr("uvicorn.run", run)
    with pytest.raises(ValueError, match="HRRR.nc and GFS.nc are missing"):
        api.main(["--data-dir", str(tmp_path)])
    run.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_default_cli_still_prepares_synthetic_files_and_binds_only_to_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock()
    monkeypatch.setattr("uvicorn.run", run)
    monkeypatch.setattr(api.tempfile, "gettempdir", lambda: str(tmp_path))
    api.main([])
    run.assert_called_once()
    assert run.call_args.kwargs["host"] == "127.0.0.1"
    directory = tmp_path / "mesoforge-synthetic-temperature-demo"
    assert (directory / "HRRR.nc").is_file()
    assert (directory / "GFS.nc").is_file()
