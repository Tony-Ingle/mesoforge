from __future__ import annotations

from datetime import timedelta

import pytest

from mesoforge.verification.metrics import compute_verification_report_v2
from mesoforge.verification.validation import (
    validate_matched_pairs_v2,
    validate_verification_report_v2,
)
from tests.contracts.test_verification_v2 import _row

ART = "art_00000000-0000-0000-0000-000000000001"
STATIONS = ("station.kcbg", "station.kjmr", "station.kros")


def _coverage():
    return [
        _row(
            station_id=station,
            target_horizon_hours=horizon,
            valid_time=_row().valid_time.replace(hour=0) + timedelta(hours=horizon),
            precipitation_interval_start=_row().valid_time.replace(hour=0)
            + timedelta(hours=horizon - 1),
            precipitation_interval_end=_row().valid_time.replace(hour=0) + timedelta(hours=horizon),
        )
        for station in STATIONS
        for horizon in range(1, 37)
    ]


def test_matched_pairs_validator_requires_exact_canonical_coverage() -> None:
    validate_matched_pairs_v2(_coverage())
    with pytest.raises(ValueError, match="108"):
        validate_matched_pairs_v2(_coverage()[:-1])
    duplicated = _coverage()
    duplicated[-1] = duplicated[0]
    with pytest.raises(ValueError, match="canonical"):
        validate_matched_pairs_v2(duplicated)


def test_report_validator_requires_every_planned_stratum_and_finite_json() -> None:
    rows = _coverage()
    report = compute_verification_report_v2(
        rows,
        metric_set_id="phase2-multimodel-station.v1",
        baseline_artifact_id=ART,
        matched_pairs_artifact_id=ART,
    )
    validate_verification_report_v2(report)
    bad = report.model_copy(
        update={
            "rows": tuple(row for row in report.rows if row.stratum_kind != "by_availability_state")
        }
    )
    with pytest.raises(ValueError, match="availability"):
        validate_verification_report_v2(bad)
