"""Deterministic MesoForge QC range/completeness checks (plan Section
3.7, Task 9).

Named, deterministic checks only -- the opaque provider ``qcField`` is
preserved as lineage metadata but never itself accepted or rejected by
these rules (plan: "do not reject merely because qcField is nonzero").
"""

from __future__ import annotations

import math

from mesoforge.catalog.configuration import ObservationNormalizationPolicy


def temperature_in_range(temperature_k: float, policy: ObservationNormalizationPolicy) -> bool:
    return (
        math.isfinite(temperature_k)
        and policy.temperature_valid_min_k <= temperature_k <= policy.temperature_valid_max_k
    )


def wind_speed_in_range(wind_speed_m_s: float, policy: ObservationNormalizationPolicy) -> bool:
    return (
        math.isfinite(wind_speed_m_s)
        and policy.wind_speed_valid_min_m_s <= wind_speed_m_s <= policy.wind_speed_valid_max_m_s
    )


def direction_in_range(direction_degrees: float) -> bool:
    return math.isfinite(direction_degrees) and 0.0 <= direction_degrees <= 360.0


def receipt_not_too_early(
    *,
    event_time_epoch_s: float,
    receipt_time_epoch_s: float,
    policy: ObservationNormalizationPolicy,
) -> bool:
    """Section 3.7: reject receipt before event by more than 5 minutes
    (a provider revision cannot arrive meaningfully before the event it
    describes)."""
    delta_minutes = (event_time_epoch_s - receipt_time_epoch_s) / 60.0
    return delta_minutes <= policy.max_receipt_before_event_minutes


def station_metadata_within_tolerance(
    *,
    record_latitude: float,
    record_longitude: float,
    record_elevation_m: float,
    snapshot_latitude: float,
    snapshot_longitude: float,
    snapshot_elevation_m: float,
    policy: ObservationNormalizationPolicy,
) -> bool:
    """Section 3.2/3.7: a per-record lat/lon/elev differing from the
    effective station snapshot by more than the configured tolerance
    rejects that observation revision with ``station_metadata_conflict``
    (never silently moves the forecast point)."""
    lat_ok = abs(record_latitude - snapshot_latitude) <= policy.station_coordinate_tolerance_degrees
    lon_ok = (
        abs(record_longitude - snapshot_longitude) <= policy.station_coordinate_tolerance_degrees
    )
    elev_ok = abs(record_elevation_m - snapshot_elevation_m) <= policy.station_elevation_tolerance_m
    return lat_ok and lon_ok and elev_ok
