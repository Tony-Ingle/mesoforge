"""Temperature from fixed prepared inputs; synthetic fixtures remain explicitly labeled."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import pyproj
import xarray as xr

from mesoforge.alignment.station_frame import StationAlignmentError, align_station_to_model
from mesoforge.forecasting.scalar_blend import Contribution, blend_scalar

_DATA_KIND = "synthetic_demonstration"
_NOTICE = "Synthetic demonstration data; not a current weather forecast."
_REAL_KIND = "real_prepared_guidance"
_REAL_NOTICE = "Real HRRR/GFS guidance from fixed prepared inputs; not a current live forecast."
_VARIABLE = "air_temperature_2m"
_WEIGHTS = {"HRRR": 0.7, "GFS": 0.3}
# Owner-approved demonstration weights throughout hours 1..36, not optimized
# weights or the Phase 2 table's 60/40 HRRR/GFS row for hours 19..36.
_CRS = pyproj.CRS.from_epsg(4326)


class UnsupportedCoordinateError(ValueError):
    """The requested coordinate is outside the demonstration's supported rectangle."""


def validate_supported_coordinate(latitude: float, longitude: float) -> None:
    """Apply the existing demonstration coverage boundary without loading guidance."""
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not 45.5 <= latitude <= 46.0
        or not -93.5 <= longitude <= -93.0
    ):
        raise UnsupportedCoordinateError(
            "Demonstration coordinates must be within latitude 45.5..46.0, longitude -93.5..-93.0"
        )


def _iso(value: np.datetime64) -> str:
    return str(np.datetime_as_string(value, unit="s")) + "Z"


def _target_time(value: object) -> np.datetime64:
    if not isinstance(value, str):
        raise ValueError("target_reference_time must be an explicit UTC timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("target_reference_time must be an explicit UTC timestamp")
    if parsed.minute or parsed.second or parsed.microsecond:
        raise ValueError("target_reference_time must identify an exact hour")
    return np.datetime64(parsed.replace(tzinfo=None), "ns")


def prepare_demo_files(directory: Path) -> None:
    """Populate an empty directory with deterministic synthetic files, without overwrites."""
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        return
    target = np.datetime64("2026-08-30T12:00:00", "ns")
    x = np.array([-93.5, -93.25, -93.0], dtype=np.float64)
    y = np.array([45.5, 45.75, 46.0], dtype=np.float64)
    longitude, latitude = np.meshgrid(x, y)
    for model, age, base, lat_factor, lon_factor in (
        ("HRRR", 0, 280.0, 2.0, 4.0),
        ("GFS", 6, 290.0, 4.0, 2.0),
    ):
        cycle = target - np.timedelta64(age, "h")
        leads = np.array([1 + age, 2 + age, 3 + age], dtype="timedelta64[h]").astype(
            "timedelta64[ns]"
        )
        values = np.stack(
            [
                base + hour + lat_factor * (latitude - 45.5) + lon_factor * (longitude + 93.5)
                for hour in (1, 2, 3)
            ]
        )
        dataset = xr.Dataset(
            data_vars={_VARIABLE: (("source_lead_time", "y", "x"), values, {"unit_id": "K"})},
            coords={
                "x": x,
                "y": y,
                "forecast_reference_time": cycle,
                "source_lead_time": leads,
                "source_valid_time": ("source_lead_time", cycle + leads),
            },
            attrs={
                "model": model,
                "data_kind": _DATA_KIND,
                "notice": _NOTICE,
                "target_reference_time": _iso(target),
                "grid_description": "Synthetic latitude/longitude demonstration grid",
            },
        )
        with (directory / f"{model}.nc").open("x+b") as output:
            dataset.to_netcdf(output, engine="h5netcdf", format="NETCDF4")


def _validate_guidance(dataset: xr.Dataset, model: str) -> np.datetime64:
    if dataset.attrs.get("model") != model or dataset.attrs.get("data_kind") not in (
        _DATA_KIND,
        _REAL_KIND,
    ):
        raise ValueError(f"{model}: file must identify the matching contributor and data kind")
    target = _target_time(dataset.attrs.get("target_reference_time"))
    required = {
        _VARIABLE,
        "x",
        "y",
        "forecast_reference_time",
        "source_lead_time",
        "source_valid_time",
    }
    if not required.issubset(dataset.variables):
        raise ValueError(f"{model}: prepared guidance is missing required fields or coordinates")
    field = dataset[_VARIABLE]
    if field.attrs.get("unit_id") != "K" or field.attrs.get("units", "K") != "K":
        raise ValueError(f"{model}: prepared temperature must use K")
    if field.dims != ("source_lead_time", "y", "x") or field.dtype.kind != "f":
        raise ValueError(f"{model}: temperature must have floating source_lead_time/y/x values")
    for axis in ("x", "y"):
        values = dataset[axis].values
        if (
            dataset[axis].dims != (axis,)
            or len(values) < 2
            or not np.all(np.isfinite(values))
            or not (np.all(np.diff(values) > 0) or np.all(np.diff(values) < 0))
        ):
            raise ValueError(f"{model}: {axis} must be a finite, strictly monotonic grid axis")
    cycle = dataset["forecast_reference_time"]
    leads = dataset["source_lead_time"]
    valid = dataset["source_valid_time"]
    if (
        cycle.ndim != 0
        or cycle.dtype.kind != "M"
        or leads.dims != ("source_lead_time",)
        or leads.dtype.kind != "m"
        or valid.dims != ("source_lead_time",)
        or valid.dtype.kind != "M"
    ):
        raise ValueError(f"{model}: source cycle, leads and valid times have invalid metadata")
    cycle_value = cast(np.datetime64, cycle.values.astype("datetime64[ns]")[()])
    lead_values = leads.values.astype("timedelta64[ns]")
    if (
        np.isnat(cycle_value)
        or cycle_value > target
        or cycle_value.astype("datetime64[h]") != cycle_value
        or np.any(np.isnat(lead_values))
        or np.any(lead_values < np.timedelta64(0, "h"))
        or np.any(lead_values % np.timedelta64(1, "h") != np.timedelta64(0, "ns"))
        or len(np.unique(lead_values)) != len(lead_values)
        or not np.array_equal(valid.values, cycle_value + lead_values)
    ):
        raise ValueError(f"{model}: source cycle, leads and valid times disagree")
    return target


def _verify_file(directory: Path, filename: str, expected: str) -> None:
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError(f"Missing or invalid retained input path: {filename}")
    with path.open("rb") as retained:
        actual = hashlib.file_digest(retained, "sha256").hexdigest()
    if actual != expected:
        raise ValueError(f"Checksum mismatch for {filename}")


def _load_source_manifest(
    directory: Path, guidance: dict[str, xr.Dataset], target: np.datetime64
) -> tuple[dict[str, Any], str]:
    """Verify retained source and prepared file identities before serving real inputs."""
    path = directory / "manifest.json"
    if not path.is_file():
        raise ValueError("Real prepared guidance requires manifest.json")
    payload = path.read_bytes()
    manifest = json.loads(payload)
    try:
        if (
            manifest["data_kind"] != _REAL_KIND
            or _target_time(manifest["target_reference_time"]) != target
        ):
            raise ValueError("Real guidance manifest data kind or target time disagrees")
        entries = {(row["model"], row["valid_time"]): row for row in manifest["inputs"]}
        if len(entries) != len(manifest["inputs"]):
            raise ValueError("Real guidance manifest contains duplicate source times")
        for row in entries.values():
            for prefix in ("raw", "index"):
                _verify_file(directory, row[f"{prefix}_file"], row[f"{prefix}_sha256"])
        for model, dataset in guidance.items():
            prepared = manifest["prepared_files"][model]
            if prepared["file"] != f"{model}.nc":
                raise ValueError(f"{model}: manifest names the wrong prepared file")
            _verify_file(directory, prepared["file"], prepared["sha256"])
            cycle = cast(np.datetime64, dataset["forecast_reference_time"].values[()])
            for lead, valid in zip(
                dataset["source_lead_time"].values, dataset["source_valid_time"].values, strict=True
            ):
                row = entries[(model, _iso(valid))]
                if row["cycle"] != _iso(cycle) or row["source_lead_hours"] != int(
                    lead / np.timedelta64(1, "h")
                ):
                    raise ValueError(f"{model}: source cycle/lead disagrees with retained evidence")
                if not row["source_grib_url"].startswith("https://"):
                    raise ValueError(f"{model}: missing source URL in retained evidence")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Incomplete real guidance source manifest") from exc
    return manifest, hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class PreparedPointForecast:
    """Eagerly loaded demonstration guidance; request-time calculation does no I/O."""

    _guidance: dict[str, xr.Dataset]
    _target_reference_time: np.datetime64
    _projections: dict[str, pyproj.CRS]
    data_kind: str
    _manifest: dict[str, Any] | None
    _manifest_sha256: str | None
    _horizons: tuple[int, ...]

    @property
    def notice(self) -> str:
        return _REAL_NOTICE if self.data_kind == _REAL_KIND else _NOTICE

    @property
    def horizon_hours(self) -> tuple[int, ...]:
        """The prepared snapshot's declared forecast window, including missing hours."""
        return self._horizons

    @classmethod
    def from_directory(cls, directory: Path) -> PreparedPointForecast:
        guidance: dict[str, xr.Dataset] = {}
        projections: dict[str, pyproj.CRS] = {}
        kinds: set[str] = set()
        target: np.datetime64 | None = None
        for model in _WEIGHTS:
            path = directory / f"{model}.nc"
            if not path.exists():
                continue
            with xr.open_dataset(path, engine="h5netcdf") as opened:
                dataset = opened.load()
            model_target = _validate_guidance(dataset, model)
            kind = str(dataset.attrs["data_kind"])
            kinds.add(kind)
            if len(kinds) != 1:
                raise ValueError("Cannot mix real and synthetic guidance")
            if kind == _REAL_KIND:
                wkt = dataset.attrs.get("crs_wkt2")
                if not isinstance(wkt, str) or not wkt:
                    raise ValueError(f"{model}: real guidance requires crs_wkt2")
                crs = pyproj.CRS.from_wkt(wkt)
                if (model == "HRRR" and not crs.is_projected) or (
                    model == "GFS" and not crs.is_geographic
                ):
                    raise ValueError(f"{model}: incorrect native projection")
                projections[model] = crs
            else:
                projections[model] = _CRS
            if target is not None and model_target != target:
                raise ValueError("Prepared model files disagree on target_reference_time")
            target = model_target
            guidance[model] = dataset
        if target is None:
            raise ValueError("No prepared demonstration guidance: HRRR.nc and GFS.nc are missing")
        data_kind = kinds.pop()
        manifest, digest = (
            _load_source_manifest(directory, guidance, target)
            if data_kind == _REAL_KIND
            else (None, None)
        )
        horizons = tuple(manifest.get("target_horizon_hours", (1, 2, 3))) if manifest else (1, 2, 3)
        if horizons not in ((1, 2, 3), tuple(range(1, 37))) or any(
            type(hour) is not int for hour in horizons
        ):
            raise ValueError(
                "Prepared temperature horizons must be 1..36 or the retained 1..3 slice"
            )
        return cls(guidance, target, projections, data_kind, manifest, digest, horizons)

    def forecast(self, *, latitude: float, longitude: float) -> dict[str, Any]:
        validate_supported_coordinate(latitude, longitude)
        hours: list[dict[str, Any]] = []
        for horizon in self._horizons:
            valid_time = self._target_reference_time + np.timedelta64(horizon, "h")
            sources: list[dict[str, Any]] = []
            contributions: list[Contribution] = []
            reasons: list[str] = []
            for model, weight in _WEIGHTS.items():
                source: dict[str, Any] = {
                    "model": model,
                    "cycle": None,
                    "source_lead_hours": None,
                    "weight": weight,
                }
                sources.append(source)
                dataset = self._guidance.get(model)
                if dataset is None:
                    reasons.append(f"{model}: prepared guidance file is missing")
                    continue
                cycle = cast(
                    np.datetime64,
                    dataset["forecast_reference_time"].values.astype("datetime64[ns]")[()],
                )
                source["cycle"] = _iso(cycle)
                if not np.any(dataset["source_valid_time"].values == valid_time):
                    reasons.append(f"{model}: no guidance for this valid time")
                    continue
                source["source_lead_hours"] = int((valid_time - cycle) / np.timedelta64(1, "h"))
                if self._manifest is not None:
                    evidence = next(
                        row
                        for row in self._manifest["inputs"]
                        if row["model"] == model and row["valid_time"] == _iso(valid_time)
                    )
                    source.update(
                        raw_sha256=evidence["raw_sha256"],
                        source_url=evidence["source_grib_url"],
                        prepared_sha256=self._manifest["prepared_files"][model]["sha256"],
                    )
                try:
                    aligned = align_station_to_model(
                        dataset,
                        crs=self._projections[model],
                        station_latitude=latitude,
                        station_longitude=longitude,
                        canonical_variable_id=_VARIABLE,
                        target_horizon_hours=(horizon,),
                        target_reference_time=self._target_reference_time,
                    )
                except StationAlignmentError:
                    reasons.append(
                        f"{model}: grid coverage or finite corner values are unavailable"
                    )
                    continue
                if horizon not in aligned or not math.isfinite(aligned[horizon].value):
                    reasons.append(f"{model}: no finite temperature for this valid time")
                    continue
                contributions.append(
                    Contribution(model=model, value=aligned[horizon].value, weight=weight)
                )
            temperature = None if reasons else blend_scalar(tuple(contributions)).blended_value
            hours.append(
                {
                    "horizon_hours": horizon,
                    "valid_time": _iso(valid_time),
                    "temperature": {"value": temperature, "unit": "K"},
                    "sources": sources,
                    "missing_reasons": reasons,
                }
            )
        result = {
            "data_kind": self.data_kind,
            "notice": self.notice,
            "latitude": latitude,
            "longitude": longitude,
            "target_reference_time": _iso(self._target_reference_time),
            "hours": hours,
        }
        if self._manifest_sha256 is not None:
            result["manifest_sha256"] = self._manifest_sha256
        return result
