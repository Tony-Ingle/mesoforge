from __future__ import annotations

from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mesoforge.verification.metrics import compute_verification_report_v2
from tests.contracts.test_verification_v2 import _row

pytestmark = pytest.mark.scientific

ART = "art_00000000-0000-0000-0000-000000000001"


@given(
    temperature_state=st.sampled_from(("complete", "fallback", "unavailable", "inconsistent")),
    pop_state=st.sampled_from(("complete", "fallback", "unavailable", "inconsistent")),
)
def test_availability_strata_are_independent_per_metric(
    temperature_state: str, pop_state: str
) -> None:
    seed = _row()
    midnight = seed.valid_time.replace(hour=0)
    rank = {"complete": 0, "fallback": 1, "unavailable": 2, "inconsistent": 3}
    aggregate_state = max((temperature_state, pop_state), key=rank.__getitem__)
    rows = [
        _row(
            station_id=station,
            target_horizon_hours=horizon,
            valid_time=midnight + timedelta(hours=horizon),
            precipitation_interval_start=midnight + timedelta(hours=horizon - 1),
            precipitation_interval_end=midnight + timedelta(hours=horizon),
            availability_state=aggregate_state,
            temperature_availability_state=temperature_state,
            pop_availability_state=pop_state,
        )
        for station in ("station.kcbg", "station.kjmr", "station.kros")
        for horizon in range(1, 37)
    ]
    report = compute_verification_report_v2(
        rows,
        metric_set_id="phase2-multimodel-station.v1",
        baseline_artifact_id=ART,
        matched_pairs_artifact_id=ART,
    )
    strata = {
        (row.stratum_value, row.metric_name): sum(row.missing_counts.values())
        for row in report.rows
        if row.stratum_kind == "by_availability_state"
    }
    assert strata[(temperature_state, "temperature_mae")] == 108
    assert strata[(pop_state, "pop_brier_score")] == 108
    if temperature_state != pop_state:
        assert strata[(pop_state, "temperature_mae")] == 0
        assert strata[(temperature_state, "pop_brier_score")] == 0
