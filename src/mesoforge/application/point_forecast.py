"""Temperature from fixed prepared inputs; synthetic fixtures remain explicitly labeled."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import pyproj
import xarray as xr

from mesoforge.alignment.station_frame import StationAlignmentError, align_station_to_model
from mesoforge.application.prepared_qpf import read_qpf_inputs, required_qpf_leads
from mesoforge.application.probability_forecast import POP, extract_probability_hour
from mesoforge.application.spatial_coverage import (
    CoverageRequiredError,
    UnsupportedCoordinateError,
    bbox_in_grid,
    bbox_within_prepared_domain,
    point_in_grid,
    validate_coordinate,
)
from mesoforge.application.surface_forecast import FIELD_UNITS, extract_surface_hour
from mesoforge.catalog.configuration import Phase2BlendConfiguration, _lists_to_tuples
from mesoforge.catalog.domains import BoundingBox
from mesoforge.forecasting.recipes import (
    DEFAULT_CONFIGURATION,
    ContributorConfiguration,
    evaluate_recipe,
    with_qpf_fields,
    with_surface_fields,
)

_DATA_KIND = "synthetic_demonstration"
_NOTICE = "Synthetic demonstration data; not a current weather forecast."
_REAL_KIND = "real_prepared_guidance"
_REAL_NOTICE = "Real HRRR/GFS guidance from fixed prepared inputs; not a current live forecast."
_SELECTED_NOTICE = (
    "Real prepared HRRR/GFS temperature with 70/30 demonstration weights; "
    "see source and valid times."
)
_VARIABLE = "air_temperature_2m"
# Owner-approved demonstration weights throughout hours 1..36, not optimized
# weights or the Phase 2 table's 60/40 HRRR/GFS row for hours 19..36.
_CRS = pyproj.CRS.from_epsg(4326)


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
    for variable, unit in FIELD_UNITS.items():
        if variable == _VARIABLE or variable not in dataset:
            continue
        extra = dataset[variable]
        if (
            extra.attrs.get("unit_id") != unit
            or extra.attrs.get("units", unit) != unit
            or extra.dims != field.dims
            or extra.dtype.kind != "f"
        ):
            raise ValueError(
                f"{model}: {variable} has invalid canonical units or native dimensions"
            )
        if (
            variable in ("eastward_wind_10m", "northward_wind_10m")
            and dataset.attrs.get("wind_reference") != "earth_relative"
        ):
            raise ValueError(
                f"{model}: prepared winds must be earth-relative before point extraction"
            )
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
    directory: Path,
    guidance: dict[str, xr.Dataset],
    target: np.datetime64,
    *,
    models: set[str] | None = None,
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
        rows = [row for row in manifest["inputs"] if models is None or row["model"] in models]
        entries = {(row["model"], row["valid_time"]): row for row in rows}
        if len(entries) != len(rows):
            raise ValueError("Real guidance manifest contains duplicate source times")
        for row in entries.values():
            for prefix in ("raw", "index"):
                _verify_file(directory, row[f"{prefix}_file"], row[f"{prefix}_sha256"])
            extras = row.get("extra_messages", [])
            variables = [extra["canonical_variable_id"] for extra in extras]
            if len(variables) != len(set(variables)) or not set(variables) <= set(FIELD_UNITS) - {
                _VARIABLE
            }:
                raise ValueError(f"{row['model']}: invalid or duplicate extra field evidence")
            for extra in extras:
                _verify_file(directory, extra["raw_file"], extra["raw_sha256"])
                if (directory / extra["raw_file"]).stat().st_size != extra["raw_bytes"]:
                    raise ValueError("Retained extra field byte count disagrees")
        if manifest.get("qpf_fields"):
            qpf_rows = [
                row
                for row in manifest.get("qpf_inputs", [])
                if models is None or row["model"] in models
            ]
            read_qpf_inputs(directory, qpf_rows)
            for row in qpf_rows:
                dataset = guidance[row["model"]]
                leads = tuple(
                    int(value / np.timedelta64(1, "h")) for value in dataset.source_lead_time.values
                )
                if row["cycle"] != _iso(dataset.forecast_reference_time.values[()]) or row[
                    "source_lead_hours"
                ] not in required_qpf_leads(row["model"], leads):
                    raise ValueError(
                        "Retained QPF parent cycle/lead disagrees with prepared guidance"
                    )
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


def _shadow_directories(directory: Path) -> list[Path]:
    """Resolve candidate views before requests; the exact point selects its view later."""
    index = directory / "coverage.json"
    if not index.is_file():
        return [directory]
    payload = json.loads(index.read_text(encoding="utf-8-sig"))
    regions = payload.get("regions") if isinstance(payload, dict) else None
    if not isinstance(regions, list):
        raise ValueError("Shadow coverage index must contain a regions list")
    candidates = []
    for region in regions:
        if not isinstance(region, dict) or not isinstance(region.get("directory"), str):
            raise ValueError("Shadow coverage region requires a prepared directory")
        area = BoundingBox.model_validate(region.get("area"))
        path = Path(region["directory"])
        if not path.is_absolute():
            path = directory / path
        candidates.append(((area.north - area.south) * (area.east - area.west), str(path), path))
    return list(dict.fromkeys(item[2] for item in sorted(candidates)))


@dataclass(frozen=True)
class _ShadowView:
    dataset: xr.Dataset
    crs: pyproj.CRS
    metadata: dict[str, Any]


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
    _configuration: ContributorConfiguration
    _shadow_views: dict[str, list[_ShadowView]]
    _surface_configuration: Phase2BlendConfiguration | None = None
    _pop_views: list[tuple[xr.Dataset, pyproj.CRS, dict[str, Any]]] = field(default_factory=list)
    _pop_guidance: dict[str, Any] | None = None

    @property
    def notice(self) -> str:
        if self._surface_configuration is not None:
            return (
                "Real prepared surface guidance: temperature retains the 70/30 demonstration "
                "control; other fields use explicit retained Phase 2 policies. "
                "See source cycles, valid times and per-field missingness."
            )
        if self._manifest is not None and (
            "cycle_selection" in self._manifest or "current_model_set" in self._manifest
        ):
            return _SELECTED_NOTICE
        return _REAL_NOTICE if self.data_kind == _REAL_KIND else _NOTICE

    @property
    def horizon_hours(self) -> tuple[int, ...]:
        """The prepared snapshot's declared forecast window, including missing hours."""
        return self._horizons

    @classmethod
    def from_directory(
        cls,
        directory: Path,
        *,
        configuration: ContributorConfiguration = DEFAULT_CONFIGURATION,
        shadow_directories: Mapping[str, Path] | None = None,
    ) -> PreparedPointForecast:
        if configuration.control_recipe.field != _VARIABLE:
            raise ValueError("Prepared point forecasts require a temperature control recipe")
        guidance: dict[str, xr.Dataset] = {}
        projections: dict[str, pyproj.CRS] = {}
        kinds: set[str] = set()
        target: np.datetime64 | None = None
        definitions = configuration.model_map()
        attached = dict(shadow_directories or {})
        for model in attached:
            if model not in definitions or definitions[model].status not in (
                "shadow",
                "evaluated",
                "deprecated",
            ):
                raise ValueError(
                    f"{model}: shadow data requires a registered shadow/evaluated/deprecated model"
                )
        active_models = tuple(item.model for item in configuration.control_recipe.contributors)
        for model in active_models:
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
                if (definitions[model].grid_type == "projected" and not crs.is_projected) or (
                    definitions[model].grid_type == "geographic" and not crs.is_geographic
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
            raise ValueError(
                "No prepared demonstration guidance: "
                + " and ".join(f"{model}.nc" for model in active_models)
                + " are missing"
            )
        data_kind = kinds.pop()
        manifest, digest = (
            _load_source_manifest(directory, guidance, target, models=set(active_models))
            if data_kind == _REAL_KIND
            else (None, None)
        )
        surface_configuration = None
        if manifest is not None and manifest.get("surface_fields"):
            policy = manifest["surface_blend_configuration"]
            selected = manifest.get("current_model_set", {}).get("selection")
            if (
                selected is not None
                and policy != selected["source_configuration"]["blend_configuration"]
            ):
                raise ValueError(
                    "Surface blend policy differs from the retained decision configuration"
                )
            surface_configuration = Phase2BlendConfiguration.model_validate(
                _lists_to_tuples(policy)
            )
        horizons = tuple(manifest.get("target_horizon_hours", (1, 2, 3))) if manifest else (1, 2, 3)
        if horizons not in ((1, 2, 3), tuple(range(1, 37))) or any(
            type(hour) is not int for hour in horizons
        ):
            raise ValueError(
                "Prepared temperature horizons must be 1..36 or the retained 1..3 slice"
            )
        shadow_views: dict[str, list[_ShadowView]] = {}
        for model, definition in definitions.items():
            if definition.status not in ("shadow", "evaluated", "deprecated"):
                continue
            views = shadow_views[model] = []
            identities: set[str] = set()
            for shadow_directory in _shadow_directories(attached.get(model, directory)):
                path = shadow_directory / f"{model}.nc"
                if not path.exists():
                    continue
                with xr.open_dataset(path, engine="h5netcdf") as opened:
                    dataset = opened.load()
                if _validate_guidance(dataset, model) != target:
                    raise ValueError(f"{model}: shadow guidance disagrees on target_reference_time")
                kind = str(dataset.attrs["data_kind"])
                wkt = dataset.attrs.get("crs_wkt2")
                if kind == _REAL_KIND and (not isinstance(wkt, str) or not wkt):
                    raise ValueError(f"{model}: real guidance requires crs_wkt2")
                crs = pyproj.CRS.from_wkt(wkt) if wkt else _CRS
                if (definition.grid_type == "projected" and not crs.is_projected) or (
                    definition.grid_type == "geographic" and not crs.is_geographic
                ):
                    raise ValueError(f"{model}: incorrect native projection")
                metadata: dict[str, Any] = {
                    "data_kind": kind,
                    "prepared_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                identity: dict[str, Any] = {
                    "data_kind": kind,
                    "cycle": _iso(
                        cast(np.datetime64, dataset["forecast_reference_time"].values[()])
                    ),
                }
                if kind == _REAL_KIND:
                    shadow_manifest, shadow_digest = _load_source_manifest(
                        shadow_directory, {model: dataset}, target, models={model}
                    )
                    metadata.update(manifest=shadow_manifest, manifest_sha256=shadow_digest)
                    identity.update(
                        inputs=[row for row in shadow_manifest["inputs"] if row["model"] == model],
                        configuration_sha256=shadow_manifest.get("configuration_sha256"),
                        source_metadata=shadow_manifest.get("source_metadata"),
                    )
                identities.add(json.dumps(identity, sort_keys=True))
                if len(identities) > 1:
                    raise ValueError(f"{model}: shadow regions disagree on retained input identity")
                views.append(_ShadowView(dataset, crs, metadata))
        return cls(
            guidance,
            target,
            projections,
            data_kind,
            manifest,
            digest,
            horizons,
            configuration,
            shadow_views,
            surface_configuration,
        )

    def check_coordinate(self, latitude: float, longitude: float) -> None:
        validate_coordinate(latitude, longitude)
        for contributor in self._configuration.control_recipe.contributors:
            model = contributor.model
            dataset = self._guidance.get(model)
            if dataset is None:
                continue
            crs = self._projections[model]
            attrs = dataset.attrs
            if "source_x_min" in attrs and not point_in_grid(
                latitude,
                longitude,
                crs,
                np.array([attrs["source_x_min"], attrs["source_x_max"]]),
                np.array([attrs["source_y_min"], attrs["source_y_max"]]),
            ):
                raise UnsupportedCoordinateError(
                    f"{model}: coordinate is outside the native model domain"
                )
            if not point_in_grid(latitude, longitude, crs, dataset.x.values, dataset.y.values):
                raise CoverageRequiredError(
                    f"{model}: prepared coverage is insufficient; "
                    "run coordinate preparation before HTTP"
                )

    def covers_area(self, area: BoundingBox) -> bool:
        """Geometry only: missing values/times remain forecast missingness, not coverage."""
        active_models = {item.model for item in self._configuration.control_recipe.contributors}
        if not active_models.issubset(self._guidance):
            return False
        for model in active_models:
            dataset = self._guidance[model]
            if bbox_in_grid(area, self._projections[model], dataset.x.values, dataset.y.values):
                continue
            attrs = dataset.attrs
            if "source_x_min" in attrs and bbox_within_prepared_domain(
                area,
                self._projections[model],
                dataset.x.values,
                dataset.y.values,
                np.array([attrs["source_x_min"], attrs["source_x_max"]]),
                np.array([attrs["source_y_min"], attrs["source_y_max"]]),
            ):
                continue
            return False
        return True

    def forecast(self, *, latitude: float, longitude: float) -> dict[str, Any]:
        """Extract the configured surface point from its local numerical baseline grid.

        The older temperature-only prepared snapshots retain their historical path.
        Grid construction belongs to explicit preparation/batch work, not HTTP reads.
        """
        if self._surface_configuration is not None:
            from mesoforge.application.local_surface_grid import (
                build_local_surface_grid,
                extract_grid_point,
            )

            grid = build_local_surface_grid(
                latitude=latitude,
                longitude=longitude,
                calculate_column=self._forecast_column,
            )
            return extract_grid_point(grid, latitude=latitude, longitude=longitude)
        return self._forecast_column(latitude=latitude, longitude=longitude)

    def _forecast_column(self, *, latitude: float, longitude: float) -> dict[str, Any]:
        """Shared native extraction/science for a local grid node (no source I/O)."""
        self.check_coordinate(latitude, longitude)
        selected_shadows = {
            model: next(
                (
                    view
                    for view in views
                    if point_in_grid(
                        latitude, longitude, view.crs, view.dataset.x.values, view.dataset.y.values
                    )
                ),
                None,
            )
            for model, views in self._shadow_views.items()
        }
        surface_datasets = {
            model: (dataset, self._projections[model], self._manifest)
            for model, dataset in self._guidance.items()
        }
        surface_datasets.update(
            {
                model: (view.dataset, view.crs, view.metadata.get("manifest"))
                for model, view in selected_shadows.items()
                if view is not None
            }
        )
        pop_view = next(
            (
                entry
                for entry in self._pop_views
                if point_in_grid(
                    latitude, longitude, entry[1], entry[0].x.values, entry[0].y.values
                )
            ),
            None,
        )
        hours: list[dict[str, Any]] = []
        for horizon in self._horizons:
            valid_time = self._target_reference_time + np.timedelta64(horizon, "h")
            sources: list[dict[str, Any]] = []
            shadow_sources: list[dict[str, Any]] = []
            weights = {
                item.model: item.weight for item in self._configuration.control_recipe.contributors
            }
            shadow_models = {
                model
                for model, definition in self._configuration.model_map().items()
                if definition.status in ("shadow", "evaluated", "deprecated")
            }
            for model, weight in (
                *weights.items(),
                *((model, 0.0) for model in sorted(shadow_models)),
            ):
                source_reasons: list[str] = []
                source: dict[str, Any] = {
                    "model": model,
                    "cycle": None,
                    "source_lead_hours": None,
                    "weight": weight,
                    "temperature": {"value": None, "unit": "K"},
                    "missing_reasons": source_reasons,
                }
                is_shadow = model in shadow_models
                source_manifest = self._manifest
                dataset = self._guidance.get(model)
                crs = self._projections.get(model)
                if is_shadow:
                    shadow_sources.append(source)
                    view = selected_shadows[model]
                    dataset, crs = (view.dataset, view.crs) if view else (None, None)
                    metadata = view.metadata if view else {}
                    source["data_kind"] = metadata.get("data_kind")
                    if "prepared_sha256" in metadata:
                        source["prepared_sha256"] = metadata["prepared_sha256"]
                    if "manifest_sha256" in metadata:
                        source["manifest_sha256"] = metadata["manifest_sha256"]
                    source_manifest = metadata.get("manifest")
                else:
                    sources.append(source)
                if dataset is None:
                    source_reasons.append(
                        f"{model}: no prepared shadow region covers the forecast coordinate"
                        if is_shadow and self._shadow_views[model]
                        else f"{model}: prepared guidance file is missing"
                    )
                    continue
                cycle = cast(
                    np.datetime64,
                    dataset["forecast_reference_time"].values.astype("datetime64[ns]")[()],
                )
                source["cycle"] = _iso(cycle)
                if not np.any(dataset["source_valid_time"].values == valid_time):
                    source_reasons.append(f"{model}: no guidance for this valid time")
                    continue
                source["source_lead_hours"] = int((valid_time - cycle) / np.timedelta64(1, "h"))
                if source_manifest is not None:
                    evidence = next(
                        row
                        for row in source_manifest["inputs"]
                        if row["model"] == model and row["valid_time"] == _iso(valid_time)
                    )
                    source.update(
                        raw_sha256=evidence["raw_sha256"],
                        source_url=evidence["source_grib_url"],
                        prepared_sha256=source_manifest["prepared_files"][model]["sha256"],
                    )
                    if is_shadow:
                        # Retain the exact per-message evidence without changing the
                        # active control's source dictionary or manifest identity.
                        source["acquisition"] = deepcopy(evidence)
                        if "source_metadata" in source_manifest:
                            source["source_metadata"] = deepcopy(source_manifest["source_metadata"])
                    if not is_shadow and (
                        "cycle_selection" in source_manifest
                        or "current_model_set" in source_manifest
                    ):
                        source["acquisition"] = {
                            key: evidence[key]
                            for key in (
                                "raw_bytes",
                                "index_sha256",
                                "index_bytes",
                                "source_index_url",
                                "byte_start",
                                "byte_end",
                                "endpoint",
                                "grib_retrieved_at",
                                "index_retrieved_at",
                                "grib_available_at",
                                "index_available_at",
                                "grib_last_modified",
                                "index_last_modified",
                                "etag",
                            )
                        }
                try:
                    aligned = align_station_to_model(
                        dataset,
                        crs=cast(pyproj.CRS, crs),
                        station_latitude=latitude,
                        station_longitude=longitude,
                        canonical_variable_id=_VARIABLE,
                        target_horizon_hours=(horizon,),
                        target_reference_time=self._target_reference_time,
                    )
                except StationAlignmentError:
                    source_reasons.append(
                        f"{model}: grid coverage or finite corner values are unavailable"
                    )
                    continue
                if horizon not in aligned or not math.isfinite(aligned[horizon].value):
                    source_reasons.append(f"{model}: no finite temperature for this valid time")
                    continue
                source["temperature"]["value"] = aligned[horizon].value
            reasons = [reason for source in sources for reason in source["missing_reasons"]]
            temperature = evaluate_recipe(
                self._configuration.control_recipe,
                {source["model"]: source["temperature"]["value"] for source in sources},
            ).value
            hours.append(
                {
                    "horizon_hours": horizon,
                    "valid_time": _iso(valid_time),
                    "temperature": {"value": temperature, "unit": "K"},
                    "sources": sources,
                    "missing_reasons": reasons,
                    **({"shadow_sources": shadow_sources} if shadow_sources else {}),
                }
            )
            if self._surface_configuration is not None:
                selection = (self._manifest or {}).get("current_model_set", {}).get("selection")
                hours[-1]["surface"] = extract_surface_hour(
                    datasets=surface_datasets,
                    temperature_sources=[*sources, *shadow_sources],
                    temperature_k=temperature,
                    latitude=latitude,
                    longitude=longitude,
                    horizon=horizon,
                    target_reference_time=self._target_reference_time,
                    configuration=self._surface_configuration,
                    selection=selection,
                )
                if self._pop_guidance is not None:
                    pop, native_pop = extract_probability_hour(
                        pop_view,
                        latitude=latitude,
                        longitude=longitude,
                        horizon=horizon,
                        target_reference_time=self._target_reference_time,
                        policy=self._surface_configuration.pop_policy,
                        unavailable_reason=(
                            self._pop_guidance.get("reason")
                            or "NBM: no prepared probability region covers this coordinate"
                        ),
                    )
                    hours[-1]["surface"]["fields"][POP] = pop
                    hours[-1]["surface"]["contributors"]["NBM"] = {
                        "model": "NBM",
                        "role": "field_source",
                        "native_supported_fields": [POP],
                        "fields": {POP: native_pop},
                    }
        contributor_configuration = (
            with_surface_fields(self._configuration)
            if self._surface_configuration is not None
            else self._configuration
        )
        if self._manifest is not None and self._manifest.get("qpf_fields"):
            contributor_configuration = with_qpf_fields(contributor_configuration)
        result: dict[str, Any] = {
            "data_kind": self.data_kind,
            "notice": self.notice,
            "latitude": latitude,
            "longitude": longitude,
            "target_reference_time": _iso(self._target_reference_time),
            "hours": hours,
            "contributor_configuration": contributor_configuration.model_dump(mode="json"),
        }
        if self._manifest_sha256 is not None:
            result["manifest_sha256"] = self._manifest_sha256
        if self._pop_guidance is not None:
            result["pop_guidance"] = {
                **deepcopy(self._pop_guidance),
                "source_metadata": [
                    {
                        key: deepcopy(manifest.get(key))
                        for key in (
                            "manifest_sha256",
                            "source_metadata",
                            "selection_evidence",
                            "created_at",
                            "code_identity",
                            "prepared_files",
                        )
                    }
                    for _, _, manifest in self._pop_views
                ],
                "note": (
                    "NBM probability preparation is separate from "
                    "the four-model temperature decision"
                ),
            }
        if self._manifest is not None and self._manifest.get("qpf_fields"):
            result["qpf_preparation"] = {
                "created_at": self._manifest.get("created_at"),
                "code_identity": self._manifest.get("code_identity"),
                "acquisition": self._manifest.get("qpf_acquisition"),
                "source_selection": (
                    "QPF discovered with the current model set"
                    if self._manifest.get("current_model_set", {})
                    .get("selection", {})
                    .get("qpf_fields")
                    else "QPF added by later preparation; original discovery is historical"
                ),
            }
        if self._manifest is not None and "cycle_selection" in self._manifest:
            selection = self._manifest["cycle_selection"]
            # Full acquisition/discovery evidence stays in the checksummed manifest.
            # Carry the selection decision with the immutable issued payload as well.
            result["cycle_selection"] = {
                key: selection[key]
                for key in (
                    "execution_time",
                    "target_reference_time",
                    "first_valid_time",
                    "last_valid_time",
                    "selected_cycles",
                    "status",
                    "completed_at",
                )
            }
            result["cycle_selection"]["candidates"] = {
                model: [
                    {key: row[key] for key in ("cycle", "status", "reason", "source_lead_hours")}
                    for row in rows
                ]
                for model, rows in selection["candidates"].items()
            }
        if self._manifest is not None and "current_model_set" in self._manifest:
            result["current_model_set"] = deepcopy(self._manifest["current_model_set"])
        return result
