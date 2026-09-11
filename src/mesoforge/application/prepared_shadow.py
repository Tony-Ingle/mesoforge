"""Normalize decoded Lambert temperature frames for retained shadow guidance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pyproj
import xarray as xr

from mesoforge.application.spatial_coverage import UnsupportedCoordinateError, native_bbox_bounds
from mesoforge.catalog.domains import BoundingBox
from mesoforge.guidance.normalization import (
    build_lambert_conformal_crs,
    compute_latlon_grid,
    compute_projected_coordinates,
)

_VARIABLE = "air_temperature_2m"


def _utc_hour(value: datetime) -> np.datetime64:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Shadow cycle and target must be explicit UTC timestamps")
    if value.minute or value.second or value.microsecond:
        raise ValueError("Shadow cycle and target must identify exact hours")
    return np.datetime64(value.replace(tzinfo=None), "ns")


def _lambert_grid(field: xr.DataArray) -> tuple[pyproj.CRS, np.ndarray, np.ndarray]:
    """Use the decoded projection, then check it against the decoded native cells."""
    attrs = field.attrs
    if field.dims != ("y", "x") or field.dtype.kind != "f":
        raise ValueError("Shadow temperature must have floating native y/x values")
    if attrs.get("GRIB_gridType") != "lambert":
        raise ValueError("Shadow normalization currently requires a Lambert native grid")
    scanning = {
        "iScansNegatively": 0,
        "jScansPositively": 1,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 0,
    }
    if any(attrs.get(f"GRIB_{name}") != value for name, value in scanning.items()):
        raise ValueError("Unsupported shadow native scanning arrangement")
    if attrs.get("GRIB_shapeOfTheEarth") != 6 or attrs.get("GRIB_radius") != 6371229:
        raise ValueError("Shadow normalization requires the NCEP 6371229 m sphere")
    crs = build_lambert_conformal_crs(
        lov_degrees=float(attrs["GRIB_LoVInDegrees"]),
        lad_degrees=float(attrs["GRIB_LaDInDegrees"]),
        latin1_degrees=float(attrs["GRIB_Latin1InDegrees"]),
        latin2_degrees=float(attrs["GRIB_Latin2InDegrees"]),
        earth_radius_m=float(attrs["GRIB_radius"]),
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
    if (
        field.shape != (len(y), len(x))
        or min(len(x), len(y)) < 2
        or not np.all(np.isfinite(x))
        or not np.all(np.isfinite(y))
        or not np.all(np.diff(x) > 0)
        or not np.all(np.diff(y) > 0)
    ):
        raise ValueError("Invalid shadow native grid dimensions or spacing")
    lat, lon = compute_latlon_grid(crs, x=x, y=y)
    decoded_lon = (field["longitude"].values + 180.0) % 360.0 - 180.0
    if not np.allclose(lat, field["latitude"].values, rtol=0, atol=1e-5) or not np.allclose(
        lon, decoded_lon, rtol=0, atol=1e-5
    ):
        raise ValueError("Decoded shadow cells disagree with the native projected grid")
    return crs, x, y


def _axis_slice(axis: np.ndarray, lower: float, upper: float) -> slice:
    lower, upper = max(lower, axis[0]), min(upper, axis[-1])
    if lower > upper:
        raise UnsupportedCoordinateError("Requested footprint does not intersect the shadow domain")
    # Enclose the projected footprint and one additional native cell, clipped at the domain edge.
    first = max(0, int(np.searchsorted(axis, lower, side="right")) - 2)
    last = min(len(axis), int(np.searchsorted(axis, upper, side="left")) + 2)
    return slice(first, last)


def normalize_shadow_temperature(
    decoded: dict[int, xr.DataArray],
    *,
    model: str,
    cycle: datetime,
    target: datetime,
    area: BoundingBox | None = None,
) -> xr.Dataset:
    """Prepare only available valid-time-aligned leads; never fill an absent forecast hour.

    The source adapter must already validate the 2 m instantaneous temperature GRIB
    contract. This boundary checks its units/times/native cells before constructing
    the existing prepared dataset format. Acquisition/provenance remain caller-owned.
    """
    source_time, target_time = _utc_hour(cycle), _utc_hour(target)
    if source_time > target_time:
        raise ValueError("Shadow source cycle must not be after the target reference time")
    if not decoded:
        raise ValueError("No decoded shadow temperature frames are available")
    age = int((target_time - source_time) / np.timedelta64(1, "h"))
    if any(type(lead) is not int or not age + 1 <= lead <= age + 36 for lead in decoded):
        raise ValueError("Shadow leads must align with target hours 1 through 36")
    leads = sorted(decoded)
    frames: list[np.ndarray] = []
    native: tuple[pyproj.CRS, np.ndarray, np.ndarray] | None = None
    xs, ys = slice(None), slice(None)
    for lead in leads:
        field = decoded[lead]
        if str(field.attrs.get("GRIB_units", "")).strip().lower() != "k":
            raise ValueError(f"{model}: decoded shadow temperature must use K")
        expected = {
            "time": source_time,
            "step": np.timedelta64(lead, "h"),
            "valid_time": source_time + np.timedelta64(lead, "h"),
        }
        if any(
            name not in field.coords or field[name].ndim != 0 or field[name].values[()] != value
            for name, value in expected.items()
        ):
            raise ValueError(f"{model}: decoded shadow cycle/lead/valid time mismatch")
        crs, x, y = _lambert_grid(field)
        if native is None:
            native = crs, x, y
            if area is not None:
                xmin, xmax, ymin, ymax = native_bbox_bounds(area, crs)
                xs, ys = _axis_slice(x, xmin, xmax), _axis_slice(y, ymin, ymax)
        elif (
            crs != native[0] or not np.array_equal(x, native[1]) or not np.array_equal(y, native[2])
        ):
            raise ValueError(f"{model}: shadow native grid changes between leads")
        frames.append(np.asarray(field.values[ys, xs], dtype=np.float64))
    assert native is not None
    crs, x, y = native
    durations = np.array(leads, dtype="timedelta64[h]").astype("timedelta64[ns]")
    return xr.Dataset(
        data_vars={
            _VARIABLE: (
                ("source_lead_time", "y", "x"),
                np.stack(frames),
                {"unit_id": "K", "units": "K"},
            )
        },
        coords={
            "x": x[xs],
            "y": y[ys],
            "forecast_reference_time": source_time,
            "source_lead_time": durations,
            "source_valid_time": ("source_lead_time", source_time + durations),
        },
        attrs={
            "model": model,
            "data_kind": "real_prepared_guidance",
            "target_reference_time": target.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "crs_wkt2": crs.to_wkt(),
            "grid_description": "Native Lambert shadow grid with a clipped one-cell footprint halo",
            "source_x_min": float(x[0]),
            "source_x_max": float(x[-1]),
            "source_y_min": float(y[0]),
            "source_y_max": float(y[-1]),
            **({"prepared_area_json": area.model_dump_json()} if area is not None else {}),
        },
    )
