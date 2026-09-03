"""Phase 2 METAR normalization extension (plan Section 6.1, Task 11):
dew point (C->K), explicit wind gust (knots->m/s), and raw routine
``Prrrr`` hourly liquid precipitation (hundredths of an inch -> kg/m2).

Pure computation -- no network/storage import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import jcs

from mesoforge.catalog.configuration import ObservationNormalizationPolicy
from mesoforge.catalog.stations import StationRecord
from mesoforge.common.errors import MesoForgeError
from mesoforge.common.identifiers import ArtifactId, Digest, StationId
from mesoforge.contracts.observations import RawMetarRecord
from mesoforge.contracts.observations_v2 import NormalizedObservationV2, RawMetarRecordV2
from mesoforge.observations.normalization import (
    MetarNormalizationError as MetarNormalizationError,
)
from mesoforge.observations.normalization import (
    compute_logical_observation_digest,
    compute_revision_digest,
    convert_temperature_c_to_k,
    convert_wind_speed_knots_to_m_s,
    derive_wind_components,
)
from mesoforge.observations.quality import (
    direction_in_range,
    receipt_not_too_early,
    station_metadata_within_tolerance,
    temperature_in_range,
    wind_speed_in_range,
)

_KNOTS_TO_M_S = 0.5144444444444445
_INCH_HUNDREDTHS_TO_MM = 0.254  # 0.01 inch * 25.4 mm/inch
_PRRRR_PATTERN = re.compile(r"(?<![A-Za-z0-9])P(\d{4})(?![A-Za-z0-9])")


class PrecipitationParsingError(MesoForgeError):
    """Raised when a syntactically present ``Prrrr``-shaped token
    cannot be safely parsed (should not normally occur given the
    regex; retained for defense-in-depth)."""


def convert_dew_point_c_to_k(dew_point_degc: float) -> float:
    return dew_point_degc + 273.15


def convert_gust_knots_to_m_s(gust_knots: float) -> float:
    return gust_knots * _KNOTS_TO_M_S


@dataclass(frozen=True, slots=True)
class PrecipitationTruth:
    status: Literal["reported", "missing", "malformed"]
    amount_kg_m2: float | None
    interval_start: datetime | None
    interval_end: datetime | None


def extract_hourly_precipitation(
    *, raw_ob: str, metar_type: str, report_time: datetime
) -> PrecipitationTruth:
    """Section 6.1: accept only a non-SPECI routine report with an
    explicit syntactically valid ``Prrrr`` group representing the hour
    ending at the report time. ``rrrr`` is hundredths of an inch;
    convert ``rrrr * 0.01 inch * 25.4 = rrrr * 0.254`` to kg/m2 (kg/m2
    numerically equals mm of liquid water depth). Missing ``Prrrr`` is
    missing truth, never zero. A SPECI report never contributes
    precipitation truth even if it happens to carry a ``Prrrr`` group.
    """
    if metar_type.upper() == "SPECI":
        return PrecipitationTruth(
            status="missing", amount_kg_m2=None, interval_start=None, interval_end=None
        )

    matches = _PRRRR_PATTERN.findall(raw_ob)
    if not matches:
        return PrecipitationTruth(
            status="missing", amount_kg_m2=None, interval_start=None, interval_end=None
        )
    if len(matches) > 1:
        return PrecipitationTruth(
            status="malformed", amount_kg_m2=None, interval_start=None, interval_end=None
        )

    hundredths = int(matches[0])
    amount_mm = hundredths * _INCH_HUNDREDTHS_TO_MM
    interval_end = report_time
    interval_start = report_time - timedelta(hours=1)
    return PrecipitationTruth(
        status="reported",
        amount_kg_m2=amount_mm,
        interval_start=interval_start,
        interval_end=interval_end,
    )


def normalize_metar_record_v2(
    *,
    raw: RawMetarRecordV2,
    station: StationRecord,
    station_id: StationId,
    policy: ObservationNormalizationPolicy,
    raw_artifact_id: ArtifactId,
    raw_record_index: int,
    station_snapshot_artifact_id: ArtifactId,
    ingested_at: datetime,
    query_window_start: datetime,
    query_window_end: datetime,
) -> NormalizedObservationV2:
    """Section 3.7/6.1: Phase 2 METAR normalization -- adds dew point
    (C->K), explicit wind gust (knots->m/s), and hourly liquid
    precipitation truth (``extract_hourly_precipitation``) to the
    Phase 1 rules (station identity, event-window, receipt timing,
    metadata tolerance, temperature/wind range checks). Never raises
    for an out-of-range/missing value; only a genuinely impossible
    station mismatch or a receipt-before-event violation is a full
    rejection."""
    flags: list[str] = []

    logical_digest = compute_logical_observation_digest(
        station_id=station_id, event_time=raw.obs_time
    )
    canonical_record = raw.model_dump(mode="json")
    revision_digest = compute_revision_digest(
        logical_observation_digest=logical_digest,
        receipt_time=raw.receipt_time,
        canonical_provider_record=canonical_record,
    )

    if raw.icao_id != station.provider_icao_id:
        raise MetarNormalizationError(
            f"record icao_id {raw.icao_id!r} does not match expected station "
            f"{station.provider_icao_id!r}"
        )
    if raw.obs_time < query_window_start or raw.obs_time > query_window_end:
        raise MetarNormalizationError(
            f"record event time {raw.obs_time!r} lies outside the raw query window "
            f"[{query_window_start!r}, {query_window_end!r}]"
        )
    if not receipt_not_too_early(
        event_time_epoch_s=raw.obs_time.timestamp(),
        receipt_time_epoch_s=raw.receipt_time.timestamp(),
        policy=policy,
    ):
        raise MetarNormalizationError(
            f"receipt_time {raw.receipt_time!r} precedes event {raw.obs_time!r} by more than "
            f"{policy.max_receipt_before_event_minutes} minutes"
        )

    metadata_ok = station_metadata_within_tolerance(
        record_latitude=raw.lat,
        record_longitude=raw.lon,
        record_elevation_m=raw.elev,
        snapshot_latitude=station.latitude,
        snapshot_longitude=station.longitude,
        snapshot_elevation_m=station.elevation_m,
        policy=policy,
    )
    truth = extract_hourly_precipitation(
        raw_ob=raw.raw_ob, metar_type=raw.metar_type, report_time=raw.obs_time
    )
    if not metadata_ok:
        return NormalizedObservationV2(
            logical_observation_digest=logical_digest,
            revision_digest=revision_digest,
            station_id=station_id,
            provider_station_id=raw.icao_id,
            event_time=raw.obs_time,
            report_time=raw.report_time,
            provider_available_at=raw.receipt_time,
            ingested_at=ingested_at,
            metar_type=raw.metar_type,
            raw_observation=raw.raw_ob,
            raw_record_digest=Digest.of_bytes(_canonical_bytes(canonical_record)),
            raw_artifact_id=raw_artifact_id,
            raw_record_index=raw_record_index,
            station_snapshot_artifact_id=station_snapshot_artifact_id,
            latitude_degrees=raw.lat,
            longitude_degrees=raw.lon,
            elevation_m=raw.elev,
            provider_qc_field=raw.qc_field,
            mesoforge_qc_state="rejected",
            quality_flags=("station_metadata_conflict",),
        )

    temperature_k: float | None = None
    if raw.temp is not None:
        converted = convert_temperature_c_to_k(raw.temp)
        if temperature_in_range(converted, policy):
            temperature_k = converted
        else:
            flags.append("temperature_out_of_range")

    dew_point_k: float | None = None
    if raw.dewp is not None:
        converted_dew = convert_dew_point_c_to_k(raw.dewp)
        if temperature_in_range(converted_dew, policy):
            dew_point_k = converted_dew
        else:
            flags.append("dew_point_out_of_range")

    wind_gust_m_s: float | None = None
    if raw.wgst is not None:
        wind_gust_m_s = convert_gust_knots_to_m_s(raw.wgst)

    wind_speed_m_s: float | None = None
    wind_from_direction_degrees: float | None = None
    eastward: float | None = None
    northward: float | None = None

    if raw.wspd is not None:
        converted_speed = convert_wind_speed_knots_to_m_s(raw.wspd)
        if wind_speed_in_range(converted_speed, policy):
            wind_speed_m_s = converted_speed
            if converted_speed == 0.0:
                eastward = 0.0
                northward = 0.0
            elif raw.wdir == "VRB":
                flags.append("variable_wind_direction")
            elif raw.wdir is None:
                flags.append("wind_direction_missing")
            elif isinstance(raw.wdir, float) and direction_in_range(raw.wdir):
                # METAR reports due north as 360, not 0 (the same
                # convention operational NBM WDIR uses). The canonical
                # contract is the half-open [0, 360) circle, so 360 is
                # canonicalized to 0 before it is retained or used.
                # Doing it exactly here -- rather than trusting
                # ``math.radians(360.0)`` -- avoids leaking a spurious
                # -2.4e-16 * speed eastward component into a due north
                # wind, and keeps the retained direction inside the
                # bound that ``matched-pairs.v2`` enforces.
                canonical_direction = 0.0 if raw.wdir == 360.0 else raw.wdir
                wind_from_direction_degrees = canonical_direction
                eastward, northward = derive_wind_components(
                    wind_speed_m_s=converted_speed, direction_degrees=canonical_direction
                )
            else:
                flags.append("wind_direction_out_of_range")
        else:
            flags.append("wind_speed_out_of_range")
    if raw.wspd is None:
        flags.append("wind_speed_missing")

    if temperature_k is None and wind_speed_m_s is None:
        qc_state: Literal["eligible", "partial", "rejected"] = (
            "rejected" if (raw.temp is None and raw.wspd is None) else "partial"
        )
    elif temperature_k is None or wind_speed_m_s is None:
        qc_state = "partial"
    else:
        qc_state = "eligible"

    return NormalizedObservationV2(
        logical_observation_digest=logical_digest,
        revision_digest=revision_digest,
        station_id=station_id,
        provider_station_id=raw.icao_id,
        event_time=raw.obs_time,
        report_time=raw.report_time,
        provider_available_at=raw.receipt_time,
        ingested_at=ingested_at,
        metar_type=raw.metar_type,
        raw_observation=raw.raw_ob,
        raw_record_digest=Digest.of_bytes(_canonical_bytes(canonical_record)),
        raw_artifact_id=raw_artifact_id,
        raw_record_index=raw_record_index,
        station_snapshot_artifact_id=station_snapshot_artifact_id,
        latitude_degrees=raw.lat,
        longitude_degrees=raw.lon,
        elevation_m=raw.elev,
        temperature_k=temperature_k,
        dew_point_k=dew_point_k,
        wind_speed_m_s=wind_speed_m_s,
        wind_from_direction_degrees=wind_from_direction_degrees,
        eastward_wind_10m_m_s=eastward,
        northward_wind_10m_m_s=northward,
        wind_gust_m_s=wind_gust_m_s,
        precipitation_amount_kg_m2=truth.amount_kg_m2,
        precipitation_truth_status=truth.status,
        precipitation_interval_start=truth.interval_start,
        precipitation_interval_end=truth.interval_end,
        provider_qc_field=raw.qc_field,
        mesoforge_qc_state=qc_state,
        quality_flags=tuple(sorted(set(flags))),
    )


def _canonical_bytes(value: dict[str, object]) -> bytes:
    return bytes(jcs.canonicalize(value))


__all__ = [
    "PrecipitationParsingError",
    "PrecipitationTruth",
    "RawMetarRecord",
    "convert_dew_point_c_to_k",
    "convert_gust_knots_to_m_s",
    "extract_hourly_precipitation",
    "normalize_metar_record_v2",
]
