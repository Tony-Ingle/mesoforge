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
from mesoforge.contracts.observations_v2 import NormalizedObservationV2
from mesoforge.contracts.verification import (
    AvailabilityStateV2,
    FieldStatus,
    FieldStatusV2,
    MatchedPairRow,
    MatchedPairRowV2,
    RowStatus,
)

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


_V2_STATIONS = ("station.kcbg", "station.kjmr", "station.kros")
_V2_HORIZONS = tuple(range(1, 37))
_STATE_NAMES: dict[int, AvailabilityStateV2] = {
    0: "complete",
    1: "fallback",
    2: "unavailable",
    3: "inconsistent",
}


def _datetime_from_numpy(value: object) -> datetime:
    naive: datetime = value.astype("datetime64[us]").item()  # type: ignore[attr-defined]
    return naive.replace(tzinfo=UTC)


def _select_observation_v2(
    *,
    station_id: StationId,
    valid_time: datetime,
    observations: list[NormalizedObservationV2],
    tolerance: timedelta,
    verification_cutoff: datetime,
) -> tuple[NormalizedObservationV2 | None, bool]:
    candidates = [
        o
        for o in observations
        if o.station_id == station_id
        and abs((o.event_time - valid_time).total_seconds()) <= tolerance.total_seconds()
    ]
    if not candidates:
        return None, False
    by_logical: dict[str, list[NormalizedObservationV2]] = {}
    for candidate in candidates:
        by_logical.setdefault(str(candidate.logical_observation_digest), []).append(candidate)
    eligible_revisions: list[NormalizedObservationV2] = []
    for revisions in by_logical.values():
        eligible = [
            o
            for o in revisions
            if o.provider_available_at <= verification_cutoff
            and o.ingested_at <= verification_cutoff
        ]
        if eligible:
            eligible_revisions.append(
                max(
                    eligible,
                    key=lambda o: (o.provider_available_at, o.ingested_at, str(o.revision_digest)),
                )
            )
    if not eligible_revisions:
        return None, True
    return min(
        eligible_revisions,
        key=lambda o: (
            abs((o.event_time - valid_time).total_seconds()),
            o.event_time,
            str(o.logical_observation_digest),
        ),
    ), True


def match_baseline_to_observations_v2(
    *,
    baseline: xr.Dataset,
    station_ids: tuple[str, ...],
    target_horizons: tuple[int, ...],
    observations: list[NormalizedObservationV2],
    matching_policy: MatchingPolicy,
    verification_cutoff: datetime,
    baseline_artifact_id: ArtifactId,
    observations_artifact_id: ArtifactId,
) -> list[MatchedPairRowV2]:
    """Build the exact 3 x 36 ``matched-pairs.v2`` coverage frame."""
    if station_ids != _V2_STATIONS or target_horizons != _V2_HORIZONS:
        raise ValueError("Phase 2 matching requires canonical 3 stations x horizons 1..36")
    if (
        tuple(str(v) for v in baseline.location.values) != station_ids
        or tuple(int(v) for v in baseline.target_horizon.values) != target_horizons
    ):
        raise ValueError("baseline coordinate frame does not match requested Phase 2 frame")
    tolerance = timedelta(minutes=matching_policy.tolerance_minutes)
    rows: list[MatchedPairRowV2] = []
    variable_map = {
        "temperature": "air_temperature_2m",
        "dew_point": "dew_point_temperature_2m",
        "eastward_component": "eastward_wind_10m",
        "northward_component": "northward_wind_10m",
        "gust": "wind_gust_10m",
        "qpf": "liquid_equivalent_precipitation_amount_1h",
        "pop": "probability_of_precipitation_1h",
    }
    state_rank = {"complete": 0, "fallback": 1, "unavailable": 2, "inconsistent": 3}
    for station_id in station_ids:
        for horizon in target_horizons:
            selection = baseline.sel(location=station_id, target_horizon=horizon)
            valid_time = _datetime_from_numpy(selection.target_valid_time.values)
            forecast: dict[str, float | None] = {}
            states: list[AvailabilityStateV2] = []
            for field, variable in variable_map.items():
                value = float(selection[variable].values)
                forecast[field] = value if math.isfinite(value) else None
                code = int(selection[f"{variable}_state"].values)
                if code not in _STATE_NAMES:
                    raise ValueError(f"invalid availability state code {code} for {variable}")
                states.append(_STATE_NAMES[code])
            u, v = forecast["eastward_component"], forecast["northward_component"]
            if u is None or v is None:
                forecast["wind_speed"] = forecast["wind_direction"] = None
            else:
                speed = math.hypot(u, v)
                forecast["wind_speed"] = speed
                forecast["wind_direction"] = (
                    None if speed == 0 else (math.degrees(math.atan2(-u, -v)) % 360.0)
                )
            availability = max(states, key=state_rank.__getitem__)
            selected, had_candidate = _select_observation_v2(
                station_id=StationId(station_id),
                valid_time=valid_time,
                observations=observations,
                tolerance=tolerance,
                verification_cutoff=verification_cutoff,
            )
            blanket: FieldStatusV2 = (
                "revision_after_cutoff" if had_candidate else "no_report_within_tolerance"
            )
            statuses: dict[str, FieldStatusV2] = {
                name: blanket
                for name in (
                    "temperature",
                    "dew_point",
                    "eastward_component",
                    "northward_component",
                    "wind_speed",
                    "wind_direction",
                    "gust",
                    "qpf",
                    "pop",
                )
            }
            observed: dict[str, float | None] = {name: None for name in statuses}
            if selected is not None:
                if _STATION_METADATA_CONFLICT_FLAG in selected.quality_flags:
                    statuses = dict.fromkeys(statuses, "station_metadata_conflict")
                elif selected.mesoforge_qc_state == "rejected":
                    statuses = dict.fromkeys(statuses, "observation_qc_rejected")
                else:
                    observed.update(
                        {
                            "temperature": selected.temperature_k,
                            "dew_point": selected.dew_point_k,
                            "eastward_component": selected.eastward_wind_10m_m_s,
                            "northward_component": selected.northward_wind_10m_m_s,
                            "wind_speed": selected.wind_speed_m_s,
                            "wind_direction": selected.wind_from_direction_degrees,
                            "gust": selected.wind_gust_m_s,
                            "qpf": selected.precipitation_amount_kg_m2,
                        }
                    )
                    for name in (
                        "temperature",
                        "dew_point",
                        "eastward_component",
                        "northward_component",
                        "wind_speed",
                        "gust",
                    ):
                        if forecast[name] is None:
                            statuses[name] = "forecast_missing_or_invalid"
                        else:
                            statuses[name] = (
                                "matched" if observed[name] is not None else "field_missing"
                            )
                    if forecast["wind_direction"] is None or observed["wind_direction"] is None:
                        statuses["wind_direction"] = "field_missing"
                    elif (forecast["wind_speed"] or 0) < matching_policy.calm_threshold_m_s or (
                        observed["wind_speed"] or 0
                    ) < matching_policy.calm_threshold_m_s:
                        statuses["wind_direction"] = "calm_direction_excluded"
                    else:
                        statuses["wind_direction"] = "matched"
                    interval_ok = (
                        selected.precipitation_truth_status == "reported"
                        and selected.precipitation_interval_start is not None
                        and selected.precipitation_interval_end is not None
                        and selected.precipitation_interval_end
                        - selected.precipitation_interval_start
                        == timedelta(hours=1)
                        and abs((selected.precipitation_interval_end - valid_time).total_seconds())
                        <= tolerance.total_seconds()
                    )
                    if interval_ok:
                        statuses["qpf"] = (
                            "matched"
                            if forecast["qpf"] is not None
                            else "forecast_missing_or_invalid"
                        )
                        statuses["pop"] = (
                            "matched"
                            if forecast["pop"] is not None
                            else "forecast_missing_or_invalid"
                        )
                    else:
                        statuses["qpf"] = statuses["pop"] = "precipitation_interval_mismatch"
            for name in statuses:
                if forecast.get(name) is None and statuses[name] == "matched":
                    statuses[name] = "forecast_missing_or_invalid"
            row_status: RowStatus = (
                "matched_any_field" if "matched" in statuses.values() else "matched_no_fields"
            )
            rows.append(
                MatchedPairRowV2(
                    station_id=StationId(station_id),
                    target_horizon_hours=horizon,
                    valid_time=valid_time,
                    precipitation_interval_start=valid_time - timedelta(hours=1),
                    precipitation_interval_end=valid_time,
                    baseline_artifact_id=baseline_artifact_id,
                    observations_artifact_id=observations_artifact_id,
                    matching_policy_id=matching_policy.matching_policy_id,
                    matching_policy_digest=matching_policy.digest,
                    verification_cutoff=verification_cutoff,
                    availability_state=availability,
                    selected_logical_observation_digest=(
                        selected.logical_observation_digest if selected else None
                    ),
                    selected_revision_digest=(selected.revision_digest if selected else None),
                    selected_event_time=(selected.event_time if selected else None),
                    selected_provider_available_at=(
                        selected.provider_available_at if selected else None
                    ),
                    delta_seconds=(
                        abs((selected.event_time - valid_time).total_seconds())
                        if selected
                        else None
                    ),
                    forecast_temperature_k=forecast["temperature"],
                    forecast_dew_point_k=forecast["dew_point"],
                    forecast_eastward_wind_m_s=forecast["eastward_component"],
                    forecast_northward_wind_m_s=forecast["northward_component"],
                    forecast_wind_speed_m_s=forecast["wind_speed"],
                    forecast_wind_from_direction_degrees=forecast["wind_direction"],
                    forecast_wind_gust_m_s=forecast["gust"],
                    forecast_qpf_kg_m2=forecast["qpf"],
                    forecast_pop_probability=forecast["pop"],
                    observed_temperature_k=observed["temperature"],
                    observed_dew_point_k=observed["dew_point"],
                    observed_eastward_wind_m_s=observed["eastward_component"],
                    observed_northward_wind_m_s=observed["northward_component"],
                    observed_wind_speed_m_s=observed["wind_speed"],
                    observed_wind_from_direction_degrees=observed["wind_direction"],
                    observed_wind_gust_m_s=observed["gust"],
                    observed_qpf_kg_m2=observed["qpf"],
                    row_status=row_status,
                    temperature_status=statuses["temperature"],
                    dew_point_status=statuses["dew_point"],
                    eastward_component_status=statuses["eastward_component"],
                    northward_component_status=statuses["northward_component"],
                    wind_speed_status=statuses["wind_speed"],
                    wind_direction_status=statuses["wind_direction"],
                    gust_status=statuses["gust"],
                    qpf_status=statuses["qpf"],
                    pop_status=statuses["pop"],
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
