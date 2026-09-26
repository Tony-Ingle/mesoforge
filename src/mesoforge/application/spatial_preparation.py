"""Ensure coordinate-derived prepared coverage before calculation or HTTP startup."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.application.prepared_qpf import qpf_raw_bytes
from mesoforge.application.prepared_temperature import _raw_byte_count, rebuild_temperature_guidance
from mesoforge.application.spatial_coverage import (
    CONTEXT_KM,
    MODEL_BUFFER_KM,
    CoverageRequiredError,
    UnsupportedCoordinateError,
    footprint,
    plan_regions,
    validate_coordinate,
)
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION, ContributorConfiguration
from mesoforge.guidance.runtime import SystemClock


def source_identity(manifest: dict[str, Any]) -> str:
    """A spatial view does not change the acquired guidance identity."""
    value = {
        key: manifest[key]
        for key in ("data_kind", "target_reference_time", "configuration_sha256", "inputs")
    }
    value["target_horizon_hours"] = manifest.get("target_horizon_hours", [1, 2, 3])
    if manifest.get("qpf_fields"):
        value["qpf_fields"] = True
        value["qpf_inputs"] = manifest.get("qpf_inputs", [])
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass
class PreparedRegions:
    regions: list[PreparedPointForecast]
    failures: dict[tuple[float, float], str]

    @property
    def data_kind(self) -> str:
        return self.regions[0].data_kind

    @property
    def notice(self) -> str:
        return self.regions[0].notice

    @property
    def horizon_hours(self) -> tuple[int, ...]:
        return self.regions[0].horizon_hours

    @property
    def prepared_reference_time(self) -> np.datetime64:
        return self.regions[0].prepared_reference_time

    def prepared_valid_times(self) -> dict[str, list[np.datetime64]]:
        """Valid times common to every region; regions share one source window."""
        held = [region.prepared_valid_times() for region in self.regions]
        return {
            model: [
                value
                for value in held[0][model]
                if all(value in set(other.get(model, [])) for other in held[1:])
            ]
            for model in held[0]
        }

    def reference_view(self, reference_time: datetime | np.datetime64) -> PreparedRegions:
        return replace(
            self, regions=[region.reference_view(reference_time) for region in self.regions]
        )

    def with_policy_overrides(self, overrides: dict[str, Any]) -> PreparedRegions:
        return replace(
            self, regions=[region.with_policy_overrides(overrides) for region in self.regions]
        )

    def _region_for(self, latitude: float, longitude: float) -> PreparedPointForecast:
        validate_coordinate(latitude, longitude)
        if (latitude, longitude) in self.failures:
            raise UnsupportedCoordinateError(self.failures[latitude, longitude])
        unsupported = []
        for region in self.regions:
            try:
                region.check_coordinate(latitude, longitude)
            except CoverageRequiredError:
                continue
            except UnsupportedCoordinateError as exc:
                unsupported.append(exc)
                continue
            return region
        if len(unsupported) == len(self.regions):
            raise unsupported[0]
        raise CoverageRequiredError(
            "Coverage is not prepared; run coordinate preparation before HTTP"
        )

    def forecast(self, *, latitude: float, longitude: float) -> dict[str, Any]:
        return self._region_for(latitude, longitude).forecast(
            latitude=latitude, longitude=longitude
        )

    def point_column(self, *, latitude: float, longitude: float) -> dict[str, Any]:
        return self._region_for(latitude, longitude).point_column(
            latitude=latitude, longitude=longitude
        )


def load_prepared(
    directory: Path,
    *,
    configuration: ContributorConfiguration = DEFAULT_CONFIGURATION,
    shadow_directories: Mapping[str, Path] | None = None,
    pop_guidance: dict[str, Any] | None = None,
    probability_sources: list[dict[str, Any]] | None = None,
    ptype_guidance: dict[str, Any] | None = None,
    snowfall_guidance: dict[str, Any] | None = None,
    snowfall_amount_guidance: dict[str, Any] | None = None,
    cloud_guidance: dict[str, Any] | None = None,
    visibility_guidance: dict[str, Any] | None = None,
    thunder_guidance: dict[str, Any] | None = None,
    ice_guidance: dict[str, Any] | None = None,
) -> PreparedPointForecast | PreparedRegions:
    """Load all shared regions once. This function never prepares or downloads."""
    index = directory / "coverage.json"
    if not index.is_file():
        prepared = attach_snowfall_guidance(
            attach_type_guidance(
                attach_pop_guidance(
                    PreparedPointForecast.from_directory(
                        directory,
                        configuration=configuration,
                        shadow_directories=shadow_directories,
                    ),
                    pop_guidance,
                    probability_sources=probability_sources,
                ),
                ptype_guidance,
            ),
            snowfall_guidance,
        )
        prepared = attach_visibility_guidance(
            attach_cloud_guidance(
                attach_snowfall_amount_guidance(prepared, snowfall_amount_guidance), cloud_guidance
            ),
            visibility_guidance,
        )
        return attach_ice_guidance(
            attach_thunder_guidance(prepared, thunder_guidance), ice_guidance
        )
    payload = json.loads(index.read_text())
    regions = []
    for row in {item["directory"]: item for item in payload["regions"]}.values():
        region = PreparedPointForecast.from_directory(
            Path(row["directory"]),
            configuration=configuration,
            shadow_directories=shadow_directories,
        )
        if (
            region._manifest is None
            or source_identity(region._manifest) != payload["source_identity"]
        ):
            raise ValueError(
                "Coverage regions must use the same source guidance and valid-time window"
            )
        regions.append(region)
    if not regions:
        raise ValueError("No prepared regions in coverage index")
    failures = {(row["lat"], row["lon"]): row["message"] for row in payload.get("failures", [])}
    prepared = attach_snowfall_guidance(
        attach_type_guidance(
            attach_pop_guidance(
                PreparedRegions(regions, failures),
                pop_guidance,
                probability_sources=probability_sources,
            ),
            ptype_guidance,
        ),
        snowfall_guidance,
    )
    prepared = attach_visibility_guidance(
        attach_cloud_guidance(
            attach_snowfall_amount_guidance(prepared, snowfall_amount_guidance), cloud_guidance
        ),
        visibility_guidance,
    )
    return attach_ice_guidance(attach_thunder_guidance(prepared, thunder_guidance), ice_guidance)


def attach_ice_guidance(
    prepared: PreparedPointForecast | PreparedRegions,
    descriptor: dict[str, Any] | None,
) -> PreparedPointForecast | PreparedRegions:
    """Load separate native ice/liquid accumulations once for every region and cell."""
    if descriptor is None:
        return prepared
    from mesoforge.application.prepared_ice import load_ice_guidance

    regions = prepared.regions if isinstance(prepared, PreparedRegions) else [prepared]
    target = regions[0]._target_reference_time
    if any(
        region._surface_configuration is None or region._target_reference_time != target
        for region in regions
    ):
        raise ValueError("Ice guidance requires one shared surface forecast target")
    views = load_ice_guidance(descriptor, target_reference_time=target)
    attached = [
        replace(region, _ice_views=views, _ice_guidance=deepcopy(descriptor)) for region in regions
    ]
    return (
        PreparedRegions(attached, prepared.failures)
        if isinstance(prepared, PreparedRegions)
        else attached[0]
    )


def attach_thunder_guidance(
    prepared: PreparedPointForecast | PreparedRegions,
    descriptor: dict[str, Any] | None,
) -> PreparedPointForecast | PreparedRegions:
    """Load native thunder events once and reuse them across regions and cells."""
    if descriptor is None:
        return prepared
    from mesoforge.application.prepared_thunder import load_thunder_guidance

    regions = prepared.regions if isinstance(prepared, PreparedRegions) else [prepared]
    target = regions[0]._target_reference_time
    if any(
        region._surface_configuration is None or region._target_reference_time != target
        for region in regions
    ):
        raise ValueError("Thunder guidance requires one shared surface forecast target")
    views = load_thunder_guidance(descriptor, target_reference_time=target)
    attached = [
        replace(region, _thunder_views=views, _thunder_guidance=deepcopy(descriptor))
        for region in regions
    ]
    return (
        PreparedRegions(attached, prepared.failures)
        if isinstance(prepared, PreparedRegions)
        else attached[0]
    )


def attach_visibility_guidance(
    prepared: PreparedPointForecast | PreparedRegions,
    descriptor: dict[str, Any] | None,
) -> PreparedPointForecast | PreparedRegions:
    """Load native visibility once and share the evidence across regions and cells."""
    if descriptor is None:
        return prepared
    from mesoforge.application.prepared_visibility import load_visibility_guidance

    regions = prepared.regions if isinstance(prepared, PreparedRegions) else [prepared]
    target = regions[0]._target_reference_time
    if any(
        region._surface_configuration is None or region._target_reference_time != target
        for region in regions
    ):
        raise ValueError("Visibility guidance requires one shared surface forecast target")
    views = load_visibility_guidance(descriptor, target_reference_time=target)
    attached = [
        replace(region, _visibility_views=views, _visibility_guidance=deepcopy(descriptor))
        for region in regions
    ]
    return (
        PreparedRegions(attached, prepared.failures)
        if isinstance(prepared, PreparedRegions)
        else attached[0]
    )


def attach_cloud_guidance(
    prepared: PreparedPointForecast | PreparedRegions,
    descriptor: dict[str, Any] | None,
) -> PreparedPointForecast | PreparedRegions:
    """Load native cloud evidence once and share it across regions and grid cells."""
    if descriptor is None:
        return prepared
    from mesoforge.application.prepared_cloud import load_cloud_guidance

    regions = prepared.regions if isinstance(prepared, PreparedRegions) else [prepared]
    target = regions[0]._target_reference_time
    if any(
        region._surface_configuration is None or region._target_reference_time != target
        for region in regions
    ):
        raise ValueError("Cloud guidance requires one shared surface forecast target")
    views = load_cloud_guidance(descriptor, target_reference_time=target)
    attached = [
        replace(region, _cloud_views=views, _cloud_guidance=deepcopy(descriptor))
        for region in regions
    ]
    return (
        PreparedRegions(attached, prepared.failures)
        if isinstance(prepared, PreparedRegions)
        else attached[0]
    )


def attach_snowfall_amount_guidance(
    prepared: PreparedPointForecast | PreparedRegions,
    descriptor: dict[str, Any] | None,
) -> PreparedPointForecast | PreparedRegions:
    """Load native amount/ratio/profile views once beside the existing SWE attachment."""
    if descriptor is None:
        return prepared
    from mesoforge.application.prepared_snowfall_amount import load_snowfall_amount_guidance

    regions = prepared.regions if isinstance(prepared, PreparedRegions) else [prepared]
    target = regions[0]._target_reference_time
    if any(
        region._surface_configuration is None or region._target_reference_time != target
        for region in regions
    ):
        raise ValueError("Snowfall amounts require one shared surface forecast target")
    views = load_snowfall_amount_guidance(descriptor, target_reference_time=target)
    attached = [
        replace(region, _snow_amount_views=views, _snow_amount_guidance=deepcopy(descriptor))
        for region in regions
    ]
    return (
        PreparedRegions(attached, prepared.failures)
        if isinstance(prepared, PreparedRegions)
        else attached[0]
    )


def attach_snowfall_guidance(
    prepared: PreparedPointForecast | PreparedRegions,
    descriptor: dict[str, Any] | None,
) -> PreparedPointForecast | PreparedRegions:
    """Load native accumulation views once, shared by every coordinate/grid cell."""
    if descriptor is None:
        return prepared
    from mesoforge.application.prepared_snowfall import load_snowfall_guidance

    regions = prepared.regions if isinstance(prepared, PreparedRegions) else [prepared]
    target = regions[0]._target_reference_time
    if any(
        region._surface_configuration is None or region._target_reference_time != target
        for region in regions
    ):
        raise ValueError("Snowfall guidance requires one shared surface forecast target")
    views = load_snowfall_guidance(descriptor, target_reference_time=target)
    attached = [
        replace(region, _snow_views=views, _snow_guidance=deepcopy(descriptor))
        for region in regions
    ]
    return (
        PreparedRegions(attached, prepared.failures)
        if isinstance(prepared, PreparedRegions)
        else attached[0]
    )


def attach_type_guidance(
    prepared: PreparedPointForecast | PreparedRegions,
    descriptor: dict[str, Any] | None,
) -> PreparedPointForecast | PreparedRegions:
    """Load shared native categorical views once, before grid construction."""
    if descriptor is None:
        return prepared
    from mesoforge.application.prepared_precipitation_type import load_type_guidance

    regions = prepared.regions if isinstance(prepared, PreparedRegions) else [prepared]
    target = regions[0]._target_reference_time
    if any(
        region._surface_configuration is None or region._target_reference_time != target
        for region in regions
    ):
        raise ValueError("P-type guidance requires one shared surface forecast target")
    views = load_type_guidance(descriptor, target_reference_time=target)
    attached = [
        replace(region, _type_views=views, _type_guidance=deepcopy(descriptor))
        for region in regions
    ]
    return (
        PreparedRegions(attached, prepared.failures)
        if isinstance(prepared, PreparedRegions)
        else attached[0]
    )


def attach_pop_guidance(
    prepared: PreparedPointForecast | PreparedRegions,
    descriptor: dict[str, Any] | None,
    *,
    probability_sources: list[dict[str, Any]] | None = None,
) -> PreparedPointForecast | PreparedRegions:
    """Load one shared field source before forecasts; never acquire from a grid node."""
    if descriptor is None:
        if probability_sources:
            raise ValueError("Probability shadows require the active NBM PoP attachment")
        return prepared
    from mesoforge.application.prepared_pop import load_pop_guidance

    regions = prepared.regions if isinstance(prepared, PreparedRegions) else [prepared]
    if any(region._surface_configuration is None for region in regions):
        raise ValueError("Probability guidance requires the existing surface-grid forecast")
    target = regions[0]._target_reference_time
    if any(region._target_reference_time != target for region in regions):
        raise ValueError("Probability attachment requires one shared target reference time")
    views = load_pop_guidance(descriptor, target_reference_time=target)
    from mesoforge.application.prepared_probability_sources import load_probability_sources

    probability_views = load_probability_sources(
        probability_sources or [], target_reference_time=target
    )
    attached = [
        replace(
            region,
            _pop_views=views,
            _pop_guidance=deepcopy(descriptor),
            _probability_views=probability_views,
        )
        for region in regions
    ]
    return (
        PreparedRegions(attached, prepared.failures)
        if isinstance(prepared, PreparedRegions)
        else attached[0]
    )


def ensure_coverage(
    locations: list[Any],
    source_directory: Path,
    *,
    cache_directory: Path | None = None,
    contributor_configuration: ContributorConfiguration = DEFAULT_CONFIGURATION,
    shadow_directories: Mapping[str, Path] | None = None,
) -> tuple[PreparedPointForecast | PreparedRegions, dict[str, Any]]:
    """Inspect the whole collection, reuse prepared views or rebuild shared views from raw.

    Model acquisition retains complete native messages, so expanding a spatial view
    never requires reacquiring the same cycle. Fresh cycles use the existing acquisition
    command first. Missing/corrupt retained evidence is an explicit failure, not permission
    to silently substitute another cycle.
    """
    from mesoforge.application.batch_forecast import _coordinates

    index = source_directory / "coverage.json"
    if index.is_file():
        cache_directory = cache_directory or source_directory
        source_directory = Path(json.loads(index.read_text())["source_directory"])
    source_directory = source_directory.resolve()
    prepared = PreparedPointForecast.from_directory(
        source_directory,
        configuration=contributor_configuration,
        shadow_directories=shadow_directories,
    )
    if prepared.data_kind != "real_prepared_guidance":
        return prepared, {"regions": [], "downloaded_bytes": 0, "mode": "synthetic_fixture"}
    assert prepared._manifest is not None
    identity = source_identity(prepared._manifest)
    cache = cache_directory or source_directory.with_name(source_directory.name + "-coverage")
    coordinates = []
    for location in locations:
        try:
            lat, lon = _coordinates(location)
            validate_coordinate(lat, lon)
        except ValueError:
            continue  # Existing batch handling reports this location's input error.
        coordinates.append((lat, lon))
    areas = plan_regions(coordinates)
    candidates = [(source_directory, prepared)]
    if cache.exists():
        for path in sorted(cache.glob("*/manifest.json")):
            if path.parent.resolve() == source_directory:
                continue
            manifest = json.loads(path.read_text())
            if source_identity(manifest) == identity:
                candidates.append(
                    (
                        path.parent,
                        PreparedPointForecast.from_directory(
                            path.parent,
                            configuration=contributor_configuration,
                            shadow_directories=shadow_directories,
                        ),
                    )
                )
    reports = []
    selected: list[PreparedPointForecast] = []
    failures: dict[tuple[float, float], str] = {}
    for area in areas:
        reused = next(((path, item) for path, item in candidates if item.covers_area(area)), None)
        status = "reused"
        if reused is None:
            key = hashlib.sha256((identity + area.model_dump_json()).encode()).hexdigest()[:24]
            destination = cache / key
            failure_file = destination / "failure.json"
            if failure_file.exists():
                message = json.loads(failure_file.read_text())["message"]
                for lat, lon in coordinates:
                    if area.south <= lat <= area.north and area.west <= lon <= area.east:
                        failures[lat, lon] = message
                continue
            if destination.exists():
                raise ValueError(
                    f"Incomplete or insufficient cached region is retained at {destination}"
                )
            configuration, _ = load_configuration_source(
                base_path=Path("configs/base.yaml"),
                additional_overlay_paths=(
                    Path("configs/phase1-grasston.yaml"),
                    Path("configs/phase2-grasston.yaml"),
                ),
            )
            assert configuration.phase2 is not None
            try:
                rebuild_temperature_guidance(
                    source_directory,
                    destination,
                    configuration=configuration.phase2,
                    clock=SystemClock(),
                    area=area,
                )
                item = PreparedPointForecast.from_directory(
                    destination,
                    configuration=contributor_configuration,
                    shadow_directories=shadow_directories,
                )
            except UnsupportedCoordinateError as exc:
                failure_file.write_text(json.dumps({"message": str(exc)}))
                # Other geographic groups still get prepared. The native-domain failure
                # is reported separately from insufficient prepared coverage.
                for lat, lon in coordinates:
                    if area.south <= lat <= area.north and area.west <= lon <= area.east:
                        failures[lat, lon] = str(exc)
                continue
            if not item.covers_area(area):
                raise CoverageRequiredError(
                    "Rebuilt guidance does not cover the requested footprint"
                )
            reused = destination, item
            candidates.append(reused)
            status = "prepared_from_retained_raw"
        path, item = reused
        if not any(existing is item for existing in selected):
            selected.append(item)
        reports.append(
            {
                "area": area.model_dump(),
                "directory": str(path),
                "status": status,
                "prepared_bytes": sum(
                    (path / f"{model}.nc").stat().st_size for model in ("HRRR", "GFS")
                ),
                "manifest_sha256": item._manifest_sha256,
            }
        )
    # Keep native metadata available for an invalid/out-of-domain-only collection.
    if not selected:
        selected = [prepared]
        reports = [{"directory": str(source_directory), "status": "reused", "area": None}]
    report = {
        "source_directory": str(source_directory),
        "source_identity": identity,
        "model_buffer_km": MODEL_BUFFER_KM,
        "context_km": CONTEXT_KM,
        "observation_search_km": 50,
        "downloaded_bytes": 0,
        "retained_raw_bytes": _raw_byte_count(prepared._manifest["inputs"])
        + qpf_raw_bytes(prepared._manifest.get("qpf_inputs", [])),
        "retained_index_bytes": sum(row["index_bytes"] for row in prepared._manifest["inputs"]),
        "footprints": [
            {
                "lat": lat,
                "lon": lon,
                "model_buffer": footprint(lat, lon, MODEL_BUFFER_KM).model_dump(),
                "context": footprint(lat, lon, CONTEXT_KM).model_dump(),
            }
            for lat, lon in coordinates
        ],
        "regions": reports,
        "failures": [
            {"lat": lat, "lon": lon, "message": message} for (lat, lon), message in failures.items()
        ],
    }
    cache.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2)
    output = cache / "coverage.json"
    if not output.exists() or output.read_text() != payload:
        output.write_text(payload)
    return PreparedRegions(selected, failures), report
