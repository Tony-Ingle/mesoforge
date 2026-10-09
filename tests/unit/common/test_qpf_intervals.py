"""Native QPF events are a partition, never arbitrary hourly redistributions."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.common.qpf_intervals import summarize_qpf_intervals, validate_qpf_intervals

START = datetime(2026, 10, 8, 15, tzinfo=UTC)


def events(start=START, *, coarse_start=36):
    rows = []
    lead = 0
    while lead < 120:
        duration = min(3 if lead >= coarse_start else 1, 120 - lead)
        rows.append(
            {
                "interval_start": (start + timedelta(hours=lead)).isoformat(),
                "interval_end": (start + timedelta(hours=lead + duration)).isoformat(),
                "interval_closure": "left_open_right_closed",
                "unit": "kg/m^2",
                "value": 0.0 if lead < coarse_start else 3.0,
                "accumulation_duration_hours": duration,
            }
        )
        lead += duration
    return rows


def test_exact_partition_zero_missing_and_crossing_events_are_distinct():
    rows = events(coarse_start=23)
    original = deepcopy(rows)
    end = START + timedelta(hours=120)
    assert validate_qpf_intervals(rows, start=START, end=end) == original
    horizon = summarize_qpf_intervals(rows, start=START, end=end)
    assert horizon["complete"] and horizon["total_kg_m2"] == 99
    dry = summarize_qpf_intervals(rows, start=START, end=START + timedelta(hours=23))
    assert dry["complete"] and dry["total_kg_m2"] == 0
    card = summarize_qpf_intervals(rows, start=START, end=START + timedelta(hours=24))
    assert not card["complete"] and card["total_kg_m2"] is None
    assert card["boundary_crossing_events"] == 1
    assert card["sum_of_available_contained_events_kg_m2"] == 0
    assert rows == original
    rows[-1]["value"] = None
    assert summarize_qpf_intervals(rows, start=START, end=end)["total_kg_m2"] is None


@pytest.mark.parametrize(
    "defect",
    [
        "gap",
        "overlap",
        "wrong_unit",
        "wrong_closure",
        "wrong_duration",
        "negative",
        "nan",
        "missing_value",
        "partial",
    ],
)
def test_invalid_partition_rejected(defect):
    rows = events()
    if defect == "gap":
        rows.pop(10)
    elif defect == "overlap":
        rows.insert(10, deepcopy(rows[10]))
    elif defect == "wrong_unit":
        rows[0]["unit"] = "inches"
    elif defect == "wrong_closure":
        rows[0]["interval_closure"] = "left_closed_right_open"
    elif defect == "wrong_duration":
        rows[0]["accumulation_duration_hours"] = 3
    elif defect == "negative":
        rows[0]["value"] = -1
    elif defect == "nan":
        rows[0]["value"] = float("nan")
    elif defect == "missing_value":
        del rows[0]["value"]
    else:
        rows.pop()
    with pytest.raises(ValueError):
        validate_qpf_intervals(rows, start=START, end=START + timedelta(hours=120))
