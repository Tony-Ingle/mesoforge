"""Exact cover aggregation cannot manufacture an hourly precipitation event."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.guidance.precipitation import (
    QPF_INTERVAL_COMPOSITION_POLICY,
    QpfIntervalAmount,
    QpfIntervalError,
    compose_qpf_interval,
)

CYCLE = datetime(2026, 10, 8, tzinfo=UTC)


def amount(start: int, end: int, value: float = 0.0) -> QpfIntervalAmount:
    return QpfIntervalAmount(
        source="GFS",
        cycle=CYCLE,
        interval_start=CYCLE + timedelta(hours=start),
        interval_end=CYCLE + timedelta(hours=end),
        unit="kg/m^2",
        spatial_support="native-gridpoint-12-34",
        value=value,
        evidence_refs={"prepared_sha256": "retained-content", "record": f"/qpf/{start}-{end}"},
    )


@pytest.mark.parametrize("duration", [3, 6])
def test_hourlies_form_exact_coarser_event_with_all_parent_evidence(duration: int) -> None:
    rows = [amount(hour, hour + 1, hour / 2) for hour in range(duration)]
    result = compose_qpf_interval(
        rows[::-1], target_start=CYCLE, target_end=CYCLE + timedelta(hours=duration)
    )
    assert result.value == sum(row.value for row in rows if row.value is not None)
    assert result.state == "known"
    assert result.inputs == tuple(rows)
    payload = result.payload()
    assert payload["policy"] == QPF_INTERVAL_COMPOSITION_POLICY
    assert payload["interval_closure"] == "left_open_right_closed"
    assert payload["temporal_semantics"] == "accumulation"
    assert payload["duration_hours"] == duration
    assert payload["unit"] == "kg/m^2"
    assert payload["inputs"] == [row.payload() for row in rows]
    assert (
        compose_qpf_interval(rows, target_start=CYCLE, target_end=result.interval_end).payload()
        == payload
    )


def test_native_coarse_amount_passes_without_disaggregation() -> None:
    source = amount(0, 6, 12.3)
    result = compose_qpf_interval([source], target_start=CYCLE, target_end=source.interval_end)
    assert result.value == 12.3
    assert result.payload()["operation"] == "native_interval"
    with pytest.raises(QpfIntervalError, match="splitting"):
        compose_qpf_interval([source], target_start=CYCLE, target_end=CYCLE + timedelta(hours=1))


def test_mixed_durations_can_sum_only_if_they_cover_exactly() -> None:
    rows = [amount(0, 3, 1), amount(3, 4, 2), amount(4, 5, 3), amount(5, 6, 4)]
    result = compose_qpf_interval(rows, target_start=CYCLE, target_end=CYCLE + timedelta(hours=6))
    assert result.value == 10
    assert [row.interval_end for row in result.inputs] == [row.interval_end for row in rows]


@pytest.mark.parametrize(
    "rows",
    [
        [amount(0, 1), amount(2, 3)],  # gap
        [amount(0, 2), amount(1, 3)],  # overlap
        [amount(0, 1), amount(0, 1), amount(1, 3)],  # duplicate evidence
        [amount(0, 1)],  # incomplete tail
        [amount(1, 3)],  # incomplete head
        [amount(0, 3), amount(3, 4)],  # extra event
        [amount(0, 6)],  # no proportional splitting
    ],
)
def test_reject_non_exact_covers(rows: list[QpfIntervalAmount]) -> None:
    with pytest.raises(QpfIntervalError, match="cover"):
        compose_qpf_interval(rows, target_start=CYCLE, target_end=CYCLE + timedelta(hours=3))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("source", "HRRR"),
        ("cycle", CYCLE - timedelta(hours=6)),
        ("unit", "mm"),
        ("spatial_support", "different-coordinate"),
    ],
)
def test_reject_mixed_source_cycle_unit_or_spatial_support(key: str, value: object) -> None:
    rows = [amount(0, 1), replace(amount(1, 2), **{key: value})]
    with pytest.raises(QpfIntervalError, match="disagree"):
        compose_qpf_interval(rows, target_start=CYCLE, target_end=CYCLE + timedelta(hours=2))


def test_zero_is_numeric_but_missing_and_unavailable_are_not_partial_totals() -> None:
    rows = [amount(0, 1), amount(1, 2), amount(2, 3)]
    end = CYCLE + timedelta(hours=3)
    dry = compose_qpf_interval(rows, target_start=CYCLE, target_end=end)
    assert dry.value == 0
    assert dry.state == "known"
    rows[1] = replace(rows[1], value=None, state="missing", missing_reasons=("native missing",))
    incomplete = compose_qpf_interval(rows, target_start=CYCLE, target_end=end)
    assert incomplete.value is None
    assert incomplete.state == "missing"
    assert incomplete.payload()["missing_reasons"] == ["native missing"]
    rows[2] = replace(
        rows[2], value=None, state="unavailable", missing_reasons=("source unavailable",)
    )
    unavailable = compose_qpf_interval(rows, target_start=CYCLE, target_end=end)
    assert unavailable.value is None
    assert unavailable.state == "unavailable"
    assert [row.state for row in unavailable.inputs] == ["known", "missing", "unavailable"]


@pytest.mark.parametrize("value", [-0.1, float("nan"), float("inf"), True, None])
def test_invalid_known_amount_never_becomes_zero(value: float | None) -> None:
    with pytest.raises(QpfIntervalError, match="finite"):
        replace(amount(0, 1), value=value)


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "missing", "value": 0},
        {"state": "missing", "value": None},
        {"missing_reasons": ("missing",)},
        {"state": "trace", "value": None},
        {"unit": "in"},
        {"interval_start": CYCLE - timedelta(hours=1)},
        {"interval_end": CYCLE},
        {"interval_end": CYCLE + timedelta(minutes=30)},
        {"cycle": CYCLE.replace(tzinfo=None)},
        {"evidence_refs": {}},
    ],
)
def test_invalid_metadata_is_not_repaired(changes: dict[str, object]) -> None:
    with pytest.raises(QpfIntervalError):
        replace(amount(0, 1), **changes)


def test_references_are_immutable_and_retained_input_values_do_not_change() -> None:
    refs = {"artifact": "before"}
    first = replace(amount(0, 1, 2), evidence_refs=refs)
    refs["artifact"] = "after"
    result = compose_qpf_interval(
        [first, amount(1, 2, 3)], target_start=CYCLE, target_end=CYCLE + timedelta(hours=2)
    )
    assert result.value == 5
    assert first.value == 2
    assert result.inputs[0].evidence_refs == {"artifact": "before"}
    with pytest.raises(TypeError):
        first.evidence_refs["artifact"] = "mutate"  # type: ignore[index]


def test_nonfinite_aggregate_rejected_without_clipping() -> None:
    with pytest.raises(QpfIntervalError, match="finite"):
        compose_qpf_interval(
            [amount(0, 1, 1e308), amount(1, 2, 1e308)],
            target_start=CYCLE,
            target_end=CYCLE + timedelta(hours=2),
        )
