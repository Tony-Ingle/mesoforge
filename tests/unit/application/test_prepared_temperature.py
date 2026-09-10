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
from unittest.mock import MagicMock

import numpy as np
import pyproj
import pytest
import xarray as xr

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
    """Serve six fixture products, each with an unrequested trailing message."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.payloads = {
            (model, horizon): temperature_payload(model, horizon)
            for model in ("HRRR", "GFS")
            for horizon in (1, 2, 3)
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


class FixtureSleeper:
    def sleep(self, seconds: float) -> None:
        raise AssertionError(f"Unexpected retry: {seconds}")


def prepare_fixture_guidance(directory: Path) -> tuple[dict[str, Any], FixtureTransport]:
    transport = FixtureTransport()
    manifest = prepare_temperature_guidance(
        directory,
        configuration=phase2_configuration(),
        target_reference_time=TARGET,
        hrrr_cycle=TARGET,
        gfs_cycle=GFS_CYCLE,
        transport=transport,
        clock=FixtureClock(),
        sleeper=FixtureSleeper(),
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
    transport.downloaded_bytes = 64 * 1024 * 1024 - 10
    with pytest.raises(ValueError, match="body limit"):
        transport.get("https://provider.example/fixture.idx")
    response.raw.read.assert_called_once_with(10, decode_content=True)
    assert transport.downloaded_bytes == 64 * 1024 * 1024


def test_bounded_transport_retains_complete_selected_response_and_counts_body_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    response = session.get.return_value.__enter__.return_value
    response.status_code = 206
    response.headers = {"Content-Length": "4", "Content-Range": "bytes 0-3/8"}
    response.raw.read.side_effect = [b"GRIB", b""]
    monkeypatch.setattr("requests.Session", lambda: session)
    transport = BoundedHttpTransport()
    result = transport.get("https://provider.example/fixture", headers={"Range": "bytes=0-3"})
    assert result.content == b"GRIB"
    assert result.status_code == 206
    assert transport.downloaded_bytes == 4
    session.get.return_value.__exit__.assert_called_once()
