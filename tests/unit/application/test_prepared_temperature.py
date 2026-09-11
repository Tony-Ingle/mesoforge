"""Offline GRIB decoding and bounded acquisition checks for prepared temperature.

The GRIB bytes below are generated fixtures, not downloaded observations or forecasts.
They exercise the same decoding and preparation path used for provider messages.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application import prepared_temperature
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.application.prepared_temperature import (
    BoundedHttpTransport,
    normalize_temperature_messages,
    prepare_temperature_guidance,
)
from mesoforge.catalog.configuration import Phase2Configuration, load_configuration_source
from mesoforge.guidance.acquisition_v2 import acquire_gfs_lead, acquire_hrrr_phase2_lead
from mesoforge.guidance.sources.gfs_decoding import GfsDecodeError
from mesoforge.guidance.sources.hrrr_phase2_decoding import HrrrPhase2DecodeError
from tests.fixtures import gfs_grib, hrrr_grib

ROOT = Path(__file__).resolve().parents[3]
TARGET = datetime(2026, 8, 30, 12, tzinfo=UTC)
GFS_CYCLE = TARGET - timedelta(hours=6)
VARIABLE = "air_temperature_2m"
EXTENDED_HORIZONS = tuple(range(1, 37))


def phase2_configuration() -> Phase2Configuration:
    configuration, _ = load_configuration_source(
        base_path=ROOT / "configs/base.yaml",
        environment_path=ROOT / "configs/phase1-grasston.yaml",
        additional_overlay_paths=(ROOT / "configs/phase2-grasston.yaml",),
    )
    assert configuration.phase2 is not None
    return configuration.phase2


def temperature_payload(model: str, horizon: int) -> bytes:
    if model == "HRRR":
        return hrrr_grib.make_temperature_message(
            forecast_hour=horizon,
            values_k=np.full((hrrr_grib.NY, hrrr_grib.NX), 279.0 + horizon),
            cycle_date="20260830",
            cycle_hour=12,
        )
    return gfs_grib.make_instantaneous_message(
        canonical_variable_id=VARIABLE,
        forecast_hour=horizon + 6,
        values=np.full((gfs_grib.NY, gfs_grib.NX), 289.0 + horizon),
        cycle_date="20260830",
        cycle_hour=6,
    )


@dataclass
class _Response:
    status_code: int
    headers: Mapping[str, str]
    content: bytes


class FixtureTransport:
    """Serve fixture products, each with an unrequested trailing message."""

    def __init__(self, horizons: tuple[int, ...] = (1, 2, 3)) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.payloads = {
            (model, horizon): temperature_payload(model, horizon)
            for model in ("HRRR", "GFS")
            for horizon in horizons
        }

    def _product(self, url: str) -> tuple[str, int, bytes, bytes, str]:
        model = "HRRR" if "hrrr.t" in url else "GFS"
        match = re.search(r"f(\d{2,3})(?:\.grib2)?(?:\.idx)?$", url)
        assert match is not None, url
        lead = int(match.group(1))
        horizon = lead if model == "HRRR" else lead - 6
        temperature = self.payloads[model, horizon]
        full = temperature + temperature  # A second message must not be acquired.
        cycle_hour = 12 if model == "HRRR" else 6
        index = (
            f"1:0:d=20260830{cycle_hour:02}:TMP:2 m above ground:{lead} hour fcst:\n"
            f"2:{len(temperature)}:d=20260830{cycle_hour:02}:DPT:2 m above ground:"
            f"{lead} hour fcst:\n"
        ).encode()
        modified = f"Sun, 30 Aug 2026 {cycle_hour:02}:30:00 GMT"
        return model, horizon, full, index, modified

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> _Response:
        requested = (headers or {}).get("Range")
        self.calls.append(("get", url, requested))
        _, _, full, index, modified = self._product(url)
        metadata = {"Last-Modified": modified, "ETag": '"fixture-object"'}
        if url.endswith(".idx"):
            assert requested is None
            return _Response(200, metadata, index)
        assert requested is not None, "Only selected message byte ranges may be acquired"
        start, end = (int(part) for part in requested.removeprefix("bytes=").split("-"))
        metadata["Content-Range"] = f"bytes {start}-{end}/{len(full)}"
        return _Response(206, metadata, full[start : end + 1])

    def head(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> _Response:
        self.calls.append(("head", url, None))
        _, _, full, _, modified = self._product(url)
        return _Response(
            200,
            {
                "Content-Length": str(len(full)),
                "Last-Modified": modified,
                "ETag": '"fixture-object"',
            },
            b"",
        )


class FixtureClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 1, 12, tzinfo=UTC)


def surface_payload(model: str, horizon: int, variable: str) -> bytes:
    """Existing tiny native GRIB fixtures with independently specified surface values."""
    if variable == VARIABLE:
        return temperature_payload(model, horizon)
    value = {
        "dew_point_temperature_2m": 270.0 + horizon,
        "eastward_wind_10m": 3.0,
        "northward_wind_10m": 4.0,
        "wind_gust_10m": 8.0,
    }[variable]
    if model == "GFS":
        return gfs_grib.make_instantaneous_message(
            canonical_variable_id=variable,
            forecast_hour=horizon + 6,
            values=np.full((gfs_grib.NY, gfs_grib.NX), value),
            cycle_date="20260830",
            cycle_hour=6,
            grid_relative_wind=False,
        )
    common = dict(forecast_hour=horizon, cycle_date="20260830", cycle_hour=12)
    values = np.full((hrrr_grib.NY, hrrr_grib.NX), value)
    if variable == "dew_point_temperature_2m":
        return hrrr_grib.make_dew_point_message(values_k=values, **common)
    if variable == "wind_gust_10m":
        return hrrr_grib.make_gust_message(values_m_s=values, **common)
    return hrrr_grib.make_wind_message(
        component="u" if variable == "eastward_wind_10m" else "v",
        values_m_s=values,
        grid_relative=False,
        **common,
    )


class SurfaceFixtureTransport(FixtureTransport):
    """Five native messages per lead, acquired once for all requested coordinates."""

    def __init__(self, horizons: tuple[int, ...] = (1, 2, 3)) -> None:
        super().__init__(horizons)
        self.products = {}
        descriptors = {
            VARIABLE: ("TMP", "2 m above ground"),
            "dew_point_temperature_2m": ("DPT", "2 m above ground"),
            "eastward_wind_10m": ("UGRD", "10 m above ground"),
            "northward_wind_10m": ("VGRD", "10 m above ground"),
            "wind_gust_10m": ("GUST", "surface"),
        }
        for model in ("HRRR", "GFS"):
            for horizon in horizons:
                cycle_hour = 12 if model == "HRRR" else 6
                lead = horizon if model == "HRRR" else horizon + 6
                full, lines = b"", []
                for number, (variable, (parameter, level)) in enumerate(descriptors.items(), 1):
                    payload = surface_payload(model, horizon, variable)
                    lines.append(
                        f"{number}:{len(full)}:d=20260830{cycle_hour:02}:"
                        f"{parameter}:{level}:{lead} hour fcst:\n"
                    )
                    full += payload
                self.products[model, horizon] = full, "".join(lines).encode()

    def _product(self, url: str) -> tuple[str, int, bytes, bytes, str]:
        model, horizon, _, _, modified = super()._product(url)
        full, index = self.products[model, horizon]
        return model, horizon, full, index, modified


class FixtureSleeper:
    def sleep(self, seconds: float) -> None:
        raise AssertionError(f"Unexpected retry: {seconds}")


@pytest.mark.parametrize("model", ["HRRR", "GFS"])
def test_surface_normalization_preserves_temperature_units_and_missing_wind_pair(
    model: str,
) -> None:
    horizons = (1, 2, 3)
    cycle = TARGET if model == "HRRR" else GFS_CYCLE
    age = 0 if model == "HRRR" else 6
    kwargs = dict(
        model=model,
        settings=phase2_configuration().hrrr if model == "HRRR" else phase2_configuration().gfs,
        target_reference_time=TARGET,
        source_cycle=cycle,
        payloads_by_lead={hour + age: temperature_payload(model, hour) for hour in horizons},
        target_horizon_hours=horizons,
    )
    baseline = normalize_temperature_messages(**kwargs)
    payloads = {
        variable: {hour + age: surface_payload(model, hour, variable) for hour in horizons}
        for variable in prepared_temperature.SURFACE_UNITS
    }
    del payloads["northward_wind_10m"][age + 2]
    actual = normalize_temperature_messages(**kwargs, field_payloads=payloads)
    xr.testing.assert_identical(actual.air_temperature_2m, baseline.air_temperature_2m)
    np.testing.assert_array_equal(actual.source_valid_time, baseline.source_valid_time)
    for index, hour in enumerate(horizons):
        np.testing.assert_array_equal(actual.dew_point_temperature_2m[index], 270.0 + hour)
        np.testing.assert_array_equal(actual.wind_gust_10m[index], 8.0)
    np.testing.assert_array_equal(actual.eastward_wind_10m[0], 3.0)
    np.testing.assert_array_equal(actual.northward_wind_10m[0], 4.0)
    assert np.isnan(actual.eastward_wind_10m[1]).all()
    assert np.isnan(actual.northward_wind_10m[1]).all()
    assert actual.attrs["wind_reference"] == "earth_relative"
    reasons = json.loads(actual.attrs["field_missing_reasons_json"])
    assert reasons["northward_wind_10m"][str(age + 2)]
    assert reasons["eastward_wind_10m"][str(age + 2)]
    for variable, unit in prepared_temperature.SURFACE_UNITS.items():
        assert actual[variable].attrs["unit_id"] == unit


def test_surface_raw_retention_rebuild_and_tampering(tmp_path: Path) -> None:
    source, rebuilt = tmp_path / "source", tmp_path / "rebuilt"
    configuration = phase2_configuration()
    manifest = prepare_temperature_guidance(
        source,
        configuration=configuration,
        target_reference_time=TARGET,
        hrrr_cycle=TARGET,
        gfs_cycle=GFS_CYCLE,
        transport=SurfaceFixtureTransport(),
        clock=FixtureClock(),
        sleeper=FixtureSleeper(),
        target_horizon_hours=(1, 2, 3),
        surface_fields=True,
    )
    assert manifest["surface_fields"] is True
    assert all(len(row["extra_messages"]) == 4 for row in manifest["inputs"])
    replay = prepared_temperature.rebuild_temperature_guidance(
        source,
        rebuilt,
        configuration=configuration,
        clock=FixtureClock(),
    )
    assert replay["downloaded_bytes"] == 0
    assert replay["inputs"] == manifest["inputs"]
    for model in ("HRRR", "GFS"):
        with xr.open_dataset(source / f"{model}.nc", engine="h5netcdf") as left:
            with xr.open_dataset(rebuilt / f"{model}.nc", engine="h5netcdf") as right:
                xr.testing.assert_identical(left, right)
    message = manifest["inputs"][0]["extra_messages"][0]
    (source / message["raw_file"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="Checksum or byte count mismatch"):
        prepared_temperature.rebuild_temperature_guidance(
            source,
            tmp_path / "must-not-rebuild",
            configuration=configuration,
            clock=FixtureClock(),
        )
    assert not (tmp_path / "must-not-rebuild").exists()


def prepare_fixture_guidance(
    directory: Path, horizons: tuple[int, ...] = (1, 2, 3)
) -> tuple[dict[str, Any], FixtureTransport]:
    transport = FixtureTransport(horizons)
    manifest = prepare_temperature_guidance(
        directory,
        configuration=phase2_configuration(),
        target_reference_time=TARGET,
        hrrr_cycle=TARGET,
        gfs_cycle=GFS_CYCLE,
        transport=transport,
        clock=FixtureClock(),
        sleeper=FixtureSleeper(),
        target_horizon_hours=horizons,
    )
    return manifest, transport


@pytest.mark.parametrize("model", ["HRRR", "GFS"])
def test_normalization_retains_native_projection_kelvin_and_actual_valid_times(model: str) -> None:
    configuration = phase2_configuration()
    age = 0 if model == "HRRR" else 6
    dataset = normalize_temperature_messages(
        model=model,
        settings=configuration.hrrr if model == "HRRR" else configuration.gfs,
        target_reference_time=TARGET,
        source_cycle=TARGET - timedelta(hours=age),
        payloads_by_lead={hour + age: temperature_payload(model, hour) for hour in (1, 2, 3)},
        target_horizon_hours=(1, 2, 3),
    )
    assert dataset.attrs["data_kind"] == "real_prepared_guidance"
    assert dataset.attrs["model"] == model
    assert dataset.attrs["target_reference_time"] == "2026-08-30T12:00:00Z"
    assert dataset[VARIABLE].attrs["unit_id"] == "K"
    assert set(dataset.data_vars) == {VARIABLE}
    assert dataset[VARIABLE].dims == ("source_lead_time", "y", "x")
    crs = pyproj.CRS.from_wkt(dataset.attrs["crs_wkt2"])
    assert crs.is_projected if model == "HRRR" else crs.is_geographic
    np.testing.assert_array_equal(
        dataset["source_lead_time"].values,
        np.array([1 + age, 2 + age, 3 + age], dtype="timedelta64[h]"),
    )
    np.testing.assert_array_equal(
        dataset["source_valid_time"].values,
        np.array(["2026-08-30T13", "2026-08-30T14", "2026-08-30T15"], dtype="datetime64[h]"),
    )
    for index, expected in enumerate((280, 281, 282) if model == "HRRR" else (290, 291, 292)):
        np.testing.assert_allclose(dataset[VARIABLE].values[index], expected, atol=1e-6)


@pytest.mark.parametrize("model", ["HRRR", "GFS"])
def test_normalization_rejects_messages_from_a_different_cycle(model: str) -> None:
    configuration = phase2_configuration()
    age = 0 if model == "HRRR" else 6
    error = HrrrPhase2DecodeError if model == "HRRR" else GfsDecodeError
    with pytest.raises(error, match="cycle reference time mismatch"):
        normalize_temperature_messages(
            model=model,
            settings=configuration.hrrr if model == "HRRR" else configuration.gfs,
            target_reference_time=TARGET,
            source_cycle=TARGET - timedelta(hours=age + 6),
            payloads_by_lead={
                hour + age + 6: temperature_payload(model, hour) for hour in (1, 2, 3)
            },
            target_horizon_hours=(1, 2, 3),
        )


def test_gfs_north_to_south_values_stay_attached_to_their_native_coordinates() -> None:
    latitude = gfs_grib.FIRST_LAT_DEGREES - np.arange(gfs_grib.NY) * gfs_grib.DX_DEGREES
    longitude = gfs_grib.FIRST_LON_DEGREES - 360 + np.arange(gfs_grib.NX) * gfs_grib.DX_DEGREES
    lon, lat = np.meshgrid(longitude, latitude)
    # Integer/half-K fixture values are exactly representable in GRIB. A reversed
    # latitude axis or shifted longitude would fail this spatially varying check.
    field = 280 + 2 * (lat - 45.5) + 4 * (lon + 93.5)
    payloads = {
        hour + 6: gfs_grib.make_instantaneous_message(
            canonical_variable_id=VARIABLE,
            forecast_hour=hour + 6,
            values=field + hour - 1,
            cycle_date="20260830",
            cycle_hour=6,
        )
        for hour in (1, 2, 3)
    }
    dataset = normalize_temperature_messages(
        model="GFS",
        settings=phase2_configuration().gfs,
        target_reference_time=TARGET,
        source_cycle=GFS_CYCLE,
        payloads_by_lead=payloads,
        target_horizon_hours=(1, 2, 3),
    )
    assert np.all(np.diff(dataset["y"].values) < 0)
    prepared_lon, prepared_lat = np.meshgrid(dataset["x"].values, dataset["y"].values)
    for hour in (1, 2, 3):
        expected = 280 + 2 * (prepared_lat - 45.5) + 4 * (prepared_lon + 93.5) + hour - 1
        np.testing.assert_allclose(dataset[VARIABLE].values[hour - 1], expected, atol=1e-6)


def test_preparation_acquires_only_temperature_and_retains_exact_bytes_and_provenance(
    tmp_path: Path,
) -> None:
    manifest, transport = prepare_fixture_guidance(tmp_path)
    assert manifest == json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["data_kind"] == "real_prepared_guidance"
    assert manifest["target_reference_time"] == "2026-08-30T12:00:00Z"
    assert len(manifest["inputs"]) == 6
    assert len(transport.calls) == 18  # One inventory, object HEAD, and selected range per lead.
    assert len([call for call in transport.calls if call[2] is not None]) == 6
    total_bytes = 0
    for entry in manifest["inputs"]:
        model = entry["model"]
        horizon = entry["source_lead_hours"] - (0 if model == "HRRR" else 6)
        raw = (tmp_path / entry["raw_file"]).read_bytes()
        index = (tmp_path / entry["index_file"]).read_bytes()
        assert raw == transport.payloads[model, horizon]
        assert hashlib.sha256(raw).hexdigest() == entry["raw_sha256"]
        assert hashlib.sha256(index).hexdigest() == entry["index_sha256"]
        assert len(raw) == entry["raw_bytes"]
        assert len(index) == entry["index_bytes"]
        assert b":DPT:" in index  # Retain the acquired inventory beyond the current API field.
        assert entry["valid_time"] == f"2026-08-30T{12 + horizon}:00:00Z"
        assert entry["cycle"] == f"2026-08-30T{'12' if model == 'HRRR' else '06'}:00:00Z"
        assert entry["source_grib_url"].startswith("https://")
        assert entry["source_index_url"] == entry["source_grib_url"] + ".idx"
        assert entry["grib_retrieved_at"] == "2026-09-01T12:00:00Z"
        assert entry["grib_available_at"] != entry["grib_retrieved_at"]
        assert entry["byte_start"] == 0
        assert entry["byte_end"] == len(raw)
        total_bytes += len(raw) + len(index)
    assert manifest["downloaded_bytes"] == total_bytes
    for model, entry in manifest["prepared_files"].items():
        path = tmp_path / entry["file"]
        assert path.name == f"{model}.nc"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        with xr.open_dataset(path, engine="h5netcdf") as dataset:
            assert set(dataset.data_vars) == {VARIABLE}


def test_preparation_refuses_to_overwrite_an_existing_snapshot(tmp_path: Path) -> None:
    existing = tmp_path / "operator-owned.txt"
    existing.write_text("Keep this input")
    transport = FixtureTransport()
    with pytest.raises(ValueError):
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
    assert existing.read_text() == "Keep this input"
    assert transport.calls == []


def test_default_preparation_covers_36_valid_hours_and_rebuilds_offline_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    transport = FixtureTransport(EXTENDED_HORIZONS)
    manifest = prepare_temperature_guidance(
        source,
        configuration=phase2_configuration(),
        target_reference_time=TARGET,
        hrrr_cycle=TARGET,
        gfs_cycle=GFS_CYCLE,
        transport=transport,
        clock=FixtureClock(),
        sleeper=FixtureSleeper(),
    )
    assert manifest["target_horizon_hours"] == list(EXTENDED_HORIZONS)
    assert len(manifest["inputs"]) == 72
    assert len(transport.calls) == 216
    assert len([call for call in transport.calls if call[2] is not None]) == 72
    assert manifest["downloaded_bytes"] == sum(
        item["raw_bytes"] + item["index_bytes"] for item in manifest["inputs"]
    )
    valid_times = np.datetime64("2026-08-30T12", "h") + np.arange(1, 37).astype("timedelta64[h]")
    assert str(valid_times[-1]) == "2026-09-01T00"
    originals: dict[str, xr.Dataset] = {}
    for model, age, base in (("HRRR", 0, 279), ("GFS", 6, 289)):
        rows = [item for item in manifest["inputs"] if item["model"] == model]
        assert [item["source_lead_hours"] for item in rows] == list(range(1 + age, 37 + age))
        for horizon, item in zip(EXTENDED_HORIZONS, rows, strict=True):
            assert (source / item["raw_file"]).read_bytes() == transport.payloads[model, horizon]
            assert item["valid_time"] == (TARGET + timedelta(hours=horizon)).isoformat().replace(
                "+00:00", "Z"
            )
        with xr.open_dataset(source / f"{model}.nc", engine="h5netcdf") as opened:
            dataset = opened.load()
        originals[model] = dataset
        assert dataset[VARIABLE].attrs["units"] == "K"
        assert dataset[VARIABLE].sizes["source_lead_time"] == 36
        np.testing.assert_array_equal(dataset["source_valid_time"].values, valid_times)
        np.testing.assert_array_equal(
            dataset["source_lead_time"].values,
            np.arange(1 + age, 37 + age).astype("timedelta64[h]"),
        )
        crs = pyproj.CRS.from_wkt(dataset.attrs["crs_wkt2"])
        assert crs.is_projected if model == "HRRR" else crs.is_geographic
        for horizon in EXTENDED_HORIZONS:
            np.testing.assert_array_equal(dataset[VARIABLE].values[horizon - 1], base + horizon)
    original_forecast = PreparedPointForecast.from_directory(source)
    points = ((45.8, -93.1), (45.625, -93.375))
    before = {
        point: original_forecast.forecast(latitude=point[0], longitude=point[1])["hours"]
        for point in points
    }
    source_bytes = {
        path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()
    }
    forbidden = Mock(side_effect=AssertionError("Offline rebuild attempted a network session"))
    monkeypatch.setattr("requests.Session", forbidden)
    destination = tmp_path / "rebuilt"
    rebuilt = prepared_temperature.rebuild_temperature_guidance(
        source, destination, configuration=phase2_configuration(), clock=FixtureClock()
    )
    assert rebuilt["target_horizon_hours"] == list(EXTENDED_HORIZONS)
    assert rebuilt["downloaded_bytes"] == 0
    assert rebuilt["inputs"] == manifest["inputs"]
    for model, dataset in originals.items():
        with xr.open_dataset(destination / f"{model}.nc", engine="h5netcdf") as opened:
            xr.testing.assert_identical(opened.load(), dataset)
    after_forecast = PreparedPointForecast.from_directory(destination)
    for point in points:
        after = after_forecast.forecast(latitude=point[0], longitude=point[1])["hours"]
        assert [hour["temperature"] for hour in after] == [
            hour["temperature"] for hour in before[point]
        ]
        # .7 * (279 + hour) + .3 * (289 + hour) = 282 + hour.
        assert [hour["temperature"]["value"] for hour in after] == pytest.approx(
            list(range(283, 319)), abs=1e-6
        )
        assert [hour["valid_time"] for hour in after] == [
            str(value) + ":00:00Z" for value in valid_times
        ]
        assert all(hour["missing_reasons"] == [] for hour in after)
    assert {
        path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()
    } == source_bytes
    forbidden.assert_not_called()


@pytest.mark.parametrize("model", ["HRRR", "GFS"])
def test_36_hour_preparation_rejects_cycle_too_old_before_io(tmp_path: Path, model: str) -> None:
    transport = FixtureTransport()
    cycles = {"HRRR": TARGET, "GFS": GFS_CYCLE}
    cycles[model] = TARGET - timedelta(hours=18)
    destination = tmp_path / "output"
    with pytest.raises(ValueError, match="leads <=48"):
        prepare_temperature_guidance(
            destination,
            configuration=phase2_configuration(),
            target_reference_time=TARGET,
            hrrr_cycle=cycles["HRRR"],
            gfs_cycle=cycles["GFS"],
            transport=transport,
            clock=FixtureClock(),
            sleeper=FixtureSleeper(),
        )
    assert transport.calls == []
    assert not destination.exists()


@pytest.mark.parametrize("horizons", [(), (1, 2), tuple(range(1, 36)), tuple(range(1, 38))])
def test_preparation_rejects_unsupported_horizons_before_io(
    tmp_path: Path, horizons: tuple[int, ...]
) -> None:
    transport = FixtureTransport()
    destination = tmp_path / "output"
    with pytest.raises(ValueError):
        prepare_temperature_guidance(
            destination,
            configuration=phase2_configuration(),
            target_reference_time=TARGET,
            hrrr_cycle=TARGET,
            gfs_cycle=GFS_CYCLE,
            transport=transport,
            clock=FixtureClock(),
            sleeper=FixtureSleeper(),
            target_horizon_hours=horizons,
        )
    assert transport.calls == []
    assert not destination.exists()


def test_legacy_snapshot_without_horizon_metadata_rebuilds_and_serves_three_hours(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    manifest, _ = prepare_fixture_guidance(source)
    del manifest["target_horizon_hours"]
    (source / "manifest.json").write_text(json.dumps(manifest))
    original = PreparedPointForecast.from_directory(source).forecast(latitude=45.8, longitude=-93.1)
    destination = tmp_path / "rebuilt"
    prepared_temperature.rebuild_temperature_guidance(
        source, destination, configuration=phase2_configuration(), clock=FixtureClock()
    )
    rebuilt = PreparedPointForecast.from_directory(destination).forecast(
        latitude=45.8, longitude=-93.1
    )
    for forecast in (original, rebuilt):
        assert [hour["horizon_hours"] for hour in forecast["hours"]] == [1, 2, 3]
        assert [hour["temperature"]["value"] for hour in forecast["hours"]] == pytest.approx(
            [283.0, 284.0, 285.0], abs=1e-6
        )
        assert all(hour["missing_reasons"] == [] for hour in forecast["hours"])


@pytest.mark.parametrize("remove_prepared", [False, True])
def test_raw_rebuild_reproduces_values_without_network_or_changing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remove_prepared: bool
) -> None:
    source = tmp_path / "source"
    original, _ = prepare_fixture_guidance(source)
    points = ((45.8, -93.1), (45.625, -93.375))
    forecast = PreparedPointForecast.from_directory(source)
    before_forecasts = {
        point: forecast.forecast(latitude=point[0], longitude=point[1]) for point in points
    }
    if remove_prepared:
        for model in ("HRRR", "GFS"):
            (source / f"{model}.nc").unlink()
    source_bytes = {
        path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()
    }
    network = Mock(side_effect=AssertionError("Raw rebuild attempted a network session"))
    monkeypatch.setattr("requests.Session", network)

    class RebuildClock:
        def now(self) -> datetime:
            return datetime(2026, 9, 2, 12, tzinfo=UTC)

    destination = tmp_path / "rebuilt"
    rebuilt = prepared_temperature.rebuild_temperature_guidance(
        source,
        destination,
        configuration=phase2_configuration(),
        clock=RebuildClock(),
    )
    after_forecast = PreparedPointForecast.from_directory(destination)
    for point in points:
        before = before_forecasts[point]
        after = after_forecast.forecast(latitude=point[0], longitude=point[1])
        # Rebuilding has to reproduce values exactly with the same environment.
        assert [hour["temperature"] for hour in after["hours"]] == [
            hour["temperature"] for hour in before["hours"]
        ]
        # Independently calculated: 70% of 280 K plus 30% of 290 K is
        # 283 K; both fixture models increase by exactly 1 K per hour.
        assert [hour["temperature"]["value"] for hour in after["hours"]] == pytest.approx(
            [283.0, 284.0, 285.0], abs=1e-6
        )
        assert [hour["valid_time"] for hour in after["hours"]] == [
            "2026-08-30T13:00:00Z",
            "2026-08-30T14:00:00Z",
            "2026-08-30T15:00:00Z",
        ]
        assert all(hour["missing_reasons"] == [] for hour in after["hours"])
        for hour in after["hours"]:
            assert hour["temperature"]["unit"] == "K"
            assert [item["weight"] for item in hour["sources"]] == [0.7, 0.3]
    assert rebuilt == json.loads((destination / "manifest.json").read_text())
    assert rebuilt["inputs"] == original["inputs"]
    assert rebuilt["created_at"] == "2026-09-02T12:00:00Z"
    assert rebuilt["downloaded_bytes"] == 0
    assert (
        rebuilt["source_manifest_sha256"]
        == hashlib.sha256(source_bytes[Path("manifest.json")]).hexdigest()
    )
    assert (destination / rebuilt["source_manifest_file"]).read_bytes() == source_bytes[
        Path("manifest.json")
    ]
    assert rebuilt["configuration_sha256"] == original["configuration_sha256"]
    assert (
        rebuilt["preparation_code_sha256"]
        == hashlib.sha256(Path(prepared_temperature.__file__).read_bytes()).hexdigest()
    )
    assert rebuilt["code_identity"] == original["code_identity"]
    for item in original["inputs"]:
        for kind in ("raw", "index"):
            filename = item[f"{kind}_file"]
            assert (destination / filename).read_bytes() == source_bytes[Path(filename)]
    assert {
        path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()
    } == source_bytes
    network.assert_not_called()


@pytest.mark.parametrize("kind", ["raw", "index"])
@pytest.mark.parametrize("damage", ["changed", "missing", "escape"])
def test_raw_rebuild_rejects_unusable_retained_evidence(
    tmp_path: Path, kind: str, damage: str
) -> None:
    source = tmp_path / "source"
    manifest, _ = prepare_fixture_guidance(source)
    item = manifest["inputs"][0]
    path = source / item[f"{kind}_file"]
    if damage == "changed":
        path.write_bytes(path.read_bytes() + b"changed")
    elif damage == "missing":
        path.unlink()
    else:
        # Even correctly checksummed evidence must remain within its snapshot.
        outside = tmp_path / "outside"
        outside.write_bytes(path.read_bytes())
        item[f"{kind}_file"] = "../outside"
        (source / "manifest.json").write_text(json.dumps(manifest))
    destination = tmp_path / "rebuilt"
    with pytest.raises(FileNotFoundError if damage == "missing" else ValueError):
        prepared_temperature.rebuild_temperature_guidance(
            source,
            destination,
            configuration=phase2_configuration(),
            clock=FixtureClock(),
        )
    assert not (destination / "manifest.json").exists()


@pytest.mark.parametrize("damage", ["configuration", "missing_hour", "duplicate_hour", "time"])
def test_raw_rebuild_rejects_incompatible_or_incomplete_manifest(
    tmp_path: Path, damage: str
) -> None:
    source = tmp_path / "source"
    manifest, _ = prepare_fixture_guidance(source)
    if damage == "configuration":
        manifest["configuration_sha256"] = "0" * 64
    elif damage == "missing_hour":
        manifest["inputs"].pop()
    elif damage == "duplicate_hour":
        manifest["inputs"][1] = manifest["inputs"][0]
    else:
        manifest["inputs"][0]["valid_time"] = "2026-08-30T14:00:00Z"
    (source / "manifest.json").write_text(json.dumps(manifest))
    destination = tmp_path / "rebuilt"
    with pytest.raises(ValueError):
        prepared_temperature.rebuild_temperature_guidance(
            source,
            destination,
            configuration=phase2_configuration(),
            clock=FixtureClock(),
        )
    assert not (destination / "manifest.json").exists()


def test_raw_rebuild_preserves_an_occupied_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    prepare_fixture_guidance(source)
    destination = tmp_path / "rebuilt"
    destination.mkdir()
    existing = destination / "operator-owned.txt"
    existing.write_bytes(b"keep")
    with pytest.raises(ValueError):
        prepared_temperature.rebuild_temperature_guidance(
            source,
            destination,
            configuration=phase2_configuration(),
            clock=FixtureClock(),
        )
    assert list(destination.iterdir()) == [existing]
    assert existing.read_bytes() == b"keep"


def test_cli_rebuild_uses_manifest_times_without_constructing_http_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source"
    prepare_fixture_guidance(source)
    for model in ("HRRR", "GFS"):
        (source / f"{model}.nc").unlink()
    network = Mock(side_effect=AssertionError("Raw rebuild created an HTTP transport"))
    monkeypatch.setattr(prepared_temperature, "BoundedHttpTransport", network)
    destination = tmp_path / "rebuilt"
    prepared_temperature.main(["--output-dir", str(destination), "--from-raw", str(source)])
    assert json.loads(capsys.readouterr().out) == {
        "directory": str(destination),
        "downloaded_bytes": 0,
    }
    assert (
        PreparedPointForecast.from_directory(destination).forecast(latitude=45.8, longitude=-93.1)[
            "target_reference_time"
        ]
        == "2026-08-30T12:00:00Z"
    )
    network.assert_not_called()


@pytest.mark.parametrize("cycle_option", ["--target-reference-time", "--hrrr-cycle", "--gfs-cycle"])
def test_cli_rebuild_rejects_reinterpreting_retained_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cycle_option: str
) -> None:
    network = Mock(side_effect=AssertionError("Invalid CLI options created HTTP transport"))
    monkeypatch.setattr(prepared_temperature, "BoundedHttpTransport", network)
    with pytest.raises(SystemExit) as error:
        prepared_temperature.main(
            [
                "--output-dir",
                str(tmp_path / "output"),
                "--from-raw",
                str(tmp_path / "source"),
                cycle_option,
                "2026-08-30T12:00:00Z",
            ]
        )
    assert error.value.code == 2
    network.assert_not_called()


def test_cli_acquisition_requires_coordinates_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    network = Mock(side_effect=AssertionError("Invalid CLI options created HTTP transport"))
    monkeypatch.setattr(prepared_temperature, "BoundedHttpTransport", network)
    with pytest.raises(SystemExit) as error:
        prepared_temperature.main(["--output-dir", str(tmp_path)])
    assert error.value.code == 2
    network.assert_not_called()


@pytest.mark.parametrize("model", ["HRRR", "GFS"])
@pytest.mark.parametrize("variables", [(), ("not_a_supported_field",), (VARIABLE, VARIABLE)])
def test_invalid_acquisition_selection_fails_before_any_provider_io(
    model: str, variables: tuple[str, ...]
) -> None:
    configuration = phase2_configuration()
    transport = FixtureTransport()
    acquire = acquire_hrrr_phase2_lead if model == "HRRR" else acquire_gfs_lead
    with pytest.raises(ValueError, match="canonical_variables"):
        acquire(
            configuration.hrrr if model == "HRRR" else configuration.gfs,
            transport=transport,
            clock=FixtureClock(),
            sleeper=FixtureSleeper(),
            cycle_date=TARGET.date(),
            cycle_hour=12 if model == "HRRR" else 6,
            forecast_hour=1 if model == "HRRR" else 7,
            cycle_deadline=FixtureClock().now() + timedelta(minutes=5),
            canonical_variables=variables,
        )
    assert transport.calls == []


def test_bounded_transport_never_reads_a_full_object_when_range_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    response = session.get.return_value.__enter__.return_value
    response.status_code = 200
    response.headers = {"Content-Length": "999999999"}
    monkeypatch.setattr("requests.Session", lambda: session)
    transport = BoundedHttpTransport()
    result = transport.get("https://provider.example/fixture", headers={"Range": "bytes=0-3"})
    assert result.status_code == 200
    assert result.content == b""
    assert transport.downloaded_bytes == 0
    response.raw.read.assert_not_called()
    assert session.get.call_args.kwargs["stream"] is True
    assert session.get.call_args.kwargs["headers"]["Accept-Encoding"] == "identity"


def test_bounded_transport_rejects_oversized_declared_inventory_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    response = session.get.return_value.__enter__.return_value
    response.status_code = 200
    response.headers = {"Content-Length": str(1024 * 1024 + 1)}
    monkeypatch.setattr("requests.Session", lambda: session)
    transport = BoundedHttpTransport()
    with pytest.raises(ValueError, match="body budget"):
        transport.get("https://provider.example/fixture.idx")
    response.raw.read.assert_not_called()
    assert transport.downloaded_bytes == 0


def test_bounded_transport_stops_at_cumulative_budget_without_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    response = session.get.return_value.__enter__.return_value
    response.status_code = 200
    response.headers = {}
    response.raw.read.side_effect = lambda amount, decode_content: b"x" * amount
    monkeypatch.setattr("requests.Session", lambda: session)
    transport = BoundedHttpTransport()
    transport.downloaded_bytes = 128 * 1024 * 1024 - 10
    with pytest.raises(ValueError, match="body limit"):
        transport.get("https://provider.example/fixture.idx")
    response.raw.read.assert_called_once_with(10, decode_content=True)
    assert transport.downloaded_bytes == 128 * 1024 * 1024


@pytest.mark.parametrize("body_budget", [128 * 1024 * 1024, 4])
def test_bounded_transport_retains_complete_selected_response_and_counts_body_bytes(
    monkeypatch: pytest.MonkeyPatch,
    body_budget: int,
) -> None:
    session = MagicMock()
    response = session.get.return_value.__enter__.return_value
    response.status_code = 206
    response.headers = {"Content-Length": "4", "Content-Range": "bytes 0-3/8"}
    response.raw.read.side_effect = [b"GRIB", b""]
    monkeypatch.setattr("requests.Session", lambda: session)
    transport = BoundedHttpTransport(body_budget=body_budget)
    result = transport.get("https://provider.example/fixture", headers={"Range": "bytes=0-3"})
    assert result.content == b"GRIB"
    assert result.status_code == 206
    assert transport.downloaded_bytes == 4
    session.get.return_value.__exit__.assert_called_once()
    if body_budget == 4:
        with pytest.raises(ValueError, match="exhausted the cumulative body budget"):
            transport.get("https://provider.example/fixture", headers={"Range": "bytes=0-3"})


@pytest.mark.parametrize("outside_first", [False, True])
def test_coordinate_cli_acquires_once_and_reuses_same_cycles_on_repeat(
    tmp_path, monkeypatch, capsys, outside_first
):
    transport = FixtureTransport(EXTENDED_HORIZONS)
    transport.close = Mock()
    factory = Mock(return_value=transport)
    monkeypatch.setattr(prepared_temperature, "BoundedHttpTransport", factory)
    config = tmp_path / "locations.json"
    config.write_text(
        json.dumps(
            {
                "locations": [
                    {"lat": 45.8, "lon": -93.1},
                    {"lat": 44.98, "lon": -93.27},
                    {"lat": 45.9, "lon": -93.0},
                ]
            }
        )
    )
    if outside_first:
        data = json.loads(config.read_text())
        data["locations"].insert(0, {"lat": 0.0, "lon": 0.0})
        config.write_text(json.dumps(data))
    output = tmp_path / "prepared"
    args = [
        "--config",
        str(config),
        "--output-dir",
        str(output),
        "--target-reference-time",
        TARGET.isoformat(),
        "--hrrr-cycle",
        TARGET.isoformat(),
        "--gfs-cycle",
        GFS_CYCLE.isoformat(),
    ]
    prepared_temperature.main(args)
    first = json.loads(capsys.readouterr().out)
    assert first["downloaded_bytes"] > 0
    assert len(first["footprints"]) == 3 + int(outside_first)
    assert len({row["directory"] for row in first["regions"]}) == 1
    calls = list(transport.calls)
    factory.assert_called_once()
    prepared_temperature.main(args)
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["downloaded_bytes"] == 0
    assert repeated["regions"][0]["status"] == "reused"
    assert transport.calls == calls
    factory.assert_called_once()
    from mesoforge.application.spatial_preparation import load_prepared

    loaded = load_prepared(output)
    for point in json.loads(config.read_text())["locations"]:
        if point["lat"] == 0.0:
            from mesoforge.application.spatial_coverage import UnsupportedCoordinateError

            with pytest.raises(UnsupportedCoordinateError, match="native model domain"):
                loaded.forecast(latitude=point["lat"], longitude=point["lon"])
            continue
        forecast = loaded.forecast(latitude=point["lat"], longitude=point["lon"])
        assert len(forecast["hours"]) == 36
        for hour in forecast["hours"]:
            assert hour["temperature"]["value"] == pytest.approx(282 + hour["horizon_hours"])
            assert hour["missing_reasons"] == []
