"""Matched-pairs/metric-report scientific validation."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from mesoforge.contracts.verification import MatchedPairRowV2, VerificationReportV2

_V2_STATIONS = ("station.kcbg", "station.kjmr", "station.kros")
_V2_KEYS = {(station, horizon) for station in _V2_STATIONS for horizon in range(1, 37)}


def validate_matched_pairs_v2(rows: Sequence[MatchedPairRowV2]) -> None:
    """Validate complete, unique, lineage-consistent Phase 2 coverage."""
    if len(rows) != 108:
        raise ValueError(f"matched-pairs.v2 must contain exactly 108 rows, got {len(rows)}")
    for row in rows:
        MatchedPairRowV2.model_validate(row.model_dump())
    keys = {(str(row.station_id), row.target_horizon_hours) for row in rows}
    if keys != _V2_KEYS:
        raise ValueError("matched-pairs.v2 keys must equal the canonical 3 stations x 36 horizons")
    identity = {
        (
            row.baseline_artifact_id,
            row.observations_artifact_id,
            row.matching_policy_id,
            row.matching_policy_digest,
            row.verification_cutoff,
        )
        for row in rows
    }
    if len(identity) != 1:
        raise ValueError("matched-pairs.v2 rows must share artifact, policy, and cutoff identity")


def _assert_finite_json(value: object, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"verification-report.v2 contains nonfinite JSON number at {path}")
    if isinstance(value, Mapping):
        for key, member in value.items():
            _assert_finite_json(member, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, member in enumerate(value):
            _assert_finite_json(member, f"{path}[{index}]")


def validate_verification_report_v2(report: VerificationReportV2) -> None:
    """Validate required strata, metric parity, and recursive JSON finiteness."""
    kinds = {row.stratum_kind for row in report.rows}
    required = {"overall", "by_target_horizon", "by_station", "by_availability_state"}
    missing = required - kinds
    if missing:
        label = "availability" if "by_availability_state" in missing else sorted(missing)[0]
        raise ValueError(f"verification-report.v2 missing required {label} stratum")
    horizons = {row.stratum_value for row in report.rows if row.stratum_kind == "by_target_horizon"}
    stations = {row.stratum_value for row in report.rows if row.stratum_kind == "by_station"}
    if horizons != set(range(1, 37)) or stations != set(_V2_STATIONS):
        raise ValueError("verification-report.v2 must cover all target horizons and stations")
    overall_names = {row.metric_name for row in report.rows if row.stratum_kind == "overall"}
    for row in report.rows:
        if row.metric_name not in overall_names:
            raise ValueError("every stratum must use the same metric set as overall")
    _assert_finite_json(report.model_dump(mode="json"))
