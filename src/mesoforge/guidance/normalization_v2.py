"""Phase 2 production guidance normalization (plan Sections 2.2-2.5,
3.5): decode per-lead, per-field HRRR/NBM/GFS GRIB2 payloads into
``canonical-guidance.v2`` datasets.

This module is the production wiring the Phase 2 review found missing:
HRRR/GFS grid-relative winds are asserted via ``uvRelativeToGrid`` and
rotated to earth-relative components with
``guidance.normalization.rotate_wind_to_earth_relative`` before
interpolation; NBM speed/direction is converted to earth-relative U/V
with ``guidance.sources.nbm.convert_speed_direction_to_components``
cornerwise (on the full native grid) before interpolation; and GFS APCP
duplicate/bucket selection is validated with
``guidance.precipitation.validate_dual_parent_equivalence`` and both
parents are recorded.

Each caller supplies ``field_payloads``: a mapping from
``canonical_variable_id`` to ``{source_lead_hour: message_bytes}``,
where every value is the exact single selected-message payload for
that one field at that one lead (the real byte-range acquisition
selects and ranges exactly one field row at a time; two fields sharing
an ambiguous decoded identity -- e.g. NBM's deterministic APCP and
PoP01, which cfgrib's message grouping cannot always disambiguate when
concatenated together -- must never be decoded from a shared payload).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pyproj
import xarray as xr

from mesoforge.catalog.sources import (
    GfsSourceSettings,
    HrrrPhase2SourceSettings,
    NbmSourceSettings,
    Phase2FieldContract,
)
from mesoforge.common.errors import MesoForgeError
from mesoforge.guidance.canonical_v2 import assemble_canonical_guidance_v2
from mesoforge.guidance.normalization import (
    WindRotationError,
    build_lambert_conformal_crs,
    compute_latlon_grid,
    compute_projected_coordinates,
    rotate_wind_to_earth_relative,
)
from mesoforge.guidance.precipitation import (
    ApcpCandidateRecord,
    BucketPrecipitationResult,
    compute_one_hour_qpf,
    is_bucket_reset_hour,
    select_bucket_record,
    validate_dual_parent_equivalence,
)
from mesoforge.guidance.sources.gfs_decoding import (
    decode_apcp_candidates,
    decode_instantaneous_message,
)
from mesoforge.guidance.sources.hrrr_phase2_decoding import (
    decode_selected_message as decode_hrrr_phase2_message,
)
from mesoforge.guidance.sources.nbm import (
    convert_pop_percent_to_fraction,
    convert_speed_direction_to_components,
)
from mesoforge.guidance.sources.nbm_decoding import decode_selected_message as decode_nbm_message

FieldPayloads = dict[str, dict[int, bytes | tuple[bytes, ...]]]


class GuidanceNormalizationV2Error(MesoForgeError):
    """Raised when Phase 2 per-cycle normalization cannot assemble a
    consistent canonical-guidance.v2 dataset (grid mismatch across
    leads, wind orientation disagreement, or a failed dual-parent APCP
    equivalence check)."""


@dataclass(frozen=True, slots=True)
class GfsQpfLineage:
    """Recorded per-lead GFS APCP bucket/duplicate lineage (finding 4):
    both parent records plus the dual-parent equivalence result, kept
    even when only one candidate existed (``duplicate=None``)."""

    forecast_hour: int
    bucket: ApcpCandidateRecord
    duplicate: ApcpCandidateRecord | None
    equivalent: bool | None
    result: BucketPrecipitationResult


def _find_contract(
    contracts: tuple[Phase2FieldContract, ...], variable_id: str
) -> Phase2FieldContract:
    return next(fc for fc in contracts if fc.canonical_variable_id == variable_id)


def _field_payload(field_payloads: FieldPayloads, variable_id: str, lead: int) -> bytes:
    by_lead = field_payloads.get(variable_id)
    if by_lead is None or lead not in by_lead:
        raise GuidanceNormalizationV2Error(
            f"no selected-message payload supplied for {variable_id!r} at lead {lead!r}"
        )
    value = by_lead[lead]
    if not isinstance(value, bytes):
        raise GuidanceNormalizationV2Error(
            f"{variable_id!r} at lead {lead!r} must be a single payload, not a tuple"
        )
    return value


def _apcp_field_payload(
    field_payloads: FieldPayloads, variable_id: str, lead: int
) -> bytes | tuple[bytes, ...]:
    by_lead = field_payloads.get(variable_id)
    if by_lead is None or lead not in by_lead:
        raise GuidanceNormalizationV2Error(
            f"no selected-message payload supplied for {variable_id!r} at lead {lead!r}"
        )
    return by_lead[lead]


def normalize_hrrr_phase2_cycle(
    *,
    settings: HrrrPhase2SourceSettings,
    forecast_reference_time: datetime,
    source_lead_hours: tuple[int, ...],
    field_payloads: FieldPayloads,
    grid_id: str,
    configuration_snapshot_id: str,
    variable_lineage_manifest_id: str,
) -> xr.Dataset:
    """Decode every HRRR Phase 2 lead's per-field selected-message
    payloads and assemble ``canonical-guidance.v2``. Grid-relative
    winds (``uvRelativeToGrid``) are asserted and rotated to
    earth-relative components before assembly (finding 5)."""
    cycle_date = forecast_reference_time.date()
    cycle_hour = forecast_reference_time.hour

    contracts = settings.field_contracts
    temperature_contract = _find_contract(contracts, "air_temperature_2m")
    dew_point_contract = _find_contract(contracts, "dew_point_temperature_2m")
    eastward_contract = _find_contract(contracts, "eastward_wind_10m")
    northward_contract = _find_contract(contracts, "northward_wind_10m")
    gust_contract = _find_contract(contracts, "wind_gust_10m")
    qpf_contract = _find_contract(contracts, "liquid_equivalent_precipitation_amount_1h")

    crs: pyproj.CRS | None = None
    x: np.ndarray | None = None
    y: np.ndarray | None = None
    lat: np.ndarray | None = None
    lon: np.ndarray | None = None

    temperature_leads: list[np.ndarray] = []
    dew_point_leads: list[np.ndarray] = []
    eastward_leads: list[np.ndarray] = []
    northward_leads: list[np.ndarray] = []
    gust_leads: list[np.ndarray] = []
    qpf_leads: list[np.ndarray] = []
    qpf_start_hours: list[int] = []

    for lead in source_lead_hours:
        temperature = decode_hrrr_phase2_message(
            _field_payload(field_payloads, "air_temperature_2m", lead),
            contract=temperature_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        dew_point = decode_hrrr_phase2_message(
            _field_payload(field_payloads, "dew_point_temperature_2m", lead),
            contract=dew_point_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        eastward = decode_hrrr_phase2_message(
            _field_payload(field_payloads, "eastward_wind_10m", lead),
            contract=eastward_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        northward = decode_hrrr_phase2_message(
            _field_payload(field_payloads, "northward_wind_10m", lead),
            contract=northward_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        gust = decode_hrrr_phase2_message(
            _field_payload(field_payloads, "wind_gust_10m", lead),
            contract=gust_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        qpf = decode_hrrr_phase2_message(
            _field_payload(field_payloads, "liquid_equivalent_precipitation_amount_1h", lead),
            contract=qpf_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )

        if crs is None:
            attrs = temperature.attrs
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

        u_relative = eastward.attrs.get("GRIB_uvRelativeToGrid")
        v_relative = northward.attrs.get("GRIB_uvRelativeToGrid")
        if u_relative not in (0, 1) or v_relative not in (0, 1):
            raise GuidanceNormalizationV2Error(
                f"HRRR lead {lead!r} wind uvRelativeToGrid must be 0 or 1 on both U and V, "
                f"got U={u_relative!r}, V={v_relative!r}"
            )
        assert crs is not None and x is not None and y is not None
        try:
            rotated = rotate_wind_to_earth_relative(
                u_grid=eastward.values,
                v_grid=northward.values,
                x=x,
                y=y,
                crs=crs,
                u_relative_to_grid=bool(u_relative),
                v_relative_to_grid=bool(v_relative),
            )
        except WindRotationError as exc:
            raise GuidanceNormalizationV2Error(
                f"HRRR lead {lead!r} wind rotation failed: {exc}"
            ) from exc

        temperature_leads.append(temperature.values)
        dew_point_leads.append(dew_point.values)
        eastward_leads.append(rotated.eastward)
        northward_leads.append(rotated.northward)
        gust_leads.append(gust.values)
        qpf_leads.append(qpf.values)
        qpf_start_hours.append(lead - 1)

    assert lat is not None and lon is not None and x is not None and y is not None
    return assemble_canonical_guidance_v2(
        model="hrrr",
        forecast_reference_time=np.datetime64(forecast_reference_time.replace(tzinfo=None), "ns"),
        source_lead_hours=source_lead_hours,
        x=x,
        y=y,
        lat=lat,
        lon=lon,
        instantaneous_fields={
            "air_temperature_2m": np.stack(temperature_leads),
            "dew_point_temperature_2m": np.stack(dew_point_leads),
            "eastward_wind_10m": np.stack(eastward_leads),
            "northward_wind_10m": np.stack(northward_leads),
            "wind_gust_10m": np.stack(gust_leads),
        },
        interval_fields={"liquid_equivalent_precipitation_amount_1h": np.stack(qpf_leads)},
        interval_start_hours={"liquid_equivalent_precipitation_amount_1h": tuple(qpf_start_hours)},
        grid_id=grid_id,
        configuration_snapshot_id=configuration_snapshot_id,
        variable_lineage_manifest_id=variable_lineage_manifest_id,
        crs_wkt2=crs.to_wkt() if crs is not None else None,
    )


def normalize_gfs_cycle(
    *,
    settings: GfsSourceSettings,
    forecast_reference_time: datetime,
    source_lead_hours: tuple[int, ...],
    field_payloads: FieldPayloads,
    grid_id: str,
    configuration_snapshot_id: str,
    variable_lineage_manifest_id: str,
) -> tuple[xr.Dataset, tuple[GfsQpfLineage, ...]]:
    """Decode every GFS lead and assemble ``canonical-guidance.v2``.

    For every non-reset lead, the same-bucket previous-hour APCP
    value is required at every grid point; Phase 2 always acquires
    the full contiguous lead range, so the immediately preceding
    lead's own APCP payload is decoded from ``field_payloads`` (and
    cached across leads to avoid redundant decoding). Grid-relative
    winds are asserted and rotated (finding 5); every lead's bucket/
    duplicate APCP candidates are validated for dual-parent
    equivalence and both parents are recorded in the returned
    lineage (finding 4).
    """
    cycle_date = forecast_reference_time.date()
    cycle_hour = forecast_reference_time.hour

    contracts = settings.field_contracts
    temperature_contract = _find_contract(contracts, "air_temperature_2m")
    dew_point_contract = _find_contract(contracts, "dew_point_temperature_2m")
    eastward_contract = _find_contract(contracts, "eastward_wind_10m")
    northward_contract = _find_contract(contracts, "northward_wind_10m")
    gust_contract = _find_contract(contracts, "wind_gust_10m")
    qpf_contract = _find_contract(contracts, "liquid_equivalent_precipitation_amount_1h")

    crs: pyproj.CRS | None = None
    x: np.ndarray | None = None
    y: np.ndarray | None = None
    lat: np.ndarray | None = None
    lon: np.ndarray | None = None
    grid_shape: tuple[int, int] | None = None
    x_order: np.ndarray | None = None

    temperature_leads: list[np.ndarray] = []
    dew_point_leads: list[np.ndarray] = []
    eastward_leads: list[np.ndarray] = []
    northward_leads: list[np.ndarray] = []
    gust_leads: list[np.ndarray] = []
    qpf_leads: list[np.ndarray] = []
    qpf_start_hours: list[int] = []
    lineage: list[GfsQpfLineage] = []
    bucket_cache: dict[int, ApcpCandidateRecord] = {}

    def _decode_bucket(lead: int) -> tuple[ApcpCandidateRecord, ApcpCandidateRecord | None]:
        records = decode_apcp_candidates(
            _apcp_field_payload(field_payloads, "liquid_equivalent_precipitation_amount_1h", lead),
            contract=qpf_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        bucket_record = select_bucket_record(records, forecast_hour=lead)
        duplicate_record = next((r for r in records if r is not bucket_record), None)
        return bucket_record, duplicate_record

    for lead in source_lead_hours:
        temperature = decode_instantaneous_message(
            _field_payload(field_payloads, "air_temperature_2m", lead),
            contract=temperature_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        dew_point = decode_instantaneous_message(
            _field_payload(field_payloads, "dew_point_temperature_2m", lead),
            contract=dew_point_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        eastward = decode_instantaneous_message(
            _field_payload(field_payloads, "eastward_wind_10m", lead),
            contract=eastward_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        northward = decode_instantaneous_message(
            _field_payload(field_payloads, "northward_wind_10m", lead),
            contract=northward_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        gust = decode_instantaneous_message(
            _field_payload(field_payloads, "wind_gust_10m", lead),
            contract=gust_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )

        if crs is None:
            attrs = temperature.attrs
            ni = int(attrs["GRIB_Ni"])
            nj = int(attrs["GRIB_Nj"])
            dx = float(attrs["GRIB_iDirectionIncrementInDegrees"])
            dy = float(attrs["GRIB_jDirectionIncrementInDegrees"])
            first_lon = float(attrs["GRIB_longitudeOfFirstGridPointInDegrees"])
            first_lat = float(attrs["GRIB_latitudeOfFirstGridPointInDegrees"])
            source_x = first_lon + dx * np.arange(ni, dtype=np.float64)
            normalized_x = (source_x + 180.0) % 360.0 - 180.0
            # GFS publishes 0..360 longitudes. Point lookup uses a
            # monotonic [-180, 180) axis, so reorder every decoded field
            # before interpolation while retaining source longitudes.
            x_order = np.argsort(normalized_x)
            x = normalized_x[x_order]
            y = first_lat + dy * np.arange(nj, dtype=np.float64)
            lon, lat = np.meshgrid(source_x[x_order], y)
            crs = pyproj.CRS.from_epsg(4326)
            grid_shape = (nj, ni)

        u_relative = eastward.attrs.get("GRIB_uvRelativeToGrid")
        v_relative = northward.attrs.get("GRIB_uvRelativeToGrid")
        if u_relative not in (0, 1) or v_relative not in (0, 1):
            raise GuidanceNormalizationV2Error(
                f"GFS lead {lead!r} wind uvRelativeToGrid must be 0 or 1 on both U and V, "
                f"got U={u_relative!r}, V={v_relative!r}"
            )
        assert x is not None and y is not None and crs is not None and x_order is not None
        try:
            rotated = rotate_wind_to_earth_relative(
                u_grid=eastward.values[:, x_order],
                v_grid=northward.values[:, x_order],
                x=x,
                y=y,
                crs=crs,
                u_relative_to_grid=bool(u_relative),
                v_relative_to_grid=bool(v_relative),
            )
        except WindRotationError as exc:
            raise GuidanceNormalizationV2Error(
                f"GFS lead {lead!r} wind rotation failed: {exc}"
            ) from exc

        bucket = bucket_cache.get(lead)
        duplicate: ApcpCandidateRecord | None = None
        if bucket is None:
            bucket, duplicate = _decode_bucket(lead)
            bucket_cache[lead] = bucket
        equivalent: bool | None = None
        if duplicate is not None:
            equivalent = validate_dual_parent_equivalence(bucket, duplicate)
            if not equivalent:
                raise GuidanceNormalizationV2Error(
                    f"GFS lead {lead!r} bucket/duplicate APCP candidates are not equivalent; "
                    "refusing to canonicalize an ambiguous record"
                )

        reset = is_bucket_reset_hour(lead)
        if reset:
            previous_bucket = None
        else:
            previous_bucket = bucket_cache.get(lead - 1)
            if previous_bucket is None:
                previous_bucket, _prev_duplicate = _decode_bucket(lead - 1)
                bucket_cache[lead - 1] = previous_bucket

        assert grid_shape is not None
        n_points = grid_shape[0] * grid_shape[1]
        one_hour = np.empty(n_points, dtype=np.float64)
        result: BucketPrecipitationResult | None = None
        for index in range(n_points):
            current_value = bucket.values_kg_m2[index]
            previous_value = (
                None if previous_bucket is None else previous_bucket.values_kg_m2[index]
            )
            computation = compute_one_hour_qpf(
                forecast_hour=lead,
                bucket_value_current_kg_m2=current_value,
                bucket_value_previous_kg_m2=previous_value,
            )
            one_hour[index] = computation.one_hour_qpf_kg_m2
            result = computation
        assert result is not None
        lineage.append(
            GfsQpfLineage(
                forecast_hour=lead,
                bucket=bucket,
                duplicate=duplicate,
                equivalent=equivalent,
                result=result,
            )
        )

        temperature_leads.append(temperature.values[:, x_order])
        dew_point_leads.append(dew_point.values[:, x_order])
        eastward_leads.append(rotated.eastward)
        northward_leads.append(rotated.northward)
        gust_leads.append(gust.values[:, x_order])
        qpf_leads.append(one_hour.reshape(grid_shape)[:, x_order])
        qpf_start_hours.append(lead - 1)

    assert lat is not None and lon is not None and x is not None and y is not None
    dataset = assemble_canonical_guidance_v2(
        model="gfs",
        forecast_reference_time=np.datetime64(forecast_reference_time.replace(tzinfo=None), "ns"),
        source_lead_hours=source_lead_hours,
        x=x,
        y=y,
        lat=lat,
        lon=lon,
        instantaneous_fields={
            "air_temperature_2m": np.stack(temperature_leads),
            "dew_point_temperature_2m": np.stack(dew_point_leads),
            "eastward_wind_10m": np.stack(eastward_leads),
            "northward_wind_10m": np.stack(northward_leads),
            "wind_gust_10m": np.stack(gust_leads),
        },
        interval_fields={"liquid_equivalent_precipitation_amount_1h": np.stack(qpf_leads)},
        interval_start_hours={"liquid_equivalent_precipitation_amount_1h": tuple(qpf_start_hours)},
        grid_id=grid_id,
        configuration_snapshot_id=configuration_snapshot_id,
        variable_lineage_manifest_id=variable_lineage_manifest_id,
        crs_wkt2=crs.to_wkt() if crs is not None else None,
    )
    return dataset, tuple(lineage)


def normalize_nbm_cycle(
    *,
    settings: NbmSourceSettings,
    forecast_reference_time: datetime,
    source_lead_hours: tuple[int, ...],
    field_payloads: FieldPayloads,
    grid_id: str,
    configuration_snapshot_id: str,
    variable_lineage_manifest_id: str,
) -> xr.Dataset:
    """Decode every NBM lead and assemble ``canonical-guidance.v2``.
    Speed/direction is converted to earth-relative U/V cornerwise (on
    the full native grid) *before* any spatial interpolation happens
    downstream (finding 5) using
    ``guidance.sources.nbm.convert_speed_direction_to_components``.
    Deterministic APCP and PoP01 -- which can share an ambiguous
    decoded identity when concatenated -- are always decoded from
    their own separate single-message payloads (``field_payloads``)."""
    cycle_date = forecast_reference_time.date()
    cycle_hour = forecast_reference_time.hour

    contracts = settings.field_contracts
    temperature_contract = _find_contract(contracts, "air_temperature_2m")
    dew_point_contract = _find_contract(contracts, "dew_point_temperature_2m")
    speed_contract = _find_contract(contracts, "wind_speed_10m")
    direction_contract = _find_contract(contracts, "wind_from_direction_10m")
    gust_contract = _find_contract(contracts, "wind_gust_10m")
    qpf_contract = _find_contract(contracts, "liquid_equivalent_precipitation_amount_1h")
    pop_contract = _find_contract(contracts, "probability_of_precipitation_1h")

    x: np.ndarray | None = None
    y: np.ndarray | None = None
    lat: np.ndarray | None = None
    lon: np.ndarray | None = None

    temperature_leads: list[np.ndarray] = []
    dew_point_leads: list[np.ndarray] = []
    eastward_leads: list[np.ndarray] = []
    northward_leads: list[np.ndarray] = []
    gust_leads: list[np.ndarray] = []
    qpf_leads: list[np.ndarray] = []
    pop_leads: list[np.ndarray] = []
    interval_start_hours: list[int] = []

    for lead in source_lead_hours:
        temperature = decode_nbm_message(
            _field_payload(field_payloads, "air_temperature_2m", lead),
            contract=temperature_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        dew_point = decode_nbm_message(
            _field_payload(field_payloads, "dew_point_temperature_2m", lead),
            contract=dew_point_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        speed = decode_nbm_message(
            _field_payload(field_payloads, "wind_speed_10m", lead),
            contract=speed_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        direction = decode_nbm_message(
            _field_payload(field_payloads, "wind_from_direction_10m", lead),
            contract=direction_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        gust = decode_nbm_message(
            _field_payload(field_payloads, "wind_gust_10m", lead),
            contract=gust_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        qpf = decode_nbm_message(
            _field_payload(field_payloads, "liquid_equivalent_precipitation_amount_1h", lead),
            contract=qpf_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )
        pop = decode_nbm_message(
            _field_payload(field_payloads, "probability_of_precipitation_1h", lead),
            contract=pop_contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        )

        if x is None:
            attrs = temperature.attrs
            raw_nx = attrs.get("GRIB_Nx", attrs.get("GRIB_Ni"))
            raw_ny = attrs.get("GRIB_Ny", attrs.get("GRIB_Nj"))
            if raw_nx is None or raw_ny is None:
                raise GuidanceNormalizationV2Error(
                    "NBM lead is missing required grid key Nx/Ni or Ny/Nj"
                )
            ni = int(raw_nx)
            nj = int(raw_ny)
            dx = float(attrs["GRIB_iDirectionIncrementInDegrees"])
            dy = float(attrs["GRIB_jDirectionIncrementInDegrees"])
            first_lon = float(attrs["GRIB_longitudeOfFirstGridPointInDegrees"])
            first_lat = float(attrs["GRIB_latitudeOfFirstGridPointInDegrees"])
            x = first_lon + dx * np.arange(ni, dtype=np.float64)
            y = first_lat + dy * np.arange(nj, dtype=np.float64)
            lon, lat = np.meshgrid(x, y)

        speed_values = speed.values
        direction_values = direction.values
        flat_speed = speed_values.ravel()
        flat_direction = direction_values.ravel()
        flat_u = np.empty_like(flat_speed, dtype=np.float64)
        flat_v = np.empty_like(flat_speed, dtype=np.float64)
        for index in range(flat_speed.size):
            u, v = convert_speed_direction_to_components(
                speed_m_s=float(flat_speed[index]), direction_degrees=float(flat_direction[index])
            )
            flat_u[index] = u
            flat_v[index] = v
        u_grid = flat_u.reshape(speed_values.shape)
        v_grid = flat_v.reshape(speed_values.shape)

        temperature_leads.append(temperature.values)
        dew_point_leads.append(dew_point.values)
        eastward_leads.append(u_grid)
        northward_leads.append(v_grid)
        gust_leads.append(gust.values)
        qpf_leads.append(qpf.values)
        pop_leads.append(
            np.vectorize(convert_pop_percent_to_fraction)(pop.values.astype(np.float64))
        )
        interval_start_hours.append(lead - 1)

    assert lat is not None and lon is not None and x is not None and y is not None
    return assemble_canonical_guidance_v2(
        model="nbm",
        forecast_reference_time=np.datetime64(forecast_reference_time.replace(tzinfo=None), "ns"),
        source_lead_hours=source_lead_hours,
        x=x,
        y=y,
        lat=lat,
        lon=lon,
        instantaneous_fields={
            "air_temperature_2m": np.stack(temperature_leads),
            "dew_point_temperature_2m": np.stack(dew_point_leads),
            "eastward_wind_10m": np.stack(eastward_leads),
            "northward_wind_10m": np.stack(northward_leads),
            "wind_gust_10m": np.stack(gust_leads),
        },
        interval_fields={
            "liquid_equivalent_precipitation_amount_1h": np.stack(qpf_leads),
            "probability_of_precipitation_1h": np.stack(pop_leads),
        },
        interval_start_hours={
            "liquid_equivalent_precipitation_amount_1h": tuple(interval_start_hours),
            "probability_of_precipitation_1h": tuple(interval_start_hours),
        },
        grid_id=grid_id,
        configuration_snapshot_id=configuration_snapshot_id,
        variable_lineage_manifest_id=variable_lineage_manifest_id,
    )
