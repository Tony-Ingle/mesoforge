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

from mesoforge.catalog.domains import BoundingBox
from mesoforge.catalog.sources import (
    GfsSourceSettings,
    HrrrPhase2SourceSettings,
    NbmSourceSettings,
    Phase2FieldContract,
)
from mesoforge.common.errors import MesoForgeError
from mesoforge.guidance.canonical_v2 import (
    RetainedGridSubset,
    assemble_canonical_guidance_v2,
)
from mesoforge.guidance.nbm_geometry import compute_nbm_grid
from mesoforge.guidance.normalization import (
    SubsettingError,
    WindRotationError,
    build_lambert_conformal_crs,
    compute_bbox_halo_subset_indices,
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

# Latitudes are published to at least 6 decimal places; this tolerance
# only absorbs that rounding when cross-checking a regular lat/lon
# message's declared first/last latitudes against its declared
# increment.
_GRID_SPAN_TOLERANCE_DEG = 1e-6


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


def _retained_subset(
    *,
    model: str,
    x: np.ndarray,
    y: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    domain_bbox: BoundingBox,
    halo_cells: int,
) -> tuple[RetainedGridSubset, slice, slice]:
    """Compute the native-grid bbox+halo window Phase 2 retains.

    This is the identical Phase 1 rule
    (``compute_bbox_halo_subset_indices``): the smallest native-grid
    rectangle covering the inclusive configured domain bbox plus
    ``halo_cells`` complete source cells on every side. Every source
    cell any approved station's bilinear 2x2 neighbourhood can read is
    inside it, so retained canonical values and interpolation weights
    are identical to full-grid processing -- while the retained arrays
    stay bounded on any host.
    """
    ny, nx = lat.shape
    try:
        indices = compute_bbox_halo_subset_indices(
            x=x, y=y, lat=lat, lon=lon, bbox=domain_bbox, halo_cells=halo_cells
        )
    except SubsettingError as exc:
        raise GuidanceNormalizationV2Error(
            f"{model} native grid cannot supply the configured domain bbox plus "
            f"{halo_cells}-cell halo: {exc}"
        ) from exc
    subset = RetainedGridSubset(
        source_ny=ny,
        source_nx=nx,
        y_start=indices.y_start,
        y_end=indices.y_end,
        x_start=indices.x_start,
        x_end=indices.x_end,
        halo_cells=halo_cells,
        bbox_south=domain_bbox.south,
        bbox_north=domain_bbox.north,
        bbox_west=domain_bbox.west,
        bbox_east=domain_bbox.east,
    )
    return subset, slice(indices.y_start, indices.y_end), slice(indices.x_start, indices.x_end)


def normalize_hrrr_phase2_cycle(
    *,
    settings: HrrrPhase2SourceSettings,
    forecast_reference_time: datetime,
    source_lead_hours: tuple[int, ...],
    field_payloads: FieldPayloads,
    grid_id: str,
    configuration_snapshot_id: str,
    variable_lineage_manifest_id: str,
    domain_bbox: BoundingBox,
    halo_cells: int,
) -> xr.Dataset:
    """Decode every HRRR Phase 2 lead's per-field selected-message
    payloads and assemble ``canonical-guidance.v2``. Grid-relative
    winds (``uvRelativeToGrid``) are asserted and rotated to
    earth-relative components before assembly (finding 5).

    Only the configured ``domain_bbox`` plus ``halo_cells`` of the
    native grid is retained (owner architecture decision). The window
    is computed from the full native geometry, so the retained cells,
    their coordinates, and every value in them are exactly what
    full-grid processing would have produced; the wind rotation basis
    at a retained point depends only on that point's own projected
    coordinate and the grid increment, both unchanged by subsetting.
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
    subset: RetainedGridSubset | None = None
    y_slice: slice | None = None
    x_slice: slice | None = None

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
            subset, y_slice, x_slice = _retained_subset(
                model="HRRR",
                x=x,
                y=y,
                lat=lat,
                lon=lon,
                domain_bbox=domain_bbox,
                halo_cells=halo_cells,
            )
            x = x[x_slice]
            y = y[y_slice]
            lat = lat[y_slice, x_slice]
            lon = lon[y_slice, x_slice]

        u_relative = eastward.attrs.get("GRIB_uvRelativeToGrid")
        v_relative = northward.attrs.get("GRIB_uvRelativeToGrid")
        if u_relative not in (0, 1) or v_relative not in (0, 1):
            raise GuidanceNormalizationV2Error(
                f"HRRR lead {lead!r} wind uvRelativeToGrid must be 0 or 1 on both U and V, "
                f"got U={u_relative!r}, V={v_relative!r}"
            )
        assert crs is not None and x is not None and y is not None
        assert y_slice is not None and x_slice is not None
        try:
            rotated = rotate_wind_to_earth_relative(
                u_grid=eastward.values[y_slice, x_slice],
                v_grid=northward.values[y_slice, x_slice],
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

        temperature_leads.append(temperature.values[y_slice, x_slice])
        dew_point_leads.append(dew_point.values[y_slice, x_slice])
        eastward_leads.append(rotated.eastward)
        northward_leads.append(rotated.northward)
        gust_leads.append(gust.values[y_slice, x_slice])
        qpf_leads.append(qpf.values[y_slice, x_slice])
        qpf_start_hours.append(lead - 1)

    assert lat is not None and lon is not None and x is not None and y is not None
    assert subset is not None
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
        retained_subset=subset,
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
    domain_bbox: BoundingBox,
    halo_cells: int,
) -> tuple[xr.Dataset, tuple[GfsQpfLineage, ...]]:
    """Decode every GFS lead and assemble ``canonical-guidance.v2``.

    For every non-reset lead, the same-bucket previous-hour APCP
    value is required at every grid point; Phase 2 always acquires
    the full contiguous lead range, so the immediately preceding
    lead's own APCP payload is decoded from ``field_payloads`` (and
    cached across leads to avoid redundant decoding). Grid-relative
    winds are asserted and rotated (finding 5); every lead's bucket/
    duplicate APCP candidates are validated for dual-parent
    equivalence over their full native arrays, and both parents are
    recorded in the returned lineage (finding 4).

    Only the configured ``domain_bbox`` plus ``halo_cells`` of the
    native grid is retained (owner architecture decision). Subsetting
    happens after the provider's ``[0, 360)`` longitude axis has been
    reordered onto the monotonic ``[-180, 180)`` axis and after
    dual-parent equivalence has been checked on the full arrays, and
    ``compute_one_hour_qpf`` is pointwise, so every retained value is
    exactly what full-grid processing produced.
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
    subset: RetainedGridSubset | None = None
    y_slice: slice | None = None
    x_slice: slice | None = None

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
            last_lat = float(attrs["GRIB_latitudeOfLastGridPointInDegrees"])
            source_x = first_lon + dx * np.arange(ni, dtype=np.float64)
            normalized_x = (source_x + 180.0) % 360.0 - 180.0
            # GFS publishes 0..360 longitudes. Point lookup uses a
            # monotonic [-180, 180) axis, so reorder every decoded field
            # before interpolation while retaining source longitudes.
            x_order = np.argsort(normalized_x)
            x = normalized_x[x_order]
            # The operational GFS 0.25-degree product scans
            # north-to-south (jScansPositively=0): its first grid row is
            # +90 and its last is -90. Building the latitude axis as
            # ``first_lat + dy * arange`` therefore fabricates a 90..270
            # axis that is not a latitude at all, mislocating every row.
            # Derive the axis from the message's own declared first and
            # last latitudes instead, and require the declared increment
            # to agree with that span so a contradictory geometry fails
            # closed rather than silently producing a wrong grid.
            y = np.linspace(first_lat, last_lat, nj, dtype=np.float64)
            expected_span = dy * (nj - 1)
            if nj > 1 and abs(abs(last_lat - first_lat) - expected_span) > _GRID_SPAN_TOLERANCE_DEG:
                raise GuidanceNormalizationV2Error(
                    f"GFS declared latitude span {abs(last_lat - first_lat)!r} disagrees with "
                    f"jDirectionIncrementInDegrees {dy!r} over {nj} rows (expected "
                    f"{expected_span!r}); the message's declared geometry is self-contradictory"
                )
            lon, lat = np.meshgrid(source_x[x_order], y)
            crs = pyproj.CRS.from_epsg(4326)
            grid_shape = (nj, ni)
            # The bbox is expressed on the monotonic [-180, 180) axis,
            # which is the axis station lookup uses; ``lon`` retains the
            # provider's own [0, 360) longitudes as source evidence.
            subset, y_slice, x_slice = _retained_subset(
                model="GFS",
                x=x,
                y=y,
                lat=lat,
                lon=np.broadcast_to(x, lat.shape),
                domain_bbox=domain_bbox,
                halo_cells=halo_cells,
            )
            x = x[x_slice]
            y = y[y_slice]
            lat = lat[y_slice, x_slice]
            lon = lon[y_slice, x_slice]

        u_relative = eastward.attrs.get("GRIB_uvRelativeToGrid")
        v_relative = northward.attrs.get("GRIB_uvRelativeToGrid")
        if u_relative not in (0, 1) or v_relative not in (0, 1):
            raise GuidanceNormalizationV2Error(
                f"GFS lead {lead!r} wind uvRelativeToGrid must be 0 or 1 on both U and V, "
                f"got U={u_relative!r}, V={v_relative!r}"
            )
        assert x is not None and y is not None and crs is not None and x_order is not None
        assert y_slice is not None and x_slice is not None
        try:
            rotated = rotate_wind_to_earth_relative(
                u_grid=eastward.values[:, x_order][y_slice, x_slice],
                v_grid=northward.values[:, x_order][y_slice, x_slice],
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
        # Same-bucket differencing is pointwise, so reordering onto the
        # monotonic axis and cutting to the retained window before
        # differencing yields exactly the values full-grid differencing
        # would have produced at those same cells.
        current_retained = np.asarray(bucket.values_kg_m2, dtype=np.float64).reshape(grid_shape)[
            :, x_order
        ][y_slice, x_slice]
        previous_retained = (
            None
            if previous_bucket is None
            else np.asarray(previous_bucket.values_kg_m2, dtype=np.float64).reshape(grid_shape)[
                :, x_order
            ][y_slice, x_slice]
        )
        flat_current = current_retained.ravel()
        flat_previous = None if previous_retained is None else previous_retained.ravel()
        one_hour = np.empty(flat_current.size, dtype=np.float64)
        result: BucketPrecipitationResult | None = None
        for index in range(flat_current.size):
            computation = compute_one_hour_qpf(
                forecast_hour=lead,
                bucket_value_current_kg_m2=float(flat_current[index]),
                bucket_value_previous_kg_m2=(
                    None if flat_previous is None else float(flat_previous[index])
                ),
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

        temperature_leads.append(temperature.values[:, x_order][y_slice, x_slice])
        dew_point_leads.append(dew_point.values[:, x_order][y_slice, x_slice])
        eastward_leads.append(rotated.eastward)
        northward_leads.append(rotated.northward)
        gust_leads.append(gust.values[:, x_order][y_slice, x_slice])
        qpf_leads.append(one_hour.reshape(current_retained.shape))
        qpf_start_hours.append(lead - 1)

    assert lat is not None and lon is not None and x is not None and y is not None
    assert subset is not None
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
        retained_subset=subset,
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
    domain_bbox: BoundingBox,
    halo_cells: int,
) -> xr.Dataset:
    """Decode every NBM lead and assemble ``canonical-guidance.v2``.
    Speed/direction is converted to earth-relative U/V cornerwise (on
    the retained native cells) *before* any spatial interpolation
    happens downstream (finding 5) using
    ``guidance.sources.nbm.convert_speed_direction_to_components``.
    Deterministic APCP and PoP01 -- which can share an ambiguous
    decoded identity when concatenated -- are always decoded from
    their own separate single-message payloads (``field_payloads``).

    Only the configured ``domain_bbox`` plus ``halo_cells`` of the
    approved native grid is retained (owner architecture decision).
    The window is derived from the approved profile's full native
    geometry, and the speed/direction conversion is pointwise, so
    every retained value is exactly what full-grid processing
    produced.
    """
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

    nbm_crs: pyproj.CRS | None = None
    x: np.ndarray | None = None
    y: np.ndarray | None = None
    lat: np.ndarray | None = None
    lon: np.ndarray | None = None
    subset: RetainedGridSubset | None = None
    y_slice: slice | None = None
    x_slice: slice | None = None

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
            # Codex re-review finding 2: the NBM CONUS core product is a
            # Lambert conformal conic *projected* grid. Building x/y from
            # the first grid point plus a degree increment -- as this
            # previously did -- fabricates a geographic mesh that is not
            # the published grid, mislocating every station. Normalize on
            # the approved profile's real projected CRS instead, and
            # inverse-project for the true 2-D lat/lon mesh.
            crs, x, y, lat, lon = compute_nbm_grid(settings.grid_profile)
            nbm_crs = crs
            subset, y_slice, x_slice = _retained_subset(
                model="NBM",
                x=x,
                y=y,
                lat=lat,
                lon=lon,
                domain_bbox=domain_bbox,
                halo_cells=halo_cells,
            )
            x = x[x_slice]
            y = y[y_slice]
            lat = lat[y_slice, x_slice]
            lon = lon[y_slice, x_slice]

        assert y_slice is not None and x_slice is not None
        speed_values = speed.values[y_slice, x_slice]
        direction_values = direction.values[y_slice, x_slice]
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

        temperature_leads.append(temperature.values[y_slice, x_slice])
        dew_point_leads.append(dew_point.values[y_slice, x_slice])
        eastward_leads.append(u_grid)
        northward_leads.append(v_grid)
        gust_leads.append(gust.values[y_slice, x_slice])
        qpf_leads.append(qpf.values[y_slice, x_slice])
        pop_leads.append(
            np.vectorize(convert_pop_percent_to_fraction)(
                pop.values[y_slice, x_slice].astype(np.float64)
            )
        )
        interval_start_hours.append(lead - 1)

    assert lat is not None and lon is not None and x is not None and y is not None
    assert nbm_crs is not None and subset is not None
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
        crs_wkt2=nbm_crs.to_wkt(),
        retained_subset=subset,
    )
