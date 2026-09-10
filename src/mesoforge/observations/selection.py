"""Read-only temperature matching over retained METAR rows and station metadata."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from pyproj import Geod

from mesoforge.catalog.configuration import ObservationNormalizationPolicy
from mesoforge.catalog.stations import StationDefinition
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.contracts.observations_v2 import NormalizedObservationV2
from mesoforge.observations.quality import (
    receipt_not_too_early,
    station_metadata_within_tolerance,
    temperature_in_range,
)

_GEOD = Geod(ellps="WGS84")
_MAX_DISTANCE_KM = 50.0
_MAX_TIME_DIFFERENCE_SECONDS = 15 * 60


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _station_candidate(
    station: StationDefinition | None,
    *,
    snapshot_id: ArtifactId,
    latitude: float,
    longitude: float,
) -> dict[str, Any]:
    distance = None
    if station is not None:
        _, _, meters = _GEOD.inv(
            longitude, latitude, station.expected_longitude, station.expected_latitude
        )
        distance = meters / 1000.0
    return {
        "station_id": None if station is None else station.provider_icao_id,
        "catalog_station_id": None if station is None else str(station.station_id),
        "network": "METAR",
        "provider": "aviationweather.gov",
        "latitude": None if station is None else station.expected_latitude,
        "longitude": None if station is None else station.expected_longitude,
        "elevation_m": None if station is None else station.expected_elevation_m,
        "distance_km": distance,
        "observation_time": None,
        "temperature": {"value": None, "unit": "K"},
        "time_difference_seconds": None,
        "qc": {
            "state": None,
            "flags": [],
            "provider_qc_field": None,
            "temperature_eligible": False,
        },
        "status": "excluded",
        "reasons": [],
        "provenance": {"station_snapshot_artifact_id": str(snapshot_id)},
    }


def _observation_candidate(
    observation: NormalizedObservationV2,
    *,
    latitude: float,
    longitude: float,
    valid_time: datetime,
    station_snapshots: dict[ArtifactId, tuple[StationDefinition, ...]],
    policy: ObservationNormalizationPolicy,
) -> dict[str, Any]:
    snapshot = station_snapshots.get(observation.station_snapshot_artifact_id)
    station = next((s for s in snapshot or () if s.station_id == observation.station_id), None)
    candidate = _station_candidate(
        station,
        snapshot_id=observation.station_snapshot_artifact_id,
        latitude=latitude,
        longitude=longitude,
    )
    # Report the observed identity even if the referenced metadata cannot be matched.
    candidate["station_id"] = observation.provider_station_id
    candidate["catalog_station_id"] = str(observation.station_id)
    delta = (observation.event_time - valid_time).total_seconds()
    candidate["observation_time"] = _utc(observation.event_time)
    candidate["time_difference_seconds"] = delta
    candidate["temperature"]["value"] = observation.temperature_k
    candidate["provenance"].update(
        {
            "raw_artifact_id": str(observation.raw_artifact_id),
            "raw_record_index": observation.raw_record_index,
            "raw_record_digest": str(observation.raw_record_digest),
            "logical_observation_digest": str(observation.logical_observation_digest),
            "revision_digest": str(observation.revision_digest),
            "report_time": _utc(observation.report_time),
            "provider_available_at": _utc(observation.provider_available_at),
            "ingested_at": _utc(observation.ingested_at),
        }
    )
    qc_reasons: list[str] = []
    if snapshot is None:
        qc_reasons.append("station_snapshot_unavailable")
    elif station is None:
        qc_reasons.append("station_not_in_snapshot")
    elif station.provider_icao_id != observation.provider_station_id:
        qc_reasons.append("station_identity_mismatch")
    elif not station_metadata_within_tolerance(
        record_latitude=observation.latitude_degrees,
        record_longitude=observation.longitude_degrees,
        record_elevation_m=observation.elevation_m,
        snapshot_latitude=station.expected_latitude,
        snapshot_longitude=station.expected_longitude,
        snapshot_elevation_m=station.expected_elevation_m,
        policy=policy,
    ):
        qc_reasons.append("station_metadata_conflict")
    if observation.mesoforge_qc_state == "rejected":
        qc_reasons.append("qc_rejected")
    if not receipt_not_too_early(
        event_time_epoch_s=observation.event_time.timestamp(),
        receipt_time_epoch_s=observation.provider_available_at.timestamp(),
        policy=policy,
    ):
        qc_reasons.append("receipt_before_event")
    if observation.temperature_k is None:
        qc_reasons.append("temperature_missing")
    elif not temperature_in_range(observation.temperature_k, policy):
        qc_reasons.append("temperature_out_of_range")
    # A valid temperature can coexist with an unrelated partial wind/dew-point QC result.
    qc_reasons.extend(
        flag
        for flag in observation.quality_flags
        if flag in {"temperature_out_of_range", "station_metadata_conflict"}
    )
    candidate["qc"] = {
        "state": observation.mesoforge_qc_state,
        "flags": list(observation.quality_flags),
        "provider_qc_field": observation.provider_qc_field,
        "temperature_eligible": not qc_reasons,
    }
    reasons = qc_reasons.copy()
    if candidate["distance_km"] is not None and candidate["distance_km"] > _MAX_DISTANCE_KM:
        reasons.append("outside_50_km")
    if abs(delta) > _MAX_TIME_DIFFERENCE_SECONDS:
        reasons.append("outside_15_minute_window")
    candidate["reasons"] = sorted(set(reasons))
    if not reasons:
        candidate["status"] = "eligible_not_selected"
    return candidate


def _rank(candidate: dict[str, Any]) -> tuple[Any, ...]:
    return (
        candidate["distance_km"],
        abs(candidate["time_difference_seconds"]),
        candidate["station_id"],
        candidate["observation_time"],
        candidate["provenance"]["revision_digest"],
        candidate["provenance"]["station_snapshot_artifact_id"],
    )


def select_temperature_observation(
    *,
    latitude: float,
    longitude: float,
    valid_time: datetime,
    observations: list[NormalizedObservationV2],
    station_snapshots: dict[ArtifactId, tuple[StationDefinition, ...]],
    policy: ObservationNormalizationPolicy,
) -> dict[str, Any]:
    """Choose a temperature proxy within inclusive 50 km / +/-15 minute limits.

    Only the newest retained revision of each logical observation participates.
    Revision selection precedes QC so a rejected correction cannot resurrect an
    older, eligible value. No forecast error or verification record is produced.
    """
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValueError("latitude must be finite and within [-90, 90]")
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValueError("longitude must be finite and within [-180, 180]")
    if valid_time.tzinfo is None or valid_time.utcoffset() is None:
        raise ValueError("valid_time must include a timezone")
    latest: dict[Digest, NormalizedObservationV2] = {}
    for observation in observations:
        previous = latest.get(observation.logical_observation_digest)
        if previous is None or (
            observation.provider_available_at,
            observation.ingested_at,
            observation.revision_digest,
        ) > (previous.provider_available_at, previous.ingested_at, previous.revision_digest):
            latest[observation.logical_observation_digest] = observation

    candidates = [
        _observation_candidate(
            observation,
            latitude=latitude,
            longitude=longitude,
            valid_time=valid_time,
            station_snapshots=station_snapshots,
            policy=policy,
        )
        for observation in latest.values()
    ]
    represented = {(o.station_snapshot_artifact_id, o.station_id) for o in latest.values()}
    for snapshot_id, stations in station_snapshots.items():
        for station in stations:
            if (snapshot_id, station.station_id) in represented:
                continue
            candidate = _station_candidate(
                station, snapshot_id=snapshot_id, latitude=latitude, longitude=longitude
            )
            candidate["reasons"] = ["no_retained_observation"]
            if candidate["distance_km"] > _MAX_DISTANCE_KM:
                candidate["reasons"].append("outside_50_km")
            candidates.append(candidate)
    candidates.sort(
        key=lambda c: (
            c["station_id"],
            c["provenance"]["station_snapshot_artifact_id"],
            c["observation_time"] or "",
            c["provenance"].get("revision_digest", ""),
        )
    )
    eligible = [c for c in candidates if c["status"] == "eligible_not_selected"]
    selected = min(eligible, key=_rank) if eligible else None
    if selected is not None:
        selected["status"] = "selected"
        selected["selection_reason"] = (
            "Nearest eligible station; ties use closest observation time, then station ID. "
            "Remaining ties use earlier observation time, then revision digest."
        )
    return {
        "status": "matched" if selected is not None else "unavailable",
        "unavailable_reason": None
        if selected is not None
        else "No retained temperature observation passed station metadata, QC, 50 km, "
        "and +/-15 minute requirements.",
        "selected": selected,
        "candidates": candidates,
    }
