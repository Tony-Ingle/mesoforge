"""Production Phase1SourcePort/Phase1TransformationPort adapters (plan
Section 4.3/5, Task 12; Codex review t_09a43c6c finding 4).

Composes the pure guidance/observations/forecasting/alignment/
verification domain functions with real HRRR and AviationWeather
network acquisition (``guidance.acquisition``/``observations.acquisition``)
and ``ArtifactService`` registration/transformation. This is the only
concrete, non-test-local implementation of the two ``application.phase1``
ports: production wiring (a CLI/scheduler entrypoint) and the acceptance
proof both use these classes, injecting a real ``RequestsHrrrHttpTransport``
/``RequestsAviationWeatherHttpTransport`` in production or a deterministic
scripted fixture transport in tests -- never a parallel test-only port
implementation.

Kept out of ``application/phase1.py`` itself: the import-linter contract
``phase1-application-no-scientific-logic`` forbids that module from
importing a provider adapter (``guidance.sources.hrrr`` /
``observations.sources.aviationweather``) directly, so the composition
root lives here instead, one layer below the pure coordinator.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pyarrow as pa
import xarray as xr

from mesoforge.alignment.spatial import bilinear_interpolate, project_station_point
from mesoforge.application.artifacts import (
    ArtifactService,
    InputBinding,
    SourceRegistrationRequest,
    TransformationInputRef,
    TransformationRequest,
)
from mesoforge.application.phase1 import (
    ForecastArtifacts,
    HrrrLeadArtifacts,
    ObservationArtifacts,
    Phase1Request,
    StationCatalogArtifacts,
    VerificationArtifacts,
)
from mesoforge.catalog.configuration import Phase1Configuration
from mesoforge.catalog.stations import StationCatalogSnapshot, normalize_station_catalog
from mesoforge.common.identifiers import ArtifactId
from mesoforge.contracts.artifacts import Availability
from mesoforge.contracts.observations import NormalizedObservation
from mesoforge.contracts.runs import RunManifest
from mesoforge.contracts.verification import MatchedPairRow, VerificationReport
from mesoforge.forecasting.baseline import assemble_baseline_forecast
from mesoforge.guidance.acquisition import (
    HrrrLeadAcquisition,
    acquire_hrrr_lead,
    build_acquisition_manifest_payload,
)
from mesoforge.guidance.decoding import (
    GridSignature,
    assert_consistent_grid_signatures,
    decode_selected_messages,
)
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.normalization import (
    assemble_canonical_hrrr_dataset,
    build_lambert_conformal_crs,
    compute_bbox_halo_subset_indices,
    compute_latlon_grid,
    compute_projected_coordinates,
    rotate_wind_to_earth_relative,
)
from mesoforge.guidance.validation import validate_phase1_hrrr_guidance
from mesoforge.observations.acquisition import (
    RequestRateLimiter,
    acquire_metar_batch,
    acquire_stationinfo,
)
from mesoforge.observations.normalization import normalize_metar_record
from mesoforge.observations.sources.aviationweather import parse_raw_metar_response
from mesoforge.observations.tables import build_observations_table
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.netcdf import H5NetcdfDatasetSerializer
from mesoforge.storage.parquet import ParquetTableSerializer
from mesoforge.verification.matching import match_baseline_to_observations
from mesoforge.verification.metrics import compute_verification_report
from mesoforge.verification.tables import build_matched_pairs_table

_LEADS = tuple(range(7))
_HERBIE_VERSION = "2026.3.0"
_CFGRIB_VERSION = "0.9.15.1"
_ECCODES_VERSION = "2.48.0"
_XARRAY_VERSION = xr.__version__
_NUMPY_VERSION = np.__version__
_PYPROJ_VERSION = "3.7.2"


def _valid_bytes(value: object) -> None:
    if not isinstance(value, bytes):
        raise TypeError(f"expected bytes, got {type(value)!r}")


def _valid_map(value: object) -> None:
    if not isinstance(value, dict):
        raise TypeError(f"expected dict, got {type(value)!r}")


def _valid_table(value: object) -> None:
    if not isinstance(value, pa.Table):
        raise TypeError(f"expected pyarrow.Table, got {type(value)!r}")


def _valid_dataset(value: object) -> None:
    if not isinstance(value, xr.Dataset):
        raise TypeError(f"expected xarray.Dataset, got {type(value)!r}")


def _observations_from_table(table: pa.Table) -> list[NormalizedObservation]:
    out = []
    for row in table.to_pylist():
        row["quality_flags"] = tuple(row["quality_flags"])
        out.append(NormalizedObservation.model_validate(row, strict=True))
    return out


def _matched_pairs_from_table(table: pa.Table) -> list[MatchedPairRow]:
    return [MatchedPairRow.model_validate(row, strict=True) for row in table.to_pylist()]


def _station_snapshot_from_json(value: dict[str, object]) -> StationCatalogSnapshot:
    return StationCatalogSnapshot.model_validate_json(json.dumps(value))


def _verification_report_from_json(value: dict[str, object]) -> VerificationReport:
    return VerificationReport.model_validate_json(json.dumps(value))


class Phase1ProductionAdapters:
    """Single object implementing both ``Phase1SourcePort`` and
    ``Phase1TransformationPort`` with real HRRR/AviationWeather
    acquisition and the production scientific transformations.

    Holds transient per-run HRRR acquisition metadata (byte-range
    request attempts/resolved URLs) between ``register_hrrr_sources``
    and ``build_forecast`` -- the acquisition manifest artifact records
    genuine network provenance, not merely a recomputation over already
    -registered bytes, so this metadata cannot be re-derived from the
    registered artifacts alone and must be carried across the two calls
    the ``Phase1Coordinator`` makes on this instance for one run.
    """

    def __init__(
        self,
        *,
        configuration: Phase1Configuration,
        hrrr_transport: HttpTransport,
        aviationweather_transport: HttpTransport,
        aviationweather_rate_limiter: RequestRateLimiter | None = None,
    ) -> None:
        self._configuration = configuration
        self._hrrr_transport = hrrr_transport
        self._aviationweather_transport = aviationweather_transport
        # Residual review finding 3: production composition must never
        # silently run unthrottled. When the caller does not explicitly
        # inject a (typically test-scripted) limiter, a real one is
        # always constructed here from the configured
        # ``min_request_interval_seconds`` and shared across every
        # stationinfo/METAR acquisition call this instance makes for
        # the life of the process -- never a bare ``None`` default that
        # lets ``acquire_stationinfo``/``acquire_metar_batch`` skip
        # waiting entirely (runtime probe
        # ``DEFAULT_LIMITER_SLEEPS [] CALLS 2``).
        self._aviationweather_rate_limiter = (
            aviationweather_rate_limiter
            if aviationweather_rate_limiter is not None
            else RequestRateLimiter(
                min_interval_seconds=configuration.aviationweather.min_request_interval_seconds
            )
        )
        self._json = CanonicalJsonSerializer()
        self._netcdf = H5NetcdfDatasetSerializer()
        self._parquet = ParquetTableSerializer()
        self._hrrr_acquisitions_by_run: dict[str, tuple[HrrrLeadAcquisition, ...]] = {}

    # ------------------------------------------------------------------
    # Phase1SourcePort
    # ------------------------------------------------------------------

    def register_station_catalog(
        self,
        request: Phase1Request,
        *,
        artifact_service: ArtifactService,
        clock: Clock,
        sleeper: Sleeper,
    ) -> StationCatalogArtifacts:
        settings = self._configuration.aviationweather
        station_ids = tuple(s.provider_icao_id for s in self._configuration.stations)
        fetched = acquire_stationinfo(
            settings,
            transport=self._aviationweather_transport,
            clock=clock,
            sleeper=sleeper,
            station_ids=station_ids,
            rate_limiter=self._aviationweather_rate_limiter,
        )
        response = artifact_service.register_source(
            self._source_request(
                request,
                source_authority="aviationweather.gov",
                source_locator="stationinfo",
                artifact_type="aviationweather-station-response",
                artifact_schema_version="aviationweather-station-response.v1",
                media_type="application/json",
                created_at=clock.now(),
                available_at=fetched.completed_at,
            ),
            fetched.payload,
        )

        def _normalize(inputs: Mapping[str, Any]) -> dict[str, object]:
            raw_records = inputs["response"]
            assert isinstance(raw_records, list)
            snapshot = normalize_station_catalog(
                raw_records=raw_records,
                domain=self._configuration.domain,
                expected_stations=self._configuration.stations,
                source_artifact_id=response.artifact_id,
                effective_from=fetched.completed_at,
            )
            return snapshot.model_dump(mode="json")

        snapshot_manifest = artifact_service.execute_role_bound_transformation(
            self._transformation_request(
                request,
                activity_type="stations.normalize",
                inputs=(("response", response.artifact_id),),
                output_artifact_type="station-catalog-snapshot",
                output_media_type="application/json",
                run=False,
            ),
            _normalize,
            self._json,
            input_bindings={"response": InputBinding(json.loads, lambda x: None)},
            output_validator=_valid_map,
        ).output
        return StationCatalogArtifacts(response=response, snapshot=snapshot_manifest)

    def register_hrrr_sources(
        self,
        request: Phase1Request,
        *,
        artifact_service: ArtifactService,
        clock: Clock,
        sleeper: Sleeper,
    ) -> tuple[HrrrLeadArtifacts, ...]:
        settings = self._configuration.hrrr
        cycle: date = request.cycle.date()
        cycle_hour = request.cycle.hour
        cycle_deadline = request.cycle + timedelta(
            minutes=settings.cycle_availability_deadline_minutes
        )

        leads: list[HrrrLeadArtifacts] = []
        acquisitions: list[HrrrLeadAcquisition] = []
        for forecast_hour in settings.forecast_hours:
            acquisition = acquire_hrrr_lead(
                settings,
                transport=self._hrrr_transport,
                clock=clock,
                sleeper=sleeper,
                cycle_date=cycle,
                cycle_hour=cycle_hour,
                forecast_hour=forecast_hour,
                cycle_deadline=cycle_deadline,
            )
            index_artifact = artifact_service.register_source(
                self._source_request(
                    request,
                    source_authority="noaa.hrrr",
                    source_locator=acquisition.resolved_index_url,
                    artifact_type="hrrr-grib-index",
                    artifact_schema_version="hrrr-grib-index.v1",
                    media_type="text/plain",
                    created_at=clock.now(),
                    available_at=acquisition.index_completed_at,
                    source_revision=acquisition.endpoint,
                ),
                acquisition.index_payload,
            )
            grib_artifact = artifact_service.register_source(
                self._source_request(
                    request,
                    source_authority="noaa.hrrr",
                    source_locator=acquisition.resolved_grib_url,
                    artifact_type="hrrr-selected-grib",
                    artifact_schema_version="hrrr-selected-grib.v1",
                    media_type="application/octet-stream",
                    created_at=clock.now(),
                    available_at=acquisition.grib_completed_at,
                    source_revision=acquisition.endpoint,
                ),
                acquisition.selected_grib_payload,
            )
            leads.append(
                HrrrLeadArtifacts(
                    lead_hours=forecast_hour, index=index_artifact, selected_grib=grib_artifact
                )
            )
            acquisitions.append(acquisition)

        self._hrrr_acquisitions_by_run[str(request.run_id)] = tuple(acquisitions)
        return tuple(leads)

    def register_metar_observations(
        self,
        request: Phase1Request,
        station_catalog: StationCatalogArtifacts,
        *,
        artifact_service: ArtifactService,
        clock: Clock,
        sleeper: Sleeper,
    ) -> ObservationArtifacts:
        settings = self._configuration.aviationweather
        station_ids = tuple(s.provider_icao_id for s in self._configuration.stations)
        max_lead_hours = max(self._configuration.hrrr.forecast_hours)
        query_date = (
            request.cycle
            + timedelta(hours=max_lead_hours)
            + timedelta(minutes=settings.metar_completion_offset_minutes)
        )
        fetched = acquire_metar_batch(
            settings,
            transport=self._aviationweather_transport,
            clock=clock,
            sleeper=sleeper,
            station_ids=station_ids,
            query_date=query_date,
            rate_limiter=self._aviationweather_rate_limiter,
        )
        response = artifact_service.register_source(
            self._source_request(
                request,
                source_authority="aviationweather.gov",
                source_locator="metar",
                artifact_type="aviationweather-metar-response",
                artifact_schema_version="aviationweather-metar-response.v1",
                media_type="application/json",
                created_at=clock.now(),
                available_at=fetched.completed_at,
            ),
            fetched.payload,
        )

        def _normalize(inputs: Mapping[str, Any]) -> pa.Table:
            snapshot = _station_snapshot_from_json(inputs["stations"])
            by_icao = {s.provider_icao_id: s for s in snapshot.stations}
            raw_records = parse_raw_metar_response(inputs["response"])
            normalized: list[NormalizedObservation] = []
            for index, raw_record in enumerate(raw_records):
                station = by_icao[raw_record.icao_id]
                normalized.append(
                    normalize_metar_record(
                        raw=raw_record,
                        station=station,
                        station_id=station.station_id,
                        policy=self._configuration.observation_normalization_policy,
                        raw_artifact_id=response.artifact_id,
                        raw_record_index=index,
                        station_snapshot_artifact_id=station_catalog.snapshot.artifact_id,
                        ingested_at=raw_record.receipt_time,
                        query_window_start=request.cycle,
                        query_window_end=query_date,
                    )
                )
            return build_observations_table(normalized)

        normalized_manifest = artifact_service.execute_role_bound_transformation(
            self._transformation_request(
                request,
                activity_type="metar.normalize",
                inputs=(
                    ("response", response.artifact_id),
                    ("stations", station_catalog.snapshot.artifact_id),
                ),
                output_artifact_type="normalized-metar-observations",
                output_media_type="application/x-parquet",
                run=False,
            ),
            _normalize,
            self._parquet,
            input_bindings={
                "response": InputBinding(lambda payload: payload, _valid_bytes),
                "stations": InputBinding(self._json.deserialize, _valid_map),
            },
            output_validator=_valid_table,
        ).output
        return ObservationArtifacts(responses=(response,), normalized=normalized_manifest)

    # ------------------------------------------------------------------
    # Phase1TransformationPort
    # ------------------------------------------------------------------

    def build_forecast(
        self,
        request: Phase1Request,
        run: RunManifest,
        station_catalog: StationCatalogArtifacts,
        hrrr: tuple[HrrrLeadArtifacts, ...],
        *,
        artifact_service: ArtifactService,
    ) -> ForecastArtifacts:
        settings = self._configuration.hrrr
        acquisitions = self._hrrr_acquisitions_by_run.get(str(request.run_id))
        if acquisitions is None:
            raise ValueError(
                f"no HRRR acquisition metadata retained for run {request.run_id!r}; "
                "register_hrrr_sources must run before build_forecast on the same "
                "Phase1ProductionAdapters instance"
            )

        roots = tuple((f"idx{lead.lead_hours}", lead.index.artifact_id) for lead in hrrr) + tuple(
            (f"grib{lead.lead_hours}", lead.selected_grib.artifact_id) for lead in hrrr
        )

        def _acquisition_manifest(_inputs: Mapping[str, Any]) -> dict[str, object]:
            return build_acquisition_manifest_payload(
                acquisitions,
                settings=settings,
                herbie_version=_HERBIE_VERSION,
                cfgrib_version=_CFGRIB_VERSION,
                eccodes_version=_ECCODES_VERSION,
                xarray_version=_XARRAY_VERSION,
                numpy_version=_NUMPY_VERSION,
                pyproj_version=_PYPROJ_VERSION,
            )

        acquisition_manifest = artifact_service.execute_role_bound_transformation(
            self._transformation_request(
                request,
                activity_type="hrrr.acquire",
                inputs=roots,
                output_artifact_type="hrrr-acquisition-manifest",
                output_media_type="application/json",
                run=True,
            ),
            _acquisition_manifest,
            self._json,
            input_bindings={
                role: InputBinding(lambda payload: payload, _valid_bytes) for role, _ in roots
            },
            output_validator=_valid_map,
        ).output

        def _lineage(_inputs: Mapping[str, Any]) -> dict[str, object]:
            return {
                "schema_version": "variable-lineage-manifest.v1",
                "variables": [a.canonical_variable_id for a in settings.field_assertions],
            }

        variable_lineage = artifact_service.execute_role_bound_transformation(
            self._transformation_request(
                request,
                activity_type="hrrr.lineage",
                inputs=(("acquisition", acquisition_manifest.artifact_id),),
                output_artifact_type="variable-lineage-manifest",
                output_media_type="application/json",
                run=True,
            ),
            _lineage,
            self._json,
            input_bindings={"acquisition": InputBinding(self._json.deserialize, _valid_map)},
            output_validator=_valid_map,
        ).output

        guidance_inputs = (("lineage", variable_lineage.artifact_id),) + tuple(
            (f"grib{lead.lead_hours}", lead.selected_grib.artifact_id) for lead in hrrr
        )
        guidance_bindings = {"lineage": InputBinding(self._json.deserialize, _valid_map)}
        guidance_bindings.update(
            {f"grib{h}": InputBinding(lambda payload: payload, _valid_bytes) for h in _LEADS}
        )

        cycle_date = request.cycle.date()
        cycle_hour = request.cycle.hour

        # Grid/projection parameters (LoV/LaD/Latin1/Latin2) are read
        # from the first decoded message's GRIB attrs -- never
        # hardcoded -- and persisted onto the assembled canonical
        # dataset's attrs so the extraction step below reconstructs the
        # identical CRS without re-decoding GRIB bytes.
        def _guidance(inputs: Mapping[str, Any]) -> xr.Dataset:
            temperatures = []
            eastward = []
            northward = []
            crs = None
            x = y = lat = lon = None
            grid_signatures: dict[str, GridSignature] = {}
            projection_params: dict[str, float] = {}
            for lead_hours in _LEADS:
                payload = inputs[f"grib{lead_hours}"]
                assert isinstance(payload, (bytes, bytearray))
                decoded = decode_selected_messages(
                    bytes(payload),
                    settings=settings,
                    cycle_date=cycle_date,
                    cycle_hour=cycle_hour,
                    forecast_hour=lead_hours,
                )
                temperature_field = decoded["air_temperature_2m"]
                u_field = decoded["eastward_wind_10m"]
                v_field = decoded["northward_wind_10m"]
                grid_signatures[f"lead{lead_hours}"] = GridSignature.from_data_array(
                    temperature_field
                )

                if crs is None:
                    projection_params = {
                        "lov_degrees": float(temperature_field.attrs["GRIB_LoVInDegrees"]),
                        "lad_degrees": float(temperature_field.attrs["GRIB_LaDInDegrees"]),
                        "latin1_degrees": float(temperature_field.attrs["GRIB_Latin1InDegrees"]),
                        "latin2_degrees": float(temperature_field.attrs["GRIB_Latin2InDegrees"]),
                    }
                    crs = build_lambert_conformal_crs(**projection_params)
                    x, y = compute_projected_coordinates(
                        crs,
                        first_lat_degrees=float(
                            temperature_field.attrs["GRIB_latitudeOfFirstGridPointInDegrees"]
                        ),
                        first_lon_degrees=float(
                            temperature_field.attrs["GRIB_longitudeOfFirstGridPointInDegrees"]
                        ),
                        dx_m=float(temperature_field.attrs["GRIB_DxInMetres"]),
                        dy_m=float(temperature_field.attrs["GRIB_DyInMetres"]),
                        nx=int(temperature_field.attrs["GRIB_Nx"]),
                        ny=int(temperature_field.attrs["GRIB_Ny"]),
                    )
                    lat, lon = compute_latlon_grid(crs, x=x, y=y)

                assert x is not None and y is not None
                rotated = rotate_wind_to_earth_relative(
                    u_grid=u_field.values,
                    v_grid=v_field.values,
                    x=x,
                    y=y,
                    crs=crs,
                    u_relative_to_grid=bool(u_field.attrs["GRIB_uvRelativeToGrid"]),
                    v_relative_to_grid=bool(v_field.attrs["GRIB_uvRelativeToGrid"]),
                )
                temperatures.append(temperature_field.values)
                eastward.append(rotated.eastward)
                northward.append(rotated.northward)

            assert_consistent_grid_signatures(grid_signatures)
            assert crs is not None and x is not None and y is not None
            assert lat is not None and lon is not None

            subset = compute_bbox_halo_subset_indices(
                x=x, y=y, lat=lat, lon=lon, bbox=self._configuration.domain.bbox
            )
            y_slice = slice(subset.y_start, subset.y_end)
            x_slice = slice(subset.x_start, subset.x_end)

            dataset = assemble_canonical_hrrr_dataset(
                forecast_reference_time=np.datetime64(request.cycle.replace(tzinfo=None), "ns"),
                lead_hours=_LEADS,
                x=x[x_slice],
                y=y[y_slice],
                lat=lat[y_slice, x_slice],
                lon=lon[y_slice, x_slice],
                temperature_k=np.stack([t[y_slice, x_slice] for t in temperatures]),
                eastward_wind_m_s=np.stack([e[y_slice, x_slice] for e in eastward]),
                northward_wind_m_s=np.stack([n[y_slice, x_slice] for n in northward]),
                grid_id="hrrr-conus-grasston-subset.v1",
                configuration_snapshot_id=str(request.configuration_snapshot_id),
                variable_lineage_manifest_id=str(variable_lineage.artifact_id),
            )
            dataset.attrs["projection_lov_degrees"] = projection_params["lov_degrees"]
            dataset.attrs["projection_lad_degrees"] = projection_params["lad_degrees"]
            dataset.attrs["projection_latin1_degrees"] = projection_params["latin1_degrees"]
            dataset.attrs["projection_latin2_degrees"] = projection_params["latin2_degrees"]
            return dataset

        canonical_guidance = artifact_service.execute_role_bound_transformation(
            self._transformation_request(
                request,
                activity_type="hrrr.decode-normalize-rotate",
                inputs=guidance_inputs,
                output_artifact_type="canonical-guidance",
                output_media_type="application/x-netcdf",
                run=True,
            ),
            _guidance,
            self._netcdf,
            input_bindings=guidance_bindings,
            output_validator=validate_phase1_hrrr_guidance,
        ).output

        def _extract(inputs: Mapping[str, Any]) -> dict[str, object]:
            dataset = inputs["guidance"]
            assert isinstance(dataset, xr.Dataset)
            snapshot = _station_snapshot_from_json(inputs["stations"])
            crs = build_lambert_conformal_crs(
                lov_degrees=float(dataset.attrs["projection_lov_degrees"]),
                lad_degrees=float(dataset.attrs["projection_lad_degrees"]),
                latin1_degrees=float(dataset.attrs["projection_latin1_degrees"]),
                latin2_degrees=float(dataset.attrs["projection_latin2_degrees"]),
            )
            rows = []
            for station in snapshot.stations:
                station_x, station_y = project_station_point(
                    crs, latitude=station.latitude, longitude=station.longitude
                )
                for lead_hours in _LEADS:
                    for variable_id in (
                        "air_temperature_2m",
                        "eastward_wind_10m",
                        "northward_wind_10m",
                    ):
                        extracted = bilinear_interpolate(
                            field=dataset[variable_id].isel(lead_time=lead_hours).values,
                            x=dataset.x.values,
                            y=dataset.y.values,
                            station_x=station_x,
                            station_y=station_y,
                        )
                        rows.append(
                            {
                                "station_id": str(station.station_id),
                                "lead_hours": lead_hours,
                                "variable_id": variable_id,
                                "value": extracted.value,
                                "weights": [
                                    extracted.weights.w00,
                                    extracted.weights.w01,
                                    extracted.weights.w10,
                                    extracted.weights.w11,
                                ],
                            }
                        )
            return {"schema_version": "point-extraction-report.v1", "rows": rows}

        extraction_report = artifact_service.execute_role_bound_transformation(
            self._transformation_request(
                request,
                activity_type="guidance.bilinear-extract",
                inputs=(
                    ("guidance", canonical_guidance.artifact_id),
                    ("stations", station_catalog.snapshot.artifact_id),
                ),
                output_artifact_type="point-extraction-report",
                output_media_type="application/json",
                run=True,
            ),
            _extract,
            self._json,
            input_bindings={
                "guidance": InputBinding(self._netcdf.deserialize, validate_phase1_hrrr_guidance),
                "stations": InputBinding(self._json.deserialize, _valid_map),
            },
            output_validator=_valid_map,
        ).output

        def _baseline(inputs: Mapping[str, Any]) -> xr.Dataset:
            extraction = inputs["extraction"]
            assert isinstance(extraction, dict)
            station_values = {
                (row["station_id"], row["lead_hours"], row["variable_id"]): row["value"]
                for row in extraction["rows"]
            }
            return assemble_baseline_forecast(
                station_values=station_values,
                lead_hours=_LEADS,
                forecast_issue_time=np.datetime64(
                    request.forecast_issue_time.replace(tzinfo=None), "ns"
                ),
                hrrr_source_reference_time=np.datetime64(request.cycle.replace(tzinfo=None), "ns"),
                contributor_artifact_id=str(canonical_guidance.artifact_id),
                extraction_report_artifact_id=str(extraction_report.artifact_id),
            )

        baseline = artifact_service.execute_role_bound_transformation(
            self._transformation_request(
                request,
                activity_type="forecast.baseline",
                inputs=(
                    ("guidance", canonical_guidance.artifact_id),
                    ("stations", station_catalog.snapshot.artifact_id),
                    ("extraction", extraction_report.artifact_id),
                ),
                output_artifact_type="baseline-forecast",
                output_media_type="application/x-netcdf",
                run=True,
            ),
            _baseline,
            self._netcdf,
            input_bindings={
                "guidance": InputBinding(self._netcdf.deserialize, validate_phase1_hrrr_guidance),
                "stations": InputBinding(self._json.deserialize, _valid_map),
                "extraction": InputBinding(self._json.deserialize, _valid_map),
            },
            output_validator=_valid_dataset,
        ).output

        return ForecastArtifacts(
            acquisition_manifest=acquisition_manifest,
            variable_lineage=variable_lineage,
            canonical_guidance=canonical_guidance,
            extraction_report=extraction_report,
            baseline=baseline,
        )

    def verify(
        self,
        request: Phase1Request,
        run: RunManifest,
        forecast: ForecastArtifacts,
        observations: ObservationArtifacts,
        *,
        artifact_service: ArtifactService,
    ) -> VerificationArtifacts:
        def _match(inputs: Mapping[str, Any]) -> pa.Table:
            baseline = inputs["baseline"]
            observations_table = inputs["observations"]
            assert isinstance(baseline, xr.Dataset)
            assert isinstance(observations_table, pa.Table)
            return build_matched_pairs_table(
                match_baseline_to_observations(
                    baseline=baseline,
                    station_ids=self._configuration.domain.station_ids,
                    lead_hours=_LEADS,
                    observations=_observations_from_table(observations_table),
                    matching_policy=self._configuration.matching_policy,
                    verification_cutoff=request.verification_cutoff,
                    baseline_artifact_id=forecast.baseline.artifact_id,
                    observations_artifact_id=observations.normalized.artifact_id,
                )
            )

        matched_pairs = artifact_service.execute_role_bound_transformation(
            self._transformation_request(
                request,
                activity_type="verification.match",
                inputs=(
                    ("baseline", forecast.baseline.artifact_id),
                    ("observations", observations.normalized.artifact_id),
                ),
                output_artifact_type="matched-pairs",
                output_media_type="application/x-parquet",
                run=True,
            ),
            _match,
            self._parquet,
            input_bindings={
                "baseline": InputBinding(self._netcdf.deserialize, _valid_dataset),
                "observations": InputBinding(self._parquet.deserialize, _valid_table),
            },
            output_validator=_valid_table,
        ).output

        def _metrics(inputs: Mapping[str, Any]) -> dict[str, object]:
            pairs_table = inputs["pairs"]
            assert isinstance(pairs_table, pa.Table)
            return compute_verification_report(
                _matched_pairs_from_table(pairs_table),
                metric_set=self._configuration.metric_set,
                station_ids=tuple(map(str, self._configuration.domain.station_ids)),
                lead_hours=_LEADS,
                baseline_artifact_id=forecast.baseline.artifact_id,
                matched_pairs_artifact_id=matched_pairs.artifact_id,
            ).model_dump(mode="json")

        report = artifact_service.execute_role_bound_transformation(
            self._transformation_request(
                request,
                activity_type="verification.metrics",
                inputs=(
                    ("pairs", matched_pairs.artifact_id),
                    ("baseline", forecast.baseline.artifact_id),
                    ("guidance", forecast.canonical_guidance.artifact_id),
                ),
                output_artifact_type="verification-report",
                output_media_type="application/json",
                run=True,
            ),
            _metrics,
            self._json,
            input_bindings={
                "pairs": InputBinding(self._parquet.deserialize, _valid_table),
                "baseline": InputBinding(self._netcdf.deserialize, _valid_dataset),
                "guidance": InputBinding(self._netcdf.deserialize, validate_phase1_hrrr_guidance),
            },
            output_validator=_verification_report_output_validator,
        ).output

        return VerificationArtifacts(matched_pairs=matched_pairs, report=report)

    # ------------------------------------------------------------------
    # shared request builders
    # ------------------------------------------------------------------

    def _source_request(
        self,
        request: Phase1Request,
        *,
        source_authority: str,
        source_locator: str,
        artifact_type: str,
        artifact_schema_version: str,
        media_type: str,
        created_at: datetime,
        available_at: datetime,
        source_revision: str = "acquisition.v1",
    ) -> SourceRegistrationRequest:
        return SourceRegistrationRequest(
            source_authority=source_authority,
            source_locator=source_locator,
            source_revision=source_revision,
            artifact_type=artifact_type,
            artifact_schema_version=artifact_schema_version,
            media_type=media_type,
            created_at=created_at,
            availability=Availability(
                available_at=available_at, authority=source_authority, method=source_revision
            ),
            configuration_snapshot_id=request.configuration_snapshot_id,
            configuration_digest=request.configuration_digest,
            code_revision=request.code_revision,
            environment_digest=request.environment_digest,
        )

    def _transformation_request(
        self,
        request: Phase1Request,
        *,
        activity_type: str,
        inputs: tuple[tuple[str, ArtifactId], ...],
        output_artifact_type: str,
        output_media_type: str,
        run: bool,
    ) -> TransformationRequest:
        return TransformationRequest(
            activity_type=activity_type,
            activity_version="v1",
            inputs=tuple(
                TransformationInputRef(role=role, artifact_id=artifact_id)
                for role, artifact_id in inputs
            ),
            output_role="primary",
            output_artifact_type=output_artifact_type,
            output_artifact_schema_version=f"{output_artifact_type}.v1",
            output_media_type=output_media_type,
            configuration_snapshot_id=request.configuration_snapshot_id,
            configuration_digest=request.configuration_digest,
            code_revision=request.code_revision,
            environment_digest=request.environment_digest,
            run_id=request.run_id if run else None,
        )


def _verification_report_output_validator(value: dict[str, object]) -> None:
    _verification_report_from_json(value)
