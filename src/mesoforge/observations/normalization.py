"""METAR observation normalization (plan Section 3.1/3.7, Task 9):
unit conversion, digest computation, and per-field QC state derivation.

Pure computation over already-decoded ``RawMetarRecord`` values plus
the effective station snapshot -- no network/storage import.
"""

from __future__ import annotations

import math
from datetime import datetime

import jcs

from mesoforge.catalog.configuration import ObservationNormalizationPolicy
from mesoforge.catalog.stations import StationRecord
from mesoforge.common.errors import MesoForgeError
from mesoforge.common.identifiers import ArtifactId, Digest, StationId
from mesoforge.contracts.observations import MesoForgeQcState, NormalizedObservation, RawMetarRecord
from mesoforge.observations.quality import (
    direction_in_range,
    receipt_not_too_early,
    station_metadata_within_tolerance,
    temperature_in_range,
    wind_speed_in_range,
)

_KNOTS_TO_M_S = 0.5144444444444445


class MetarNormalizationError(MesoForgeError):
    """Raised when a raw METAR record cannot be matched to the expected
    station set or fails an unconditional structural check."""


def compute_logical_observation_digest(*, station_id: StationId, event_time: datetime) -> Digest:
    """Section 3.1: ``SHA256(JCS(["aviationweather.gov", station_id,
    obs_time]))``."""
    payload = ["aviationweather.gov", str(station_id), event_time.isoformat()]
    return Digest.of_bytes(jcs.canonicalize(payload))


def compute_revision_digest(
    *,
    logical_observation_digest: Digest,
    receipt_time: datetime,
    canonical_provider_record: dict[str, object],
) -> Digest:
    """Section 3.1: ``SHA256(JCS([logical_observation_digest,
    receipt_time, canonical_provider_record]))``."""
    payload = [str(logical_observation_digest), receipt_time.isoformat(), canonical_provider_record]
    return Digest.of_bytes(jcs.canonicalize(payload))


def convert_temperature_c_to_k(temperature_degc: float) -> float:
    return temperature_degc + 273.15


def convert_wind_speed_knots_to_m_s(wind_speed_knots: float) -> float:
    return wind_speed_knots * _KNOTS_TO_M_S


def derive_wind_components(
    *, wind_speed_m_s: float, direction_degrees: float
) -> tuple[float, float]:
    """Section 3.7: ``u = -speed*sin(direction)``, ``v =
    -speed*cos(direction)``."""
    radians = math.radians(direction_degrees)
    return (-wind_speed_m_s * math.sin(radians), -wind_speed_m_s * math.cos(radians))


def normalize_metar_record(
    *,
    raw: RawMetarRecord,
    station: StationRecord,
    station_id: StationId,
    policy: ObservationNormalizationPolicy,
    raw_artifact_id: ArtifactId,
    raw_record_index: int,
    station_snapshot_artifact_id: ArtifactId,
    ingested_at: datetime,
    query_window_start: datetime,
    query_window_end: datetime,
) -> NormalizedObservation:
    """Section 3.7 normalization rules. Never raises for an
    out-of-range/missing value -- those produce ``mesoforge_qc_state``
    and null fields; only a genuinely impossible station mismatch or a
    receipt-before-event violation is treated as full rejection."""
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
    if not metadata_ok:
        return NormalizedObservation(
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
            raw_record_digest=Digest.of_bytes(jcs.canonicalize(canonical_record)),
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

    wind_speed_m_s: float | None = None
    wind_from_direction_degrees: float | None = None
    eastward: float | None = None
    northward: float | None = None

    if raw.wspd is not None:
        converted_speed = convert_wind_speed_knots_to_m_s(raw.wspd)
        if wind_speed_in_range(converted_speed, policy):
            wind_speed_m_s = converted_speed
            if converted_speed == 0.0:
                # Section 3.7: wspd == 0 -> speed/U/V are zero; direction
                # is null/undefined even if source supplies one.
                eastward = 0.0
                northward = 0.0
            elif raw.wdir == "VRB":
                flags.append("variable_wind_direction")
            elif raw.wdir is None:
                flags.append("wind_direction_missing")
            elif isinstance(raw.wdir, float) and direction_in_range(raw.wdir):
                wind_from_direction_degrees = raw.wdir
                eastward, northward = derive_wind_components(
                    wind_speed_m_s=converted_speed, direction_degrees=raw.wdir
                )
            else:
                flags.append("wind_direction_out_of_range")
        else:
            flags.append("wind_speed_out_of_range")

    if raw.wspd is None:
        flags.append("wind_speed_missing")

    qc_state: MesoForgeQcState = _derive_qc_state(
        temperature_k=temperature_k, wind_speed_m_s=wind_speed_m_s, raw=raw
    )

    return NormalizedObservation(
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
        raw_record_digest=Digest.of_bytes(jcs.canonicalize(canonical_record)),
        raw_artifact_id=raw_artifact_id,
        raw_record_index=raw_record_index,
        station_snapshot_artifact_id=station_snapshot_artifact_id,
        latitude_degrees=raw.lat,
        longitude_degrees=raw.lon,
        elevation_m=raw.elev,
        temperature_k=temperature_k,
        wind_speed_m_s=wind_speed_m_s,
        wind_from_direction_degrees=wind_from_direction_degrees,
        eastward_wind_10m_m_s=eastward,
        northward_wind_10m_m_s=northward,
        provider_qc_field=raw.qc_field,
        mesoforge_qc_state=qc_state,
        quality_flags=tuple(sorted(set(flags))),
    )


def _derive_qc_state(
    *, temperature_k: float | None, wind_speed_m_s: float | None, raw: RawMetarRecord
) -> MesoForgeQcState:
    if temperature_k is None and wind_speed_m_s is None:
        if raw.temp is None and raw.wspd is None:
            return "rejected"
        return "partial"
    if temperature_k is None or wind_speed_m_s is None:
        return "partial"
    return "eligible"
