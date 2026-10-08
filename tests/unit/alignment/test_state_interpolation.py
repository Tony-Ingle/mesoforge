"""State-only interpolation preserves native endpoints and cannot invent events."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.alignment.state_interpolation import (
    STATE_INTERPOLATION_POLICY,
    NativeStateSample,
    align_state_samples,
)
from mesoforge.alignment.temporal import TemporalAlignmentError

_CYCLE = datetime(2026, 10, 8, tzinfo=UTC)


def _sample(lead: int, value: float = 280.0, **changes: object) -> NativeStateSample:
    return NativeStateSample(
        **{
            "source": "IFS",
            "cycle": _CYCLE,
            "field": "air_temperature_2m",
            "unit": "K",
            "definition": "air_temperature",
            "vertical_extent": "2m_above_ground",
            "spatial_support": "fixed-grid-and-coordinate-reference",
            "valid_time": _CYCLE + timedelta(hours=lead),
            "value": value,
            "evidence_refs": {"raw_sha256": f"native-message-{lead}", "pointer": "/inputs/0"},
            **changes,
        }
    )


def test_native_or_interpolated_values_retain_exact_evidence_and_replay() -> None:
    left, right = _sample(3, 280.0), _sample(6, 286.0)
    native = align_state_samples(
        [left, right], target_valid_time=left.valid_time, native_step_hours=3
    )
    assert native.value == left.value
    assert native.mode == "native" and native.weights == (1.0,)
    target = _CYCLE + timedelta(hours=4)
    aligned = align_state_samples([right, left], target_valid_time=target, native_step_hours=3)
    assert aligned.value == math.fsum((280.0 * (1 - 1 / 3), 286.0 / 3))
    assert aligned.mode == "interpolated"
    assert aligned.weights == (1 - 1 / 3, 1 / 3)
    assert (
        aligned.payload()
        == align_state_samples(
            [left, right], target_valid_time=target, native_step_hours=3
        ).payload()
    )
    assert aligned.payload()["policy"] == STATE_INTERPOLATION_POLICY
    assert aligned.payload()["endpoints"] == [left.payload(), right.payload()]
    assert aligned.payload()["endpoints"][1]["source_lead_hours"] == 6


def test_wind_components_align_across_north_without_averaging_direction() -> None:
    outputs = []
    for field, function in (("eastward_wind_10m", math.sin), ("northward_wind_10m", math.cos)):
        samples = [
            _sample(
                lead,
                -10 * function(math.radians(direction)),
                field=field,
                unit="m/s",
                definition="earth_relative_wind_component",
                vertical_extent="10m_above_ground",
            )
            for lead, direction in ((0, 350), (6, 10))
        ]
        result = align_state_samples(
            samples, target_valid_time=_CYCLE + timedelta(hours=3), native_step_hours=6
        )
        outputs.append(result.value)
    assert outputs[0] == pytest.approx(0.0, abs=1e-14)
    assert outputs[1] < -9.8
    # A scalar interpolation of 350 and 10 would incorrectly give 180 degrees.
    assert abs(math.degrees(math.atan2(-outputs[0], -outputs[1]))) < 1e-12


def test_total_cloud_units_definition_and_vertical_support_are_explicit() -> None:
    for unit, maximum in (("1", 1.0), ("percent", 100.0), ("%", 100.0)):
        left = _sample(
            3,
            0.0,
            field="cloud_area_fraction",
            unit=unit,
            definition="total_cloud_cover",
            vertical_extent="entire_atmosphere",
        )
        right = replace(left, valid_time=_CYCLE + timedelta(hours=6), value=maximum)
        result = align_state_samples(
            [left, right], target_valid_time=_CYCLE + timedelta(hours=4), native_step_hours=3
        )
        assert result.value == maximum * (1 / 3)
        for invalid in ({"definition": "opaque_sky"}, {"vertical_extent": "low_layer"}):
            with pytest.raises(TemporalAlignmentError, match="total cover"):
                replace(left, **invalid)
    fraction = _sample(
        3,
        0.5,
        field="cloud_area_fraction",
        unit="1",
        definition="total_cloud_cover",
        vertical_extent="entire_atmosphere",
    )
    percent = replace(fraction, valid_time=_CYCLE + timedelta(hours=6), unit="percent", value=50.0)
    with pytest.raises(TemporalAlignmentError, match="disagree"):
        align_state_samples(
            [fraction, percent],
            target_valid_time=_CYCLE + timedelta(hours=4),
            native_step_hours=3,
        )


def test_constant_states_retain_exact_values_including_contract_boundaries() -> None:
    for value in (150.0, 280.0000000003, 340.0):
        result = align_state_samples(
            [_sample(3, value), _sample(6, value)],
            target_valid_time=_CYCLE + timedelta(hours=4),
            native_step_hours=3,
        )
        assert result.value == value


@pytest.mark.parametrize(
    "changes",
    [
        {"field": "wind_from_direction_10m", "unit": "degree", "value": 180.0},
        {"field": "wind_gust_10m", "unit": "m/s", "value": 10.0},
        {"field": "liquid_equivalent_precipitation_amount_1h", "unit": "kg/m^2", "value": 1.0},
        {"field": "probability_of_precipitation_1h", "unit": "percent", "value": 50.0},
        {"field": "precipitation_type", "value": 1.0},
        {"temporal_semantics": "maximum"},
        {"temporal_semantics": "accumulation"},
        {"temporal_semantics": "probability"},
        {"temporal_semantics": "categorical"},
        {"unit": "degF"},
        {"value": None},
        {"value": True},
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": 341.0},
        {"value": 149.0},
        {"source": ""},
        {"definition": ""},
        {"evidence_refs": {}},
        {"evidence_refs": {"raw_sha256": None}},
        {"cycle": _CYCLE.replace(tzinfo=None)},
        {"valid_time": _CYCLE - timedelta(hours=1)},
        {"valid_time": _CYCLE + timedelta(minutes=1)},
    ],
)
def test_invalid_or_nonstate_inputs_fail_closed(changes: dict) -> None:
    with pytest.raises(TemporalAlignmentError):
        _sample(3, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "GFS"},
        {"cycle": _CYCLE - timedelta(hours=6)},
        {"field": "dew_point_temperature_2m"},
        {"definition": "different-temperature-definition"},
        {"vertical_extent": "another-level"},
        {"spatial_support": "another-point"},
    ],
)
def test_incompatible_endpoints_are_not_combined(changes: dict) -> None:
    with pytest.raises(TemporalAlignmentError, match="disagree"):
        align_state_samples(
            [_sample(3), _sample(6, **changes)],
            target_valid_time=_CYCLE + timedelta(hours=4),
            native_step_hours=3,
        )


def test_no_extrapolation_duplicate_time_or_missing_native_step() -> None:
    samples = [_sample(3), _sample(6)]
    for lead in (2, 7):
        with pytest.raises(TemporalAlignmentError, match="extrapolate"):
            align_state_samples(
                samples, target_valid_time=_CYCLE + timedelta(hours=lead), native_step_hours=3
            )
    with pytest.raises(TemporalAlignmentError, match="Duplicate"):
        align_state_samples([samples[0], samples[0]], target_valid_time=_CYCLE, native_step_hours=3)
    with pytest.raises(TemporalAlignmentError, match="missing native step"):
        align_state_samples(
            [_sample(3), _sample(9)],
            target_valid_time=_CYCLE + timedelta(hours=4),
            native_step_hours=3,
        )
    with pytest.raises(TemporalAlignmentError, match="cadence"):
        align_state_samples(
            [_sample(2), _sample(5)],
            target_valid_time=_CYCLE + timedelta(hours=3),
            native_step_hours=3,
        )
    for step in (True, 3.0, 0, 2, 12):
        with pytest.raises(TemporalAlignmentError, match="cadence"):
            align_state_samples(samples, target_valid_time=_CYCLE, native_step_hours=step)
    with pytest.raises(TemporalAlignmentError, match="requires validated"):
        align_state_samples([], target_valid_time=_CYCLE, native_step_hours=3)


def test_endpoint_reference_cannot_be_modified_through_input_or_payload() -> None:
    refs = {"raw_sha256": "original"}
    sample = _sample(3, evidence_refs=refs)
    refs["raw_sha256"] = "replaced"
    exported = sample.payload()
    exported["evidence_refs"]["raw_sha256"] = "replaced-again"
    assert sample.evidence_refs["raw_sha256"] == "original"
    with pytest.raises(TypeError):
        sample.evidence_refs["raw_sha256"] = "forbidden"


def test_explicit_utc_native_lattice_handles_nbm_phase_and_transition():
    cycle = _CYCLE.replace(hour=13)
    for left_lead, right_lead, target_lead in ((48, 50, 49), (125, 128, 126)):
        left = _sample(
            left_lead,
            280.0,
            source="NBM",
            cycle=cycle,
            valid_time=cycle + timedelta(hours=left_lead),
        )
        right = replace(left, value=286.0, valid_time=cycle + timedelta(hours=right_lead))
        aligned = align_state_samples(
            [left, right],
            target_valid_time=cycle + timedelta(hours=target_lead),
            native_step_hours=right_lead - left_lead,
            native_lattice_origin=left.valid_time,
        )
        assert aligned.value == (283.0 if right_lead - left_lead == 2 else 282.0)
        assert aligned.endpoints == (left, right)
        replay_origin = datetime.fromisoformat(aligned.payload()["native_lattice_origin"])
        assert (
            align_state_samples(
                aligned.endpoints,
                target_valid_time=aligned.target_valid_time,
                native_step_hours=aligned.native_step_hours,
                native_lattice_origin=replay_origin,
            ).payload()
            == aligned.payload()
        )
