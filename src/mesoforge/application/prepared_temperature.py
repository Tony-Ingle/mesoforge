"""Prepare a fixed, bounded HRRR/GFS temperature slice before serving HTTP requests.

The acquired inventory and complete temperature messages are retained alongside the
small native-grid NetCDF views. This is a manual demonstration, not a cycle selector
or an operational availability/cutoff policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np
import pyproj
import xarray as xr

from mesoforge.catalog.configuration import Phase2Configuration, load_configuration_source
from mesoforge.catalog.domains import BoundingBox
from mesoforge.catalog.sources import GfsSourceSettings, HrrrPhase2SourceSettings
from mesoforge.guidance.acquisition_v2 import (
    Phase2LeadAcquisition,
    acquire_gfs_lead,
    acquire_hrrr_phase2_lead,
)
from mesoforge.guidance.interfaces import Clock, HttpResponse, HttpTransport, Sleeper
from mesoforge.guidance.normalization import (
    build_lambert_conformal_crs,
    compute_bbox_halo_subset_indices,
    compute_latlon_grid,
    compute_projected_coordinates,
)
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.guidance.sources.gfs_decoding import decode_instantaneous_message
from mesoforge.guidance.sources.hrrr_phase2_decoding import decode_selected_message
from mesoforge.guidance.sources.hrrr_transport import (
    RequestsHrrrHttpTransport,
    RequestsHttpResponse,
)

_VARIABLE = "air_temperature_2m"
_DATA_KIND = "real_prepared_guidance"
_AREA = BoundingBox(south=45.5, north=46.0, west=-93.5, east=-93.0)
_EXTRA_READ_KEYS = (
    "iScansNegatively",
    "jPointsAreConsecutive",
    "alternativeRowScanning",
    "shapeOfTheEarth",
    "radius",
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _hour(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Source cycles and target reference must be explicit UTC timestamps")
    if value.minute or value.second or value.microsecond:
        raise ValueError("Source cycles and target reference must identify exact hours")
    return value.astimezone(UTC)


def _leads(target: datetime, cycle: datetime) -> tuple[int, ...]:
    age = int((_hour(target) - _hour(cycle)).total_seconds() / 3600)
    if age < 0 or age + 3 > 48 or cycle.hour not in (0, 6, 12, 18):
        raise ValueError("Source cycles must be 00/06/12/18Z, at or before target, with leads <=48")
    return tuple(age + hour for hour in (1, 2, 3))


def _native_grid(
    model: str, field: xr.DataArray
) -> tuple[pyproj.CRS, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    attrs = field.attrs
    if any(attrs[f"GRIB_{key}"] != 0 for key in _EXTRA_READ_KEYS[:3]):
        raise ValueError(f"{model}: unsupported native scanning arrangement")
    if attrs["GRIB_shapeOfTheEarth"] != 6 or attrs["GRIB_radius"] != 6371229:
        raise ValueError(f"{model}: expected the NCEP 6371229 m spherical earth")
    if model == "HRRR":
        crs = build_lambert_conformal_crs(
            lov_degrees=float(attrs["GRIB_LoVInDegrees"]),
            lad_degrees=float(attrs["GRIB_LaDInDegrees"]),
            latin1_degrees=float(attrs["GRIB_Latin1InDegrees"]),
            latin2_degrees=float(attrs["GRIB_Latin2InDegrees"]),
        )
        x, y = compute_projected_coordinates(
            crs,
            first_lat_degrees=float(attrs["GRIB_latitudeOfFirstGridPointInDegrees"]),
            first_lon_degrees=float(attrs["GRIB_longitudeOfFirstGridPointInDegrees"]),
            dx_m=float(attrs["GRIB_DxInMetres"]),
            dy_m=float(attrs["GRIB_DyInMetres"]),
            nx=int(attrs["GRIB_Nx"]),
            ny=int(attrs["GRIB_Ny"]),
        )
        lat, lon = compute_latlon_grid(crs, x=x, y=y)
        decoded_lon = (field["longitude"].values + 180.0) % 360.0 - 180.0
        if not np.allclose(lat, field["latitude"].values, rtol=0, atol=1e-5) or not np.allclose(
            lon, decoded_lon, rtol=0, atol=1e-5
        ):
            raise ValueError("HRRR: decoded cells disagree with the native projected grid")
        order = np.arange(len(x))
    else:
        # cfgrib supplies the message's native coordinate order. In particular,
        # GFS latitude decreases north-to-south; keep that order and its values.
        if field.dims != ("latitude", "longitude"):
            raise ValueError("GFS: temperature must use the decoded latitude/longitude axes")
        y = np.asarray(field["latitude"].values, dtype=np.float64)
        source_x = np.asarray(field["longitude"].values, dtype=np.float64)
        if (
            len(y) != int(attrs["GRIB_Nj"])
            or len(source_x) != int(attrs["GRIB_Ni"])
            or not np.allclose(
                np.abs(np.diff(y)), float(attrs["GRIB_jDirectionIncrementInDegrees"])
            )
            or not np.allclose(np.diff(source_x), float(attrs["GRIB_iDirectionIncrementInDegrees"]))
        ):
            raise ValueError("GFS: decoded coordinates disagree with the native grid increments")
        normalized_x = (source_x + 180.0) % 360.0 - 180.0
        order = np.argsort(normalized_x)
        x = normalized_x[order]
        lon, lat = np.meshgrid(x, y)
        crs = pyproj.CRS.from_epsg(4326)
    return crs, x, y, lat, lon, order


def normalize_temperature_messages(
    *,
    model: str,
    settings: HrrrPhase2SourceSettings | GfsSourceSettings,
    target_reference_time: datetime,
    source_cycle: datetime,
    payloads_by_lead: dict[int, bytes],
) -> xr.Dataset:
    """Decode temperature only and retain the supported area plus one native cell.

    The six-field Phase 2 normalizers are deliberately not called: no missing
    wind/precipitation fields are invented to satisfy their different contract.
    """
    leads = _leads(target_reference_time, source_cycle)
    if set(payloads_by_lead) != set(leads):
        raise ValueError(f"{model}: expected exactly the three valid-time-aligned leads {leads}")
    contract = next(c for c in settings.field_contracts if c.canonical_variable_id == _VARIABLE)
    settings = settings.model_copy(
        update={"read_keys": tuple(dict.fromkeys((*settings.read_keys, *_EXTRA_READ_KEYS)))}
    )
    frames: list[xr.DataArray] = []
    crs: pyproj.CRS | None = None
    for lead in leads:
        if model == "HRRR" and isinstance(settings, HrrrPhase2SourceSettings):
            field = decode_selected_message(
                payloads_by_lead[lead],
                contract=contract,
                settings=settings,
                forecast_hour=lead,
                cycle_date=source_cycle.date(),
                cycle_hour=source_cycle.hour,
            )
        elif model == "GFS" and isinstance(settings, GfsSourceSettings):
            field = decode_instantaneous_message(
                payloads_by_lead[lead],
                contract=contract,
                settings=settings,
                forecast_hour=lead,
                cycle_date=source_cycle.date(),
                cycle_hour=source_cycle.hour,
            )
        else:
            raise ValueError("Model and source settings must match HRRR or GFS")
        lead_crs, x, y, lat, lon, order = _native_grid(model, field)
        subset = compute_bbox_halo_subset_indices(x=x, y=y, lat=lat, lon=lon, bbox=_AREA)
        ys, xs = slice(subset.y_start, subset.y_end), slice(subset.x_start, subset.x_end)
        frame = xr.DataArray(
            np.asarray(field.values[:, order][ys, xs], dtype=np.float64),
            dims=("y", "x"),
            coords={"x": x[xs], "y": y[ys]},
        )
        if frames and (
            lead_crs != crs
            or not frame["x"].equals(frames[0]["x"])
            or not frame["y"].equals(frames[0]["y"])
        ):
            raise ValueError(f"{model}: source grid changes between leads")
        crs = lead_crs
        frames.append(frame)
    assert crs is not None
    cycle = np.datetime64(source_cycle.replace(tzinfo=None), "ns")
    durations = np.array(leads, dtype="timedelta64[h]").astype("timedelta64[ns]")
    return xr.Dataset(
        data_vars={
            _VARIABLE: (
                ("source_lead_time", "y", "x"),
                np.stack([f.values for f in frames]),
                {"unit_id": "K", "units": "K"},
            )
        },
        coords={
            "x": frames[0]["x"],
            "y": frames[0]["y"],
            "forecast_reference_time": cycle,
            "source_lead_time": durations,
            "source_valid_time": ("source_lead_time", cycle + durations),
        },
        attrs={
            "model": model,
            "data_kind": _DATA_KIND,
            "target_reference_time": _iso(target_reference_time),
            "crs_wkt2": crs.to_wkt(),
            "grid_description": "Native model grid subset, supported area plus one-cell halo",
        },
    )


def _write_bytes(path: Path, payload: bytes) -> str:
    with path.open("xb") as handle:
        handle.write(payload)
    return hashlib.sha256(payload).hexdigest()


def _code_identity() -> dict[str, Any]:
    package = Path(__file__).resolve().parents[1]
    paths = (
        "application/prepared_temperature.py",
        "application/point_forecast.py",
        "api.py",
        "guidance/acquisition_v2.py",
        "guidance/http_fetch.py",
        "guidance/normalization.py",
        "guidance/sources/hrrr_phase2_decoding.py",
        "guidance/sources/gfs_decoding.py",
        "alignment/station_frame.py",
        "alignment/spatial.py",
        "forecasting/scalar_blend.py",
    )
    identity: dict[str, Any] = {
        "source_sha256": {
            path: hashlib.sha256((package / path).read_bytes()).hexdigest() for path in paths
        },
        "dependency_versions": {
            name: version(name)
            for name in ("numpy", "xarray", "cfgrib", "eccodes", "pyproj", "h5netcdf")
        },
    }
    lockfile = package.parents[1] / "uv.lock"
    if lockfile.is_file():
        identity["lockfile_sha256"] = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    return identity


def _retain_input(directory: Path, acquired: Phase2LeadAcquisition) -> dict[str, Any]:
    model = acquired.model.upper()
    if len(acquired.selected_messages) != 1:
        raise ValueError("Temperature preparation must acquire exactly one message per lead")
    message = acquired.selected_messages[0]
    raw_file = f"raw/{model}-f{acquired.forecast_hour:03d}.grib2"
    index_file = f"raw/{model}-f{acquired.forecast_hour:03d}.idx"
    raw_hash = _write_bytes(directory / raw_file, message.payload)
    index_hash = _write_bytes(directory / index_file, acquired.index_payload)
    cycle = datetime.combine(acquired.cycle_date, datetime.min.time(), tzinfo=UTC).replace(
        hour=acquired.cycle_hour
    )
    entry = {
        "model": model,
        "cycle": _iso(cycle),
        "source_lead_hours": acquired.forecast_hour,
        "valid_time": _iso(cycle + timedelta(hours=acquired.forecast_hour)),
        "raw_file": raw_file,
        "raw_sha256": raw_hash,
        "raw_bytes": len(message.payload),
        "index_file": index_file,
        "index_sha256": index_hash,
        "index_bytes": len(acquired.index_payload),
        "source_grib_url": acquired.resolved_grib_url,
        "source_index_url": acquired.resolved_index_url,
        "byte_start": message.byte_start,
        "byte_end": message.byte_end,
        "endpoint": acquired.endpoint,
        "grib_retrieved_at": _iso(acquired.grib_completed_at),
        "index_retrieved_at": _iso(acquired.index_completed_at),
        "grib_available_at": _iso(acquired.grib_available_at),
        "index_available_at": _iso(acquired.index_available_at),
        "grib_last_modified": acquired.full_object_last_modified,
        "index_last_modified": acquired.index_last_modified,
        "etag": acquired.full_object_etag,
    }
    _write_bytes(
        directory / raw_file.replace(".grib2", ".json"), json.dumps(entry, indent=2).encode()
    )
    return entry


def prepare_temperature_guidance(
    directory: Path,
    *,
    configuration: Phase2Configuration,
    target_reference_time: datetime,
    hrrr_cycle: datetime,
    gfs_cycle: datetime,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
) -> dict[str, Any]:
    """Acquire exactly six temperature messages; retain evidence and prepare two files."""
    hrrr_leads = _leads(target_reference_time, hrrr_cycle)
    gfs_leads = _leads(target_reference_time, gfs_cycle)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError(
            "Preparation requires an empty output directory; existing files are retained"
        )
    (directory / "raw").mkdir()
    inputs: list[dict[str, Any]] = []
    prepared_files: dict[str, dict[str, str]] = {}
    for model, cycle, leads in (("HRRR", hrrr_cycle, hrrr_leads), ("GFS", gfs_cycle, gfs_leads)):
        payloads: dict[int, bytes] = {}
        for lead in leads:
            kwargs = {
                "transport": transport,
                "clock": clock,
                "sleeper": sleeper,
                "cycle_date": cycle.date(),
                "cycle_hour": cycle.hour,
                "forecast_hour": lead,
                "cycle_deadline": clock.now(),
                "canonical_variables": (_VARIABLE,),
            }
            if model == "HRRR":
                acquired = acquire_hrrr_phase2_lead(configuration.hrrr, **kwargs)  # type: ignore[arg-type]
            else:
                acquired = acquire_gfs_lead(configuration.gfs, **kwargs)  # type: ignore[arg-type]
            inputs.append(_retain_input(directory, acquired))
            payloads[lead] = acquired.selected_messages[0].payload
        dataset = normalize_temperature_messages(
            model=model,
            settings=configuration.hrrr if model == "HRRR" else configuration.gfs,
            target_reference_time=target_reference_time,
            source_cycle=cycle,
            payloads_by_lead=payloads,
        )
        path = directory / f"{model}.nc"
        with path.open("x+b") as output:
            dataset.to_netcdf(output, engine="h5netcdf", format="NETCDF4")
        prepared_files[model] = {
            "file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    manifest = {
        "data_kind": _DATA_KIND,
        "target_reference_time": _iso(target_reference_time),
        "created_at": _iso(clock.now()),
        "inputs": inputs,
        "prepared_files": prepared_files,
        "downloaded_bytes": getattr(
            transport,
            "downloaded_bytes",
            sum(row["raw_bytes"] + row["index_bytes"] for row in inputs),
        ),
        "configuration_sha256": hashlib.sha256(
            configuration.model_dump_json().encode()
        ).hexdigest(),
        "preparation_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "code_identity": _code_identity(),
        "availability_note": (
            "Provider Last-Modified where available, else retrieval time; "
            "no operational cutoff applied."
        ),
    }
    _write_bytes(directory / "manifest.json", json.dumps(manifest, indent=2).encode())
    return manifest


class BoundedHttpTransport(RequestsHrrrHttpTransport):
    """Streaming adapter with a cumulative 64 MiB body budget for this manual slice."""

    def __init__(self) -> None:
        super().__init__()
        self.downloaded_bytes = 0

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> HttpResponse:
        request_headers = dict(headers or {})
        request_headers["Accept-Encoding"] = "identity"
        requested_range = request_headers.get("Range")
        limit = 16 * 1024 * 1024 if requested_range else 1024 * 1024
        if requested_range:
            start, end = (int(v) for v in requested_range.removeprefix("bytes=").split("-"))
            if end < start or end - start + 1 > limit:
                raise ValueError("Selected message exceeds the 16 MiB acquisition limit")
        if self.downloaded_bytes >= 64 * 1024 * 1024:
            raise ValueError("Acquisition exhausted the cumulative 64 MiB body budget")
        with self._session.get(
            url, headers=request_headers, timeout=timeout, stream=True
        ) as response:
            # Do not read a server's full object when it ignores Range, or an error body.
            if response.status_code != (206 if requested_range else 200):
                return RequestsHttpResponse(int(response.status_code), dict(response.headers), b"")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise ValueError("Provider ignored the uncompressed response request")
            declared = response.headers.get("Content-Length")
            expected = None if declared is None else int(declared)
            remaining = min(limit, 64 * 1024 * 1024 - self.downloaded_bytes)
            if expected is not None and (expected < 0 or expected > remaining):
                raise ValueError("Response exceeds the remaining acquisition body budget")
            chunks: list[bytes] = []
            received = 0
            while remaining > 0:
                chunk = response.raw.read(min(65536, remaining), decode_content=True)
                if not chunk:
                    break
                self.downloaded_bytes += len(chunk)
                received += len(chunk)
                remaining -= len(chunk)
                chunks.append(chunk)
            if remaining == 0 and (expected is None or received != expected):
                raise ValueError("Response reached the acquisition body limit before completion")
            return RequestsHttpResponse(
                int(response.status_code), dict(response.headers), b"".join(chunks)
            )

    def close(self) -> None:
        self._session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare six real HRRR/GFS temperature messages")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--target-reference-time", type=datetime.fromisoformat, required=True)
    parser.add_argument("--hrrr-cycle", type=datetime.fromisoformat, required=True)
    parser.add_argument("--gfs-cycle", type=datetime.fromisoformat, required=True)
    args = parser.parse_args()
    configuration, _ = load_configuration_source(
        base_path=Path("configs/base.yaml"),
        additional_overlay_paths=(
            Path("configs/phase1-grasston.yaml"),
            Path("configs/phase2-grasston.yaml"),
        ),
    )
    if configuration.phase2 is None:
        raise ValueError("Phase 2 source settings are required")
    transport = BoundedHttpTransport()
    try:
        manifest = prepare_temperature_guidance(
            args.output_dir,
            configuration=configuration.phase2,
            target_reference_time=args.target_reference_time,
            hrrr_cycle=args.hrrr_cycle,
            gfs_cycle=args.gfs_cycle,
            transport=transport,
            clock=SystemClock(),
            sleeper=SystemSleeper(),
        )
    finally:
        transport.close()
    print(
        json.dumps(
            {"directory": str(args.output_dir), "downloaded_bytes": manifest["downloaded_bytes"]}
        )
    )


if __name__ == "__main__":
    main()
