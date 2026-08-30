"""``metar-nearest-15m.v1`` as-of forecast/observation matching (plan
Section 3.8, Task 10).

Pure computation over an already-assembled/validated baseline dataset
and a list of normalized observations. No storage/network import.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import xarray as xr

from mesoforge.catalog.configuration import MatchingPolicy
from mesoforge.common.identifiers import ArtifactId, StationId
from mesoforge.contracts.observations import NormalizedObservation
from mesoforge.contracts.verification import FieldStatus, MatchedPairRow, RowStatus

_STATION_METADATA_CONFLICT_FLAG = "station_metadata_conflict"


def _forecast_values(
    baseline: xr.Dataset, *, station_id: StationId, lead_hours: int
) -> dict[str, float | None]:
    selection = baseline.sel(location=str(station_id)).isel(
        lead_time=lead_hours
    )  # lead_time index == lead hour by construction (0..6, 1h steps)
    values: dict[str, float | None] = {}
    for variable_id in (
        "air_temperature_2m",
        "eastward_wind_10m",
        "northward_wind_10m",
        "wind_speed_10m",
        "wind_from_direction_10m",
    ):
        raw_value = float(selection[variable_id].values)
        values[variable_id] = raw_value if math.isfinite(raw_value) else None
    return values


def _valid_time_at_lead(baseline: xr.Dataset, *, lead_hours: int) -> datetime:
    """The baseline's per-lead ``valid_time`` coordinate, already
    computed at assembly time (plan Section 3.6), converted to an
    aware UTC ``datetime``."""
    scalar = baseline["valid_time"].isel(lead_time=lead_hours).values
    naive: datetime = scalar.astype("datetime64[us]").item()
    return naive.replace(tzinfo=UTC)


def _forecast_is_valid(values: dict[str, float | None]) -> bool:
    return (
        values["air_temperature_2m"] is not None
        and values["eastward_wind_10m"] is not None
        and values["northward_wind_10m"] is not None
    )


def _select_best_observation(
    *,
    station_id: StationId,
    valid_time: datetime,
    observations: list[NormalizedObservation],
    tolerance: timedelta,
    verification_cutoff: datetime,
) -> tuple[NormalizedObservation | None, float | None, bool]:
    """Returns ``(selected, delta_seconds, any_within_tolerance)``.
    ``any_within_tolerance`` distinguishes "no report within tolerance"
    from "revision after cutoff" when candidates exist but none has an
    eligible (as-of-cutoff) revision."""
    candidates = [
        observation
        for observation in observations
        if observation.station_id == station_id
        and abs((observation.event_time - valid_time).total_seconds()) <= tolerance.total_seconds()
    ]
    if not candidates:
        return None, None, False

    by_logical: dict[str, list[NormalizedObservation]] = {}
    for observation in candidates:
        by_logical.setdefault(str(observation.logical_observation_digest), []).append(observation)

    selected_per_logical: list[NormalizedObservation] = []
    for revisions in by_logical.values():
        eligible = [
            revision
            for revision in revisions
            if revision.provider_available_at <= verification_cutoff
            and revision.ingested_at <= verification_cutoff
        ]
        if not eligible:
            continue
        latest = max(eligible, key=lambda r: (r.provider_available_at, str(r.revision_digest)))
        selected_per_logical.append(latest)

    if not selected_per_logical:
        return None, None, True

    def _sort_key(observation: NormalizedObservation) -> tuple[float, datetime, str]:
        delta = abs((observation.event_time - valid_time).total_seconds())
        return (delta, observation.event_time, str(observation.logical_observation_digest))

    best = min(selected_per_logical, key=_sort_key)
    delta_seconds = abs((best.event_time - valid_time).total_seconds())
    return best, delta_seconds, True


def _field_statuses(
    *,
    forecast: dict[str, float | None],
    observation: NormalizedObservation | None,
    calm_threshold_m_s: float,
) -> dict[str, FieldStatus]:
    if observation is None:
        raise AssertionError("_field_statuses requires a selected observation")

    statuses: dict[str, FieldStatus] = {}

    statuses["temperature"] = (
        "matched" if observation.temperature_k is not None else "temperature_missing"
    )

    if observation.wind_speed_m_s is None:
        statuses["wind_speed"] = "wind_speed_missing"
        statuses["eastward_component"] = "wind_speed_missing"
        statuses["northward_component"] = "wind_speed_missing"
        statuses["wind_direction"] = "wind_speed_missing"
        return statuses

    statuses["wind_speed"] = "matched"

    if observation.eastward_wind_10m_m_s is None or observation.northward_wind_10m_m_s is None:
        statuses["eastward_component"] = "wind_direction_missing_or_variable"
        statuses["northward_component"] = "wind_direction_missing_or_variable"
    else:
        statuses["eastward_component"] = "matched"
        statuses["northward_component"] = "matched"

    if observation.wind_from_direction_degrees is None:
        statuses["wind_direction"] = "wind_direction_missing_or_variable"
    else:
        forecast_speed = forecast["wind_speed_10m"]
        observed_speed = observation.wind_speed_m_s
        if (
            forecast_speed is None
            or forecast_speed < calm_threshold_m_s
            or observed_speed < calm_threshold_m_s
        ):
            statuses["wind_direction"] = "calm_direction_excluded"
        else:
            statuses["wind_direction"] = "matched"

    return statuses


def match_baseline_to_observations(
    *,
    baseline: xr.Dataset,
    station_ids: tuple[StationId, ...],
    lead_hours: tuple[int, ...],
    observations: list[NormalizedObservation],
    matching_policy: MatchingPolicy,
    verification_cutoff: datetime,
    baseline_artifact_id: ArtifactId,
    observations_artifact_id: ArtifactId,
) -> list[MatchedPairRow]:
    """Section 3.8: one row per expected ``(station, lead)`` pair in
    canonical order, regardless of whether a matching observation was
    found."""
    tolerance = timedelta(minutes=matching_policy.tolerance_minutes)

    rows: list[MatchedPairRow] = []
    for station_id in station_ids:
        for lead in lead_hours:
            valid_time = _valid_time_at_lead(baseline, lead_hours=lead)
            forecast = _forecast_values(baseline, station_id=station_id, lead_hours=lead)

            if not _forecast_is_valid(forecast):
                all_status: FieldStatus = "forecast_missing_or_invalid"
                rows.append(
                    _build_row(
                        station_id=station_id,
                        lead=lead,
                        valid_time=valid_time,
                        forecast=forecast,
                        observation=None,
                        delta_seconds=None,
                        field_statuses=dict.fromkeys(
                            (
                                "temperature",
                                "wind_speed",
                                "eastward_component",
                                "northward_component",
                                "wind_direction",
                            ),
                            all_status,
                        ),
                        matching_policy=matching_policy,
                        verification_cutoff=verification_cutoff,
                        baseline_artifact_id=baseline_artifact_id,
                        observations_artifact_id=observations_artifact_id,
                    )
                )
                continue

            selected, delta_seconds, any_within_tolerance = _select_best_observation(
                station_id=station_id,
                valid_time=valid_time,
                observations=observations,
                tolerance=tolerance,
                verification_cutoff=verification_cutoff,
            )

            if selected is None:
                blanket_status: FieldStatus = (
                    "revision_after_cutoff"
                    if any_within_tolerance
                    else "no_report_within_tolerance"
                )
                rows.append(
                    _build_row(
                        station_id=station_id,
                        lead=lead,
                        valid_time=valid_time,
                        forecast=forecast,
                        observation=None,
                        delta_seconds=None,
                        field_statuses=dict.fromkeys(
                            (
                                "temperature",
                                "wind_speed",
                                "eastward_component",
                                "northward_component",
                                "wind_direction",
                            ),
                            blanket_status,
                        ),
                        matching_policy=matching_policy,
                        verification_cutoff=verification_cutoff,
                        baseline_artifact_id=baseline_artifact_id,
                        observations_artifact_id=observations_artifact_id,
                    )
                )
                continue

            if _STATION_METADATA_CONFLICT_FLAG in selected.quality_flags:
                conflict_status: FieldStatus = "station_metadata_conflict"
                rows.append(
                    _build_row(
                        station_id=station_id,
                        lead=lead,
                        valid_time=valid_time,
                        forecast=forecast,
                        observation=selected,
                        delta_seconds=delta_seconds,
                        field_statuses=dict.fromkeys(
                            (
                                "temperature",
                                "wind_speed",
                                "eastward_component",
                                "northward_component",
                                "wind_direction",
                            ),
                            conflict_status,
                        ),
                        matching_policy=matching_policy,
                        verification_cutoff=verification_cutoff,
                        baseline_artifact_id=baseline_artifact_id,
                        observations_artifact_id=observations_artifact_id,
                    )
                )
                continue

            if selected.mesoforge_qc_state == "rejected":
                rejected_status: FieldStatus = "observation_qc_rejected"
                rows.append(
                    _build_row(
                        station_id=station_id,
                        lead=lead,
                        valid_time=valid_time,
                        forecast=forecast,
                        observation=selected,
                        delta_seconds=delta_seconds,
                        field_statuses=dict.fromkeys(
                            (
                                "temperature",
                                "wind_speed",
                                "eastward_component",
                                "northward_component",
                                "wind_direction",
                            ),
                            rejected_status,
                        ),
                        matching_policy=matching_policy,
                        verification_cutoff=verification_cutoff,
                        baseline_artifact_id=baseline_artifact_id,
                        observations_artifact_id=observations_artifact_id,
                    )
                )
                continue

            field_statuses = _field_statuses(
                forecast=forecast,
                observation=selected,
                calm_threshold_m_s=matching_policy.calm_threshold_m_s,
            )
            rows.append(
                _build_row(
                    station_id=station_id,
                    lead=lead,
                    valid_time=valid_time,
                    forecast=forecast,
                    observation=selected,
                    delta_seconds=delta_seconds,
                    field_statuses=field_statuses,
                    matching_policy=matching_policy,
                    verification_cutoff=verification_cutoff,
                    baseline_artifact_id=baseline_artifact_id,
                    observations_artifact_id=observations_artifact_id,
                )
            )

    return rows


def _build_row(
    *,
    station_id: StationId,
    lead: int,
    valid_time: datetime,
    forecast: dict[str, float | None],
    observation: NormalizedObservation | None,
    delta_seconds: float | None,
    field_statuses: dict[str, FieldStatus],
    matching_policy: MatchingPolicy,
    verification_cutoff: datetime,
    baseline_artifact_id: ArtifactId,
    observations_artifact_id: ArtifactId,
) -> MatchedPairRow:
    row_status: RowStatus = (
        "matched_any_field" if "matched" in field_statuses.values() else "matched_no_fields"
    )
    return MatchedPairRow(
        station_id=station_id,
        lead_hours=lead,
        valid_time=valid_time,
        baseline_artifact_id=baseline_artifact_id,
        observations_artifact_id=observations_artifact_id,
        matching_policy_id=matching_policy.matching_policy_id,
        matching_policy_digest=matching_policy.digest,
        verification_cutoff=verification_cutoff,
        selected_logical_observation_digest=(
            observation.logical_observation_digest if observation is not None else None
        ),
        selected_revision_digest=observation.revision_digest if observation is not None else None,
        selected_event_time=observation.event_time if observation is not None else None,
        selected_provider_available_at=(
            observation.provider_available_at if observation is not None else None
        ),
        delta_seconds=delta_seconds,
        forecast_temperature_k=forecast["air_temperature_2m"],
        forecast_eastward_wind_m_s=forecast["eastward_wind_10m"],
        forecast_northward_wind_m_s=forecast["northward_wind_10m"],
        forecast_wind_speed_m_s=forecast["wind_speed_10m"],
        forecast_wind_from_direction_degrees=forecast["wind_from_direction_10m"],
        observed_temperature_k=observation.temperature_k if observation is not None else None,
        observed_eastward_wind_m_s=(
            observation.eastward_wind_10m_m_s if observation is not None else None
        ),
        observed_northward_wind_m_s=(
            observation.northward_wind_10m_m_s if observation is not None else None
        ),
        observed_wind_speed_m_s=observation.wind_speed_m_s if observation is not None else None,
        observed_wind_from_direction_degrees=(
            observation.wind_from_direction_degrees if observation is not None else None
        ),
        row_status=row_status,
        temperature_status=field_statuses["temperature"],
        eastward_component_status=field_statuses["eastward_component"],
        northward_component_status=field_statuses["northward_component"],
        wind_speed_status=field_statuses["wind_speed"],
        wind_direction_status=field_statuses["wind_direction"],
    )
