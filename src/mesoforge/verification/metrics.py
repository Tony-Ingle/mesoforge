"""``phase1-temperature-wind.v1`` verification metric computation (plan
Section 3.9, Task 11).

Pure computation over already-matched ``MatchedPairRow`` sequences. No
storage/network import.
"""

from __future__ import annotations

import math

from mesoforge.catalog.configuration import MetricSet
from mesoforge.common.identifiers import ArtifactId
from mesoforge.contracts.verification import (
    FieldStatus,
    MatchedPairRow,
    MetricRow,
    StratumKind,
    VerificationReport,
)

_MISSING_REASON_MAP: dict[FieldStatus, str] = {
    "forecast_missing_or_invalid": "forecast_missing_or_invalid",
    "no_report_within_tolerance": "no_report_within_tolerance",
    "revision_after_cutoff": "revision_after_cutoff",
    "station_metadata_conflict": "station_metadata_conflict",
    "observation_qc_rejected": "observation_qc_rejected",
    "temperature_missing": "field_missing",
    "wind_speed_missing": "field_missing",
    "wind_direction_missing_or_variable": "field_missing",
    "calm_direction_excluded": "calm_direction_excluded",
}

_SCALAR_METRIC_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("temperature", "forecast_temperature_k", "observed_temperature_k"),
    ("eastward_wind", "forecast_eastward_wind_m_s", "observed_eastward_wind_m_s"),
    ("northward_wind", "forecast_northward_wind_m_s", "observed_northward_wind_m_s"),
    ("wind_speed", "forecast_wind_speed_m_s", "observed_wind_speed_m_s"),
)

_SCALAR_STATUS_FIELDS: dict[str, str] = {
    "temperature": "temperature_status",
    "eastward_wind": "eastward_component_status",
    "northward_wind": "northward_component_status",
    "wind_speed": "wind_speed_status",
}


def _missing_counts_dict(rows: list[MatchedPairRow], status_field: str) -> dict[str, int]:
    counts: dict[str, int] = {
        "forecast_missing_or_invalid": 0,
        "no_report_within_tolerance": 0,
        "revision_after_cutoff": 0,
        "station_metadata_conflict": 0,
        "observation_qc_rejected": 0,
        "field_missing": 0,
        "calm_direction_excluded": 0,
    }
    for row in rows:
        status: FieldStatus = getattr(row, status_field)
        if status == "matched":
            continue
        counts[_MISSING_REASON_MAP[status]] += 1
    return counts


def _compute_scalar_metrics(errors: list[float]) -> tuple[float | None, float | None, float | None]:
    """Section 3.9: ``bias = sum(error)/n``, ``mae = sum(abs(error))/n``,
    ``rmse = sqrt(sum(error**2)/n)`` in float64. ``n == 0`` -> all
    None. Rejects nonfinite inputs outright rather than silently
    excluding them (nan-aware aggregation is explicitly disallowed)."""
    if not errors:
        return None, None, None
    if not all(math.isfinite(error) for error in errors):
        raise ValueError(
            "verification metric inputs must be finite; no nonfinite value is permitted"
        )
    n = len(errors)
    bias = sum(errors) / n
    mae = sum(abs(e) for e in errors) / n
    rmse = math.sqrt(sum(e * e for e in errors) / n)
    return bias, mae, rmse


def _compute_direction_metric(errors_degrees: list[float]) -> float | None:
    """Section 3.9: arithmetic mean of the absolute signed circular
    difference in degrees."""
    if not errors_degrees:
        return None
    return sum(errors_degrees) / len(errors_degrees)


def _signed_circular_diff_degrees(forecast_degrees: float, observed_degrees: float) -> float:
    return ((forecast_degrees - observed_degrees + 180.0) % 360.0) - 180.0


def _compute_metrics_for_rows(
    rows: list[MatchedPairRow],
    *,
    metric_set: MetricSet,
    stratum_kind: StratumKind,
    stratum_lead_hours: int | None,
    stratum_station_id: str | None,
    baseline_artifact_id: ArtifactId,
    matched_pairs_artifact_id: ArtifactId,
) -> list[MetricRow]:
    metric_rows: list[MetricRow] = []

    if not rows:
        return metric_rows

    matching_policy_digest = rows[0].matching_policy_digest
    matching_policy_id = rows[0].matching_policy_id
    verification_cutoff = rows[0].verification_cutoff

    unit_ids = {
        "temperature": "K",
        "eastward_wind": "m/s",
        "northward_wind": "m/s",
        "wind_speed": "m/s",
    }

    for field_name, forecast_attr, observed_attr in _SCALAR_METRIC_FIELDS:
        status_field = _SCALAR_STATUS_FIELDS[field_name]
        errors = [
            getattr(row, forecast_attr) - getattr(row, observed_attr)
            for row in rows
            if getattr(row, status_field) == "matched"
        ]
        bias, mae, rmse = _compute_scalar_metrics(errors)
        sample_count = len(errors)
        missing_counts = _missing_counts_dict(rows, status_field)

        for metric_suffix, value in (("bias", bias), ("mae", mae), ("rmse", rmse)):
            metric_rows.append(
                MetricRow(
                    metric_set_id=metric_set.metric_set_id,
                    metric_name=f"{field_name}_{metric_suffix}",  # type: ignore[arg-type]
                    unit_id=unit_ids[field_name],
                    stratum_kind=stratum_kind,
                    stratum_lead_hours=stratum_lead_hours,
                    stratum_station_id=stratum_station_id,  # type: ignore[arg-type]
                    value=value,
                    sample_count=sample_count,
                    missing_counts=missing_counts,  # type: ignore[arg-type]
                    baseline_artifact_id=baseline_artifact_id,
                    matched_pairs_artifact_id=matched_pairs_artifact_id,
                    matching_policy_digest=matching_policy_digest,
                    matching_policy_id=matching_policy_id,
                    verification_cutoff=verification_cutoff,
                )
            )

    direction_errors = [
        abs(
            _signed_circular_diff_degrees(
                row.forecast_wind_from_direction_degrees, row.observed_wind_from_direction_degrees
            )
        )
        for row in rows
        if row.wind_direction_status == "matched"
        and row.forecast_wind_from_direction_degrees is not None
        and row.observed_wind_from_direction_degrees is not None
    ]
    direction_value = _compute_direction_metric(direction_errors)
    metric_rows.append(
        MetricRow(
            metric_set_id=metric_set.metric_set_id,
            metric_name="wind_direction_mean_absolute_circular_error",
            unit_id="degree",
            stratum_kind=stratum_kind,
            stratum_lead_hours=stratum_lead_hours,
            stratum_station_id=stratum_station_id,  # type: ignore[arg-type]
            value=direction_value,
            sample_count=len(direction_errors),
            missing_counts=_missing_counts_dict(rows, "wind_direction_status"),  # type: ignore[arg-type]
            baseline_artifact_id=baseline_artifact_id,
            matched_pairs_artifact_id=matched_pairs_artifact_id,
            matching_policy_digest=matching_policy_digest,
            matching_policy_id=matching_policy_id,
            verification_cutoff=verification_cutoff,
        )
    )

    return metric_rows


def compute_verification_report(
    rows: list[MatchedPairRow],
    *,
    metric_set: MetricSet,
    station_ids: tuple[str, ...],
    lead_hours: tuple[int, ...],
    baseline_artifact_id: ArtifactId,
    matched_pairs_artifact_id: ArtifactId,
) -> VerificationReport:
    """Section 3.9: overall, by-lead, and by-station strata, each
    containing every metric in ``metric_set``."""
    all_rows: list[MetricRow] = []

    all_rows.extend(
        _compute_metrics_for_rows(
            rows,
            metric_set=metric_set,
            stratum_kind="overall",
            stratum_lead_hours=None,
            stratum_station_id=None,
            baseline_artifact_id=baseline_artifact_id,
            matched_pairs_artifact_id=matched_pairs_artifact_id,
        )
    )

    for lead in lead_hours:
        lead_rows = [row for row in rows if row.lead_hours == lead]
        all_rows.extend(
            _compute_metrics_for_rows(
                lead_rows,
                metric_set=metric_set,
                stratum_kind="by_lead",
                stratum_lead_hours=lead,
                stratum_station_id=None,
                baseline_artifact_id=baseline_artifact_id,
                matched_pairs_artifact_id=matched_pairs_artifact_id,
            )
        )

    for station_id in station_ids:
        station_rows = [row for row in rows if str(row.station_id) == station_id]
        all_rows.extend(
            _compute_metrics_for_rows(
                station_rows,
                metric_set=metric_set,
                stratum_kind="by_station",
                stratum_lead_hours=None,
                stratum_station_id=station_id,
                baseline_artifact_id=baseline_artifact_id,
                matched_pairs_artifact_id=matched_pairs_artifact_id,
            )
        )

    return VerificationReport(rows=tuple(all_rows))
