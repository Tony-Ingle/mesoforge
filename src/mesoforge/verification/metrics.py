"""``phase1-temperature-wind.v1`` verification metric computation (plan
Section 3.9, Task 11).

Pure computation over already-matched ``MatchedPairRow`` sequences. No
storage/network import.
"""

from __future__ import annotations

import math
from dataclasses import asdict

from mesoforge.catalog.configuration import MetricSet
from mesoforge.common.identifiers import ArtifactId, MetricSetId
from mesoforge.contracts.verification import (
    FieldStatus,
    MatchedPairRow,
    MatchedPairRowV2,
    MetricRow,
    MetricRowV2,
    StratumKind,
    StratumKindV2,
    VerificationReport,
    VerificationReportV2,
)
from mesoforge.verification.qpf_pop_metrics import (
    compute_brier_decomposition,
    compute_brier_score,
    compute_contingency_counts,
    compute_contingency_metrics,
    compute_decile_reliability_bins,
    compute_roc_auc,
)
from mesoforge.verification.validation import validate_matched_pairs_v2

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


_V2_MISSING_REASONS = (
    "forecast_missing_or_invalid",
    "no_report_within_tolerance",
    "revision_after_cutoff",
    "station_metadata_conflict",
    "observation_qc_rejected",
    "field_missing",
    "calm_direction_excluded",
    "precipitation_interval_mismatch",
)
_V2_CONTINUOUS_FIELDS = (
    ("temperature", "forecast_temperature_k", "observed_temperature_k", "K"),
    ("dew_point", "forecast_dew_point_k", "observed_dew_point_k", "K"),
    ("eastward_wind", "forecast_eastward_wind_m_s", "observed_eastward_wind_m_s", "m/s"),
    ("northward_wind", "forecast_northward_wind_m_s", "observed_northward_wind_m_s", "m/s"),
    ("wind_speed", "forecast_wind_speed_m_s", "observed_wind_speed_m_s", "m/s"),
    ("gust", "forecast_wind_gust_m_s", "observed_wind_gust_m_s", "m/s"),
    ("qpf", "forecast_qpf_kg_m2", "observed_qpf_kg_m2", "kg/m2"),
)
_V2_STATUS_FIELD = {
    "temperature": "temperature_status",
    "dew_point": "dew_point_status",
    "eastward_wind": "eastward_component_status",
    "northward_wind": "northward_component_status",
    "wind_speed": "wind_speed_status",
    "gust": "gust_status",
    "qpf": "qpf_status",
    "pop": "pop_status",
    "wind_direction": "wind_direction_status",
}


def _v2_missing(rows: list[MatchedPairRowV2], field: str) -> dict[str, int]:
    result = dict.fromkeys(_V2_MISSING_REASONS, 0)
    for row in rows:
        status = getattr(row, _V2_STATUS_FIELD[field])
        if status != "matched":
            result[status] += 1
    return result


def _metric_row_v2(
    *,
    name: str,
    unit: str,
    kind: StratumKindV2,
    stratum_value: str | int | None,
    value: float | None,
    null_reason: str | None,
    sample: int,
    missing: dict[str, int],
    details: dict[str, object] | None = None,
    gust: bool = False,
) -> MetricRowV2:
    return MetricRowV2(
        metric_name=name,
        unit_id=unit,
        stratum_kind=kind,
        stratum_value=stratum_value,
        value=value,
        null_reason=null_reason,
        sample_count=sample,
        missing_counts=missing,
        details=details or {},
        label="conditional_on_reported_gust" if gust else None,
    )


def _metrics_for_rows_v2(
    rows: list[MatchedPairRowV2],
    *,
    kind: StratumKindV2,
    stratum_value: str | int | None,
) -> list[MetricRowV2]:
    output: list[MetricRowV2] = []
    for field, forecast_attr, observed_attr, unit in _V2_CONTINUOUS_FIELDS:
        errors: list[float] = []
        for row in rows:
            if getattr(row, _V2_STATUS_FIELD[field]) != "matched":
                continue
            forecast, observed = getattr(row, forecast_attr), getattr(row, observed_attr)
            if (
                forecast is None
                or observed is None
                or not (math.isfinite(forecast) and math.isfinite(observed))
            ):
                raise ValueError(f"matched {field} inputs must be finite and non-null")
            errors.append(forecast - observed)
        bias, mae, rmse = _compute_scalar_metrics(errors)
        missing = _v2_missing(rows, field)
        for suffix, value in (("bias", bias), ("mae", mae), ("rmse", rmse)):
            output.append(
                _metric_row_v2(
                    name=f"{field}_{suffix}",
                    unit=unit,
                    kind=kind,
                    stratum_value=stratum_value,
                    value=value,
                    null_reason=None if value is not None else "no_matched_samples",
                    sample=len(errors),
                    missing=missing,
                    gust=field == "gust",
                )
            )

    direction_errors: list[float] = []
    for row in rows:
        if row.wind_direction_status == "matched":
            forecast = row.forecast_wind_from_direction_degrees
            observed = row.observed_wind_from_direction_degrees
            if (
                forecast is None
                or observed is None
                or not all(map(math.isfinite, (forecast, observed)))
            ):
                raise ValueError("matched wind direction inputs must be finite and non-null")
            direction_errors.append(abs(_signed_circular_diff_degrees(forecast, observed)))
    direction = _compute_direction_metric(direction_errors)
    output.append(
        _metric_row_v2(
            name="wind_direction_mean_absolute_circular_error",
            unit="degree",
            kind=kind,
            stratum_value=stratum_value,
            value=direction,
            null_reason=None if direction is not None else "no_matched_samples",
            sample=len(direction_errors),
            missing=_v2_missing(rows, "wind_direction"),
        )
    )

    qpf_pairs = [
        (row.forecast_qpf_kg_m2, row.observed_qpf_kg_m2)
        for row in rows
        if row.qpf_status == "matched"
    ]
    checked_qpf: list[tuple[float, float]] = []
    for forecast, observed in qpf_pairs:
        if forecast is None or observed is None:
            raise ValueError("matched QPF inputs must be non-null")
        checked_qpf.append((forecast, observed))
    for threshold in (0.254, 2.54):
        contingency = compute_contingency_metrics(
            compute_contingency_counts(checked_qpf, threshold_kg_m2=threshold)
        )
        details = {"threshold_kg_m2": threshold, **asdict(contingency.counts)}
        for name in ("csi", "pod", "far", "frequency_bias", "ets"):
            output.append(
                _metric_row_v2(
                    name=f"qpf_{name}_at_{threshold}",
                    unit="1",
                    kind=kind,
                    stratum_value=stratum_value,
                    value=getattr(contingency, name),
                    null_reason=getattr(contingency, f"{name}_null_reason"),
                    sample=len(checked_qpf),
                    missing=_v2_missing(rows, "qpf"),
                    details=details,
                )
            )

    pop_pairs: list[tuple[float, float]] = []
    for row in rows:
        if row.pop_status != "matched":
            continue
        if row.forecast_pop_probability is None or row.observed_qpf_kg_m2 is None:
            raise ValueError("matched PoP inputs must be non-null")
        pop_pairs.append((row.forecast_pop_probability, float(row.observed_qpf_kg_m2 >= 0.254)))
    brier = compute_brier_score(pop_pairs)
    bins = compute_decile_reliability_bins(pop_pairs)
    output.append(
        _metric_row_v2(
            name="pop_brier_score",
            unit="1",
            kind=kind,
            stratum_value=stratum_value,
            value=brier,
            null_reason=None if brier is not None else "no_matched_samples",
            sample=len(pop_pairs),
            missing=_v2_missing(rows, "pop"),
            details={"bins": [asdict(bin_) for bin_ in bins]},
        )
    )
    decomposition = compute_brier_decomposition(bins, total_sample=len(pop_pairs))
    for component in ("reliability", "resolution", "uncertainty"):
        value = getattr(decomposition.value, component) if decomposition.value else None
        output.append(
            _metric_row_v2(
                name=f"pop_brier_{component}",
                unit="1",
                kind=kind,
                stratum_value=stratum_value,
                value=value,
                null_reason=decomposition.null_reason,
                sample=len(pop_pairs),
                missing=_v2_missing(rows, "pop"),
            )
        )
    auc = compute_roc_auc(pop_pairs)
    output.append(
        _metric_row_v2(
            name="pop_roc_auc",
            unit="1",
            kind=kind,
            stratum_value=stratum_value,
            value=auc.value,
            null_reason=auc.null_reason,
            sample=len(pop_pairs),
            missing=_v2_missing(rows, "pop"),
        )
    )
    return output


def compute_verification_report_v2(
    rows: list[MatchedPairRowV2],
    *,
    metric_set_id: str,
    baseline_artifact_id: ArtifactId,
    matched_pairs_artifact_id: ArtifactId,
) -> VerificationReportV2:
    """Compute all Section 6.2 metrics in all four Phase 2 strata."""
    if not rows:
        raise ValueError("matched-pairs.v2 input must not be empty")
    validate_matched_pairs_v2(rows)
    keys = [(str(row.station_id), row.target_horizon_hours) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("matched-pairs.v2 contains duplicate station/horizon rows")
    report_rows: list[MetricRowV2] = []
    strata: list[tuple[StratumKindV2, str | int | None, list[MatchedPairRowV2]]] = [
        ("overall", None, rows)
    ]
    horizons = sorted({row.target_horizon_hours for row in rows})
    strata.extend(
        ("by_target_horizon", horizon, [row for row in rows if row.target_horizon_hours == horizon])
        for horizon in horizons
    )
    strata.extend(
        ("by_station", station, [r for r in rows if str(r.station_id) == station])
        for station in sorted({str(r.station_id) for r in rows})
    )
    strata.extend(
        ("by_availability_state", state, [r for r in rows if r.availability_state == state])
        for state in sorted({r.availability_state for r in rows})
    )
    for kind, value, subset in strata:
        report_rows.extend(_metrics_for_rows_v2(subset, kind=kind, stratum_value=value))
    first = rows[0]
    return VerificationReportV2(
        metric_set_id=MetricSetId(metric_set_id),
        baseline_artifact_id=baseline_artifact_id,
        matched_pairs_artifact_id=matched_pairs_artifact_id,
        matching_policy_id=first.matching_policy_id,
        matching_policy_digest=first.matching_policy_digest,
        verification_cutoff=first.verification_cutoff,
        rows=tuple(report_rows),
    )
