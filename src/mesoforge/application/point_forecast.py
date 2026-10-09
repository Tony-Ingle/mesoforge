"""Temperature from fixed prepared inputs; synthetic fixtures remain explicitly labeled."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import pyproj
import xarray as xr

from mesoforge.alignment.station_frame import StationAlignmentError, align_station_to_model
from mesoforge.application.cloud_cover import CloudView, extract_cloud_contributors
from mesoforge.application.ice import IceView, extract_ice_contributors
from mesoforge.application.native_surface_inputs import (
    QPF,
    extract_native_inputs,
    extract_native_qpf,
    qpf_partition,
)
from mesoforge.application.precipitation_type import PTYPE, TypeView, extract_precipitation_type
from mesoforge.application.prepared_qpf import read_qpf_inputs, required_qpf_leads
from mesoforge.application.probability_contributors import (
    ProbabilityView,
    extract_probability_contributors,
)
from mesoforge.application.probability_forecast import POP, extract_probability_hour
from mesoforge.application.snowfall_amount_forecast import (
    AMOUNT,
    AmountView,
    extract_snowfall_amount_contributors,
)
from mesoforge.application.snowfall_forecast import SNOW, SnowView, extract_snowfall_contributors
from mesoforge.application.spatial_coverage import (
    CoverageRequiredError,
    UnsupportedCoordinateError,
    bbox_in_grid,
    bbox_within_prepared_domain,
    point_in_grid,
    validate_coordinate,
)
from mesoforge.application.surface_forecast import FIELD_UNITS, extract_surface_inputs
from mesoforge.application.thunder import THUNDER, ThunderView, extract_thunder_contributors
from mesoforge.application.visibility import VisibilityView, extract_visibility_contributors
from mesoforge.catalog.configuration import Phase2BlendConfiguration, _lists_to_tuples
from mesoforge.catalog.domains import BoundingBox
from mesoforge.common.horizon import horizon_for
from mesoforge.forecasting.field_blend import BlendState, FieldBlendEngine
from mesoforge.forecasting.provisional_policy import (
    POP6,
    PROVISIONAL_MULTIMODEL_POLICY,
    provisional_policy,
)
from mesoforge.forecasting.recipes import (
    DEFAULT_CONFIGURATION,
    ContributorConfiguration,
    RecipeContributor,
    with_qpf_fields,
    with_surface_fields,
)
from mesoforge.guidance.coverage import is_prepared_window

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


def _recipe_contributors(configuration: ContributorConfiguration) -> tuple[RecipeContributor, ...]:
    if configuration.control_recipe is None:
        raise ValueError("Native policy configuration has no historical fixed recipe")
    return configuration.control_recipe.contributors


class ReferenceCoverageError(ValueError):
    """The prepared valid times do not cover hours 1..36 after the requested reference."""


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
    source_directory = directory
    if "source_directory" in manifest:
        source_directory = Path(manifest["source_directory"]).resolve()
        if (
            source_directory != directory.resolve()
            and source_directory.parent != directory.resolve().parent
        ):
            raise ValueError(
                "Shared raw source directory must be within the same prepared region bundle"
            )
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
                _verify_file(source_directory, row[f"{prefix}_file"], row[f"{prefix}_sha256"])
            extras = row.get("extra_messages", [])
            variables = [extra["canonical_variable_id"] for extra in extras]
            extra_fields = set(FIELD_UNITS) - {_VARIABLE}
            if horizon_for(manifest).duration_hours == 120:
                extra_fields.update(
                    {
                        "cloud_area_fraction",
                        POP,
                        QPF,
                        QPF + "_equivalent_parent",
                        "wind_speed_10m",
                        "wind_from_direction_10m",
                    }
                )
            if len(variables) != len(set(variables)) or not set(variables) <= extra_fields:
                raise ValueError(f"{row['model']}: invalid or duplicate extra field evidence")
            for extra in extras:
                _verify_file(source_directory, extra["raw_file"], extra["raw_sha256"])
                if (source_directory / extra["raw_file"]).stat().st_size != extra["raw_bytes"]:
                    raise ValueError("Retained extra field byte count disagrees")
        if manifest.get("qpf_fields") and horizon_for(manifest).duration_hours != 120:
            qpf_rows = [
                row
                for row in manifest.get("qpf_inputs", [])
                if models is None or row["model"] in models
            ]
            read_qpf_inputs(source_directory, qpf_rows)
            for row in qpf_rows:
                dataset = guidance[row["model"]]
                leads = tuple(
                    int(value / np.timedelta64(1, "h")) for value in dataset.source_lead_time.values
                )
                if row["cycle"] != _iso(dataset.forecast_reference_time.values[()]) or row[
                    "source_lead_hours"
                ] not in (
                    leads
                    if horizon_for(manifest).duration_hours == 120
                    else required_qpf_leads(row["model"], leads)
                ):
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
    _probability_views: list[ProbabilityView] = field(default_factory=list)
    _type_views: list[TypeView] = field(default_factory=list)
    _type_guidance: dict[str, Any] | None = None
    _snow_views: list[SnowView] = field(default_factory=list)
    _snow_guidance: dict[str, Any] | None = None
    _snow_amount_views: list[AmountView] = field(default_factory=list)
    _snow_amount_guidance: dict[str, Any] | None = None
    _cloud_views: list[CloudView] = field(default_factory=list)
    _cloud_guidance: dict[str, Any] | None = None
    _visibility_views: list[VisibilityView] = field(default_factory=list)
    _visibility_guidance: dict[str, Any] | None = None
    _thunder_views: list[ThunderView] = field(default_factory=list)
    _thunder_guidance: dict[str, Any] | None = None
    _ice_views: list[IceView] = field(default_factory=list)
    _ice_guidance: dict[str, Any] | None = None
    # Set only on a reference view: the window the guidance was prepared for.
    _prepared_reference_time: np.datetime64 | None = None
    _prepared_horizons: tuple[int, ...] | None = None
    # Governed ACTIVE blend policies resolved by the background baseline builder.
    # Empty means the current Phase 2 defaults; never set by a location job.
    _policy_overrides: dict[str, Any] = field(default_factory=dict)

    def with_policy_overrides(self, overrides: dict[str, Any]) -> PreparedPointForecast:
        """The same prepared guidance blended under explicit governed field policies."""
        return replace(self, _policy_overrides=dict(overrides))

    @property
    def target_reference_time(self) -> np.datetime64:
        return self._target_reference_time

    @property
    def prepared_reference_time(self) -> np.datetime64:
        """The reference hour the guidance was prepared for, even when a view moved it."""
        if self._prepared_reference_time is not None:
            return self._prepared_reference_time
        return self._target_reference_time

    def prepared_valid_times(self) -> dict[str, list[np.datetime64]]:
        """Absolute valid times each active contributor actually holds."""
        return {
            model: [
                cast(np.datetime64, value.astype("datetime64[ns]"))
                for value in dataset["source_valid_time"].values
            ]
            for model, dataset in self._guidance.items()
        }

    def reference_view(self, reference_time: datetime | np.datetime64) -> PreparedPointForecast:
        """The same prepared guidance read as hours 1..36 after a later reference hour.

        Nothing is recomputed or re-acquired: every valid time is looked up as before,
        so source cycles, leads and evidence stay exactly what was prepared. The view is
        refused unless each active contributor holds all 36 valid times.
        """
        if isinstance(reference_time, datetime):
            if reference_time.tzinfo is None or reference_time.utcoffset() is None:
                raise ValueError("Reference time must include a timezone")
            reference = np.datetime64(reference_time.astimezone(UTC).replace(tzinfo=None), "ns")
        else:
            reference = reference_time.astype("datetime64[ns]")
        if reference.astype("datetime64[h]") != reference:
            raise ValueError("Reference time must be an exact UTC hour")
        prepared = self.prepared_reference_time
        if reference < prepared:
            raise ReferenceCoverageError(
                f"Reference {_iso(reference)} precedes the prepared window start {_iso(prepared)}"
            )
        horizon = horizon_for(self._manifest or {})
        native = horizon.duration_hours == 120
        if (
            self._prepared_horizons is None
            and not native
            and not is_prepared_window(self._horizons)
        ):
            raise ReferenceCoverageError("Reference views require a complete prepared window")
        horizons = horizon.leads
        required = [reference + np.timedelta64(hour, "h") for hour in horizons]
        if native:
            prepared_hours = self._prepared_horizons or self._horizons
            if reference + np.timedelta64(horizon.duration_hours, "h") > prepared + np.timedelta64(
                max(prepared_hours), "h"
            ):
                raise ReferenceCoverageError(
                    "Requested reference exceeds the retained native preparation window"
                )
        for model, valid_times in () if native else self.prepared_valid_times().items():
            held = set(valid_times)
            missing = [_iso(valid) for valid in required if valid not in held]
            if missing:
                raise ReferenceCoverageError(
                    f"{model}: prepared guidance lacks {len(missing)} of the 36 valid times "
                    f"after {_iso(reference)}; first missing {missing[0]}"
                )
        return replace(
            self,
            _target_reference_time=reference,
            _horizons=horizons,
            _prepared_reference_time=prepared,
            _prepared_horizons=(
                self._prepared_horizons if self._prepared_horizons is not None else self._horizons
            ),
        )

    @property
    def notice(self) -> str:
        if horizon_for(self._manifest or {}).duration_hours == 120:
            return (
                "Provisional field-specific multi-model forecast; transparent role/lead priors, "
                "not skill-optimized weights. Native horizons and event intervals are preserved."
            )
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
        if (
            configuration.control_recipe is not None
            and configuration.control_recipe.field != _VARIABLE
        ):
            raise ValueError("Prepared point forecasts require a temperature control recipe")
        guidance: dict[str, xr.Dataset] = {}
        projections: dict[str, pyproj.CRS] = {}
        kinds: set[str] = set()
        target: np.datetime64 | None = None
        definitions = configuration.model_map()
        declared_manifest = (
            json.loads((directory / "manifest.json").read_bytes())
            if (directory / "manifest.json").is_file()
            else {}
        )
        native = horizon_for(declared_manifest).duration_hours == 120
        if native:
            from mesoforge.forecasting.recipes import PROVISIONAL_CONFIGURATION

            if configuration not in (DEFAULT_CONFIGURATION, PROVISIONAL_CONFIGURATION):
                raise ValueError("Native preparation requires its registered provisional policy")
            configuration = PROVISIONAL_CONFIGURATION
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
        active_models = (
            tuple(
                model
                for model in ("HRRR", "RAP", "GFS", "IFS", "NBM")
                if model in declared_manifest.get("prepared_files", {})
            )
            if native
            else tuple(item.model for item in _recipe_contributors(configuration))
        )
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
                grid_type = (
                    ("projected" if model in ("HRRR", "RAP", "NBM") else "geographic")
                    if native
                    else definitions[model].grid_type
                )
                if (grid_type == "projected" and not crs.is_projected) or (
                    grid_type == "geographic" and not crs.is_geographic
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
        if native and (
            horizons != tuple(range(1, len(horizons) + 1)) or not 120 <= len(horizons) <= 126
        ):
            raise ValueError(
                "Native preparation requires a declared complete 120-hour window "
                "and bounded reference buffer"
            )
        if not native and horizons != (1, 2, 3) and not is_prepared_window(horizons):
            raise ValueError(
                "Prepared temperature horizons must be a 1..36 to 1..42 window "
                "or the retained 1..3 slice"
            )
        shadow_views: dict[str, list[_ShadowView]] = {}
        for model, definition in definitions.items():
            if native:
                continue
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
        native = horizon_for(self._manifest or {}).duration_hours == 120
        if native:
            if any(
                point_in_grid(
                    latitude, longitude, self._projections[model], data.x.values, data.y.values
                )
                for model, data in self._guidance.items()
            ):
                return
            raise CoverageRequiredError("No retained native source covers this coordinate")
        models = (
            self._guidance
            if horizon_for(self._manifest or {}).duration_hours == 120
            else {row.model for row in _recipe_contributors(self._configuration)}
        )
        for model in models:
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
        if horizon_for(self._manifest or {}).duration_hours == 120:
            return any(
                bbox_in_grid(area, self._projections[model], data.x.values, data.y.values)
                for model, data in self._guidance.items()
            )
        active_models = (
            set(self._guidance)
            if horizon_for(self._manifest or {}).duration_hours == 120
            else {item.model for item in _recipe_contributors(self._configuration)}
        )
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

            # This grid is built here and dropped on return, so its columns and the
            # grid itself are handed over instead of being copied a second time.
            grid = build_local_surface_grid(
                latitude=latitude,
                longitude=longitude,
                calculate_column=self._forecast_column,
                columns_owned=True,
            )
            return extract_grid_point(grid, latitude=latitude, longitude=longitude, copy_grid=False)
        return self._forecast_column(latitude=latitude, longitude=longitude)

    def point_column(self, *, latitude: float, longitude: float) -> dict[str, Any]:
        """One column of the same science without building the surrounding grid.

        Used to validate a prepared snapshot cheaply; issuance still builds the grid.
        """
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
            model: (
                dataset,
                self._projections[model],
                self._manifest
                if self.data_kind == _REAL_KIND
                or horizon_for(self._manifest or {}).duration_hours != 120
                else None,
            )
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
        native = horizon_for(self._manifest or {}).duration_hours == 120
        if native and "NBM" in surface_datasets:
            nbm_ds, nbm_crs, nbm_manifest = surface_datasets["NBM"]
            if nbm_manifest is not None:
                pop_view = (nbm_ds, nbm_crs, nbm_manifest)
        engine = FieldBlendEngine(
            contributors=self._configuration,
            phase2=self._surface_configuration,
            policy_overrides=self._policy_overrides,
            policy_family=PROVISIONAL_MULTIMODEL_POLICY if native else None,
        )
        hours: list[dict[str, Any]] = []
        for horizon in self._horizons:
            shadow_sources: list[dict[str, Any]]
            valid_time = self._target_reference_time + np.timedelta64(horizon, "h")
            if native:
                state, surface_contributors, sources = extract_native_inputs(
                    surface_datasets,
                    reference=self._target_reference_time,
                    horizon=horizon,
                    latitude=latitude,
                    longitude=longitude,
                )
                shadow_sources = []
                reasons = []
            else:
                sources = []
                shadow_sources = []
                weights = {
                    item.model: item.weight for item in _recipe_contributors(self._configuration)
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
                                source["source_metadata"] = deepcopy(
                                    source_manifest["source_metadata"]
                                )
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
                state = BlendState(
                    horizon=horizon,
                    contributors={
                        source["model"]: {_VARIABLE: source["temperature"]["value"]}
                        for source in sources
                    },
                )
                surface_contributors = {}
                if self._surface_configuration is not None:
                    selection = (self._manifest or {}).get("current_model_set", {}).get("selection")
                    state, surface_contributors = extract_surface_inputs(
                        datasets=surface_datasets,
                        temperature_sources=[*sources, *shadow_sources],
                        latitude=latitude,
                        longitude=longitude,
                        horizon=horizon,
                        target_reference_time=self._target_reference_time,
                        selection=selection,
                    )
            temperature_field = engine.blend_field(_VARIABLE, state)
            temperature = temperature_field["value"]
            if native:
                for source in sources:
                    source["weight"] = temperature_field.get("weights", {}).get(
                        source["model"], 0.0
                    )
                reasons = list(temperature_field["missing_reasons"])
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
                hours[-1]["surface"] = {
                    "fields": engine.surface_fields(state),
                    "source_validation": state.source_validation,
                    "contributors": surface_contributors,
                }
                if self._pop_guidance is not None or native:
                    pop, native_pop = extract_probability_hour(
                        pop_view,
                        latitude=latitude,
                        longitude=longitude,
                        horizon=horizon,
                        target_reference_time=self._target_reference_time,
                        policy=self._surface_configuration.pop_policy,
                        unavailable_reason=(
                            (self._pop_guidance or {}).get("reason")
                            or "NBM: no prepared probability region covers this coordinate"
                        ),
                    )
                    hours[-1]["surface"]["fields"][POP] = pop
                    nbm_pop_contributor = {
                        "model": "NBM",
                        "role": "field_source",
                        "native_supported_fields": [POP],
                        "fields": {POP: native_pop},
                    }
                    if native:
                        hours[-1]["surface"]["contributors"]["NBM"]["fields"][POP] = native_pop
                    else:
                        hours[-1]["surface"]["contributors"]["NBM"] = nbm_pop_contributor
                    if self._probability_views:
                        hours[-1]["surface"]["probability_guidance"] = (
                            extract_probability_contributors(
                                self._probability_views,
                                latitude=latitude,
                                longitude=longitude,
                                valid_time=_iso(valid_time),
                                active={**pop, "spatial_support": {"kind": "grid_point"}},
                            )
                        )
                if native:
                    probability_rows = (
                        hours[-1]["surface"].get("probability_guidance", {}).get("contributors", [])
                    )
                    hours[-1]["surface"]["fields"][POP6] = self._probability_event(
                        engine, probability_rows, valid_time=valid_time, horizon=horizon
                    )
                if self._type_guidance is not None:
                    evidence = extract_precipitation_type(
                        self._type_views,
                        latitude=latitude,
                        longitude=longitude,
                        valid_time=_iso(valid_time),
                    )
                    hours[-1]["surface"]["fields"][PTYPE] = evidence["field"]
                    hours[-1]["surface"]["precipitation_type_guidance"] = evidence
                if self._snow_guidance is not None:
                    snowfall = extract_snowfall_contributors(
                        self._snow_views,
                        latitude=latitude,
                        longitude=longitude,
                        valid_time=_iso(valid_time),
                        source_status=self._snow_guidance.get("source_status", {}),
                    )
                    hours[-1]["surface"]["fields"][SNOW] = snowfall["field"]
                    hours[-1]["surface"]["snowfall_guidance"] = snowfall
                if self._snow_amount_guidance is not None:
                    amounts = extract_snowfall_amount_contributors(
                        self._snow_amount_views,
                        swe_views=self._snow_views,
                        latitude=latitude,
                        longitude=longitude,
                        valid_time=_iso(valid_time),
                        source_status=self._snow_amount_guidance.get("source_status", {}),
                    )
                    hours[-1]["surface"]["fields"][AMOUNT] = amounts["field"]
                    hours[-1]["surface"]["snowfall_amount_guidance"] = amounts
                cloud = extract_cloud_contributors(
                    self._cloud_views,
                    latitude=latitude,
                    longitude=longitude,
                    valid_time=_iso(valid_time),
                    source_status=(self._cloud_guidance or {}).get("source_status", {}),
                )
                if not native:
                    hours[-1]["surface"]["fields"]["cloud_area_fraction"] = cloud["field"]
                if self._cloud_guidance is not None:
                    hours[-1]["surface"]["cloud_guidance"] = cloud
                if self._visibility_guidance is not None:
                    visibility = extract_visibility_contributors(
                        self._visibility_views,
                        latitude=latitude,
                        longitude=longitude,
                        valid_time=_iso(valid_time),
                        source_status=self._visibility_guidance.get("source_status", {}),
                    )
                    hours[-1]["surface"]["fields"]["visibility"] = visibility["field"]
                    hours[-1]["surface"]["visibility_guidance"] = visibility
                if self._thunder_guidance is not None:
                    thunder = extract_thunder_contributors(
                        self._thunder_views,
                        latitude=latitude,
                        longitude=longitude,
                        valid_time=_iso(valid_time),
                        source_status=self._thunder_guidance.get("source_status", {}),
                    )
                    hours[-1]["surface"]["fields"][THUNDER] = thunder["field"]
                    hours[-1]["surface"]["thunder_guidance"] = thunder
                if self._ice_guidance is not None:
                    ice = extract_ice_contributors(
                        self._ice_views,
                        latitude=latitude,
                        longitude=longitude,
                        valid_time=_iso(valid_time),
                        source_status=self._ice_guidance.get("source_status", {}),
                    )
                    hours[-1]["surface"]["fields"].update(ice["fields"])
                    hours[-1]["surface"]["ice_guidance"] = ice
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
        if native:
            result["forecast_horizon"] = horizon_for(self._manifest or {}).payload()
            result["field_policy_family"] = PROVISIONAL_MULTIMODEL_POLICY
            result["qpf_intervals"] = self._qpf_events(
                surface_datasets,
                hours,
                engine,
                latitude=latitude,
                longitude=longitude,
            )
        if self._prepared_reference_time is not None:
            # A reference view: the guidance was prepared for an earlier window and is
            # read by absolute valid time; the request hour never rewrites that fact.
            result["prepared_window"] = {
                "prepared_reference_time": _iso(self._prepared_reference_time),
                "prepared_horizon_hours": list(self._prepared_horizons or ()),
                "reference_offset_hours": int(
                    (self._target_reference_time - self._prepared_reference_time)
                    / np.timedelta64(1, "h")
                ),
            }
        if self._probability_views:
            result["probability_sources"] = [
                {
                    key: deepcopy(view.manifest.get(key))
                    for key in (
                        "source_id",
                        "source_metadata",
                        "manifest_sha256",
                        "prepared_file",
                        "created_at",
                        "code_identity",
                        "events",
                    )
                }
                for view in self._probability_views
            ]
        if self._type_guidance is not None:
            result["ptype_guidance"] = deepcopy(self._type_guidance)
        if self._snow_guidance is not None:
            result["snowfall_guidance"] = deepcopy(self._snow_guidance)
        if self._snow_amount_guidance is not None:
            result["snowfall_amount_guidance"] = deepcopy(self._snow_amount_guidance)
        if self._cloud_guidance is not None:
            result["cloud_guidance"] = deepcopy(self._cloud_guidance)
        if self._visibility_guidance is not None:
            result["visibility_guidance"] = deepcopy(self._visibility_guidance)
        if self._thunder_guidance is not None:
            result["thunder_guidance"] = deepcopy(self._thunder_guidance)
        if self._ice_guidance is not None:
            result["ice_guidance"] = deepcopy(self._ice_guidance)
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

    def _probability_event(
        self,
        engine: FieldBlendEngine,
        contributors: list[dict[str, Any]],
        *,
        valid_time: np.datetime64,
        horizon: int,
    ) -> dict[str, Any]:
        """Deliver a native six-hour event only at its endpoint, never as hourly PoP."""
        end = datetime.fromisoformat(_iso(valid_time))
        if horizon < 6 or end.hour % 6:
            return {
                "value": None,
                "unit": "1",
                "valid_time": _iso(valid_time),
                "status": "not_applicable",
                "temporal_semantics": "probability",
                "policy": provisional_policy(POP6).policy_id,
                "policy_family": PROVISIONAL_MULTIMODEL_POLICY,
                "missing_reasons": ["No complete native six-hour event ends at this forecast hour"],
                "weights": {},
            }
        native = {}
        contexts = {}
        for model, identity in (("NBM", "NBM_6H"), ("GEFS", "GEFS_6H")):
            rows = [
                row
                for row in contributors
                if row.get("source_id") == identity and row.get("event_id") is not None
            ]
            if len(rows) != 1:
                continue  # Absent/ambiguous evidence cannot supply a probability.
            native[model] = rows[0]
            contexts[model] = {
                "cycle": rows[0]["source_cycle"],
                "reference_time": _iso(self._target_reference_time),
                "source_lead_hours": rows[0]["source_lead_hours"],
            }
        return engine.blend_field(
            POP6,
            BlendState(
                horizon=horizon,
                contributors={},
                probabilities=native,
                source_context=contexts,
                probability_interval=(_iso(valid_time - np.timedelta64(6, "h")), _iso(valid_time)),
            ),
        )

    def _qpf_events(
        self,
        datasets: dict[str, tuple[xr.Dataset, pyproj.CRS, dict[str, Any] | None]],
        hours: list[dict[str, Any]],
        engine: FieldBlendEngine,
        *,
        latitude: float,
        longitude: float,
    ) -> list[dict[str, Any]]:
        """One canonical partition: exact hourly events, then coarser native totals.

        Coarse events have no hourly allocation. Their covered hourly field slots
        are explicitly unavailable and cannot be edited as hourly amounts.
        """
        field = "liquid_equivalent_precipitation_amount_1h"
        rows = []
        for start, end in qpf_partition(
            datasets, reference=self._target_reference_time, duration_hours=len(hours)
        ):
            lead = int((end - self._target_reference_time) / np.timedelta64(1, "h"))
            if end - start == np.timedelta64(1, "h"):
                item = deepcopy(hours[lead - 1]["surface"]["fields"][field])
                item["contributors"] = {
                    model: deepcopy(source["fields"][field])
                    for model, source in hours[lead - 1]["surface"]["contributors"].items()
                    if field in source["fields"]
                }
            else:
                native = {
                    model: extract_native_qpf(
                        model, entry, start=start, end=end, latitude=latitude, longitude=longitude
                    )
                    for model, entry in datasets.items()
                }
                contexts = {
                    model: {
                        "cycle": _iso(entry[0].forecast_reference_time.values[()]),
                        "reference_time": _iso(self._target_reference_time),
                        "source_lead_hours": int(
                            (end - entry[0].forecast_reference_time.values[()])
                            / np.timedelta64(1, "h")
                        ),
                    }
                    for model, entry in datasets.items()
                }
                state = BlendState(
                    horizon=lead,
                    contributors={},
                    precipitation=native,
                    source_context=contexts,
                    precipitation_interval=(_iso(start), _iso(end)),
                )
                item = {**engine.blend_field(field, state), "contributors": native}
                first = int((start - self._target_reference_time) / np.timedelta64(1, "h"))
                for hour in hours[first:lead]:
                    hour["surface"]["fields"][field].update(
                        value=None,
                        weights={},
                        status="unavailable",
                        missing_reasons=[
                            "Canonical QPF is a coarser exact accumulation; no hourly allocation"
                        ],
                        containing_interval={
                            "interval_start": _iso(start),
                            "interval_end": _iso(end),
                        },
                    )
            item.update(
                interval_start=_iso(start),
                interval_end=_iso(end),
                interval_closure="left_open_right_closed",
                temporal_semantics="accumulation",
            )
            rows.append(item)
        from mesoforge.common.qpf_intervals import validate_qpf_intervals

        validate_qpf_intervals(
            rows,
            start=datetime.fromisoformat(_iso(self._target_reference_time)),
            end=datetime.fromisoformat(
                _iso(self._target_reference_time + np.timedelta64(len(hours), "h"))
            ),
        )
        return rows
