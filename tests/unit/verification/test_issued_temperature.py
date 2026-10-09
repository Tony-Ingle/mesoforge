"""Independent arithmetic and eligibility checks for one issued temperature hour."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from mesoforge.common.horizon import FIVE_DAY_HORIZON
from mesoforge.verification.issued_temperature import evaluate_temperature_verification

_CUTOFF = datetime(2026, 9, 10, 14, tzinfo=UTC)


@pytest.fixture
def match() -> dict[str, Any]:
    return {
        "issued_forecast_id": "839a052b-61ab-4d58-a008-8a81d68c5137",
        "issued_at": "2026-09-10T12:00:00Z",
        "forecast": {
            "latitude": 45.8,
            "longitude": -93.1,
            "valid_time": "2026-09-10T13:00:00Z",
            "temperature": {"value": 285.5, "unit": "K"},
            "missing_reasons": [],
            "sources": [
                {"model": "HRRR", "cycle": "2026-09-10T12:00:00Z", "weight": 0.7},
                {"model": "GFS", "cycle": "2026-09-10T06:00:00Z", "weight": 0.3},
            ],
        },
        "forecast_context": {"data_kind": "synthetic_demonstration"},
        "status": "matched",
        "unavailable_reason": None,
        "selected": {
            "station_id": "KROS",
            "network": "METAR",
            "latitude": 45.69624,
            "longitude": -92.95427,
            "elevation_m": 282.0,
            "distance_km": 16.173790607953634,
            "observation_time": "2026-09-10T13:10:00Z",
            "temperature": {"value": 283.25, "unit": "K"},
            "qc": {"state": "partial", "temperature_eligible": True, "provider_qc_field": 4},
            "provenance": {
                "station_snapshot_artifact_id": "station-snapshot",
                "revision_digest": "observation-revision-1",
                "provider_available_at": "2026-09-10T13:11:00Z",
                "ingested_at": "2026-09-10T13:20:00Z",
            },
        },
        "selection_policy": {
            "max_distance_km": 50,
            "max_time_difference_minutes": 15,
            "boundaries": "inclusive",
            "ranking": ["distance", "absolute_time_difference", "station_id"],
            "revisions": "latest retained revision per logical observation, before QC",
        },
        "input_provenance": {
            "observations": {
                "artifact_id": "observation-snapshot",
                "availability": {"available_at": "2026-09-10T14:00:00Z"},
                "attributes": {"data_kind": "synthetic_observation_fixture"},
            },
            "station_snapshots": [
                {
                    "artifact_id": "station-snapshot",
                    "availability": {"available_at": "2026-09-10T12:00:00Z"},
                }
            ],
        },
    }


def _evaluate(match: dict[str, Any]) -> dict[str, Any]:
    return evaluate_temperature_verification(
        match, verification_cutoff=_CUTOFF, evaluated_at=_CUTOFF
    )


def test_long_lead_temperature_preserves_matching_and_requires_declared_window(match):
    valid = datetime.fromisoformat(match["forecast"]["valid_time"])
    target = valid - timedelta(hours=120)
    match["forecast"]["horizon_hours"] = 120
    match["forecast_context"].update(
        forecast_horizon=FIVE_DAY_HORIZON.payload(), target_reference_time=target.isoformat()
    )
    result = _evaluate(match)
    assert result["status"] == "verified"
    assert result["temperature_error"]["value"] == 2.25
    assert result["match"]["forecast"]["horizon_hours"] == 120
    del match["forecast_context"]["forecast_horizon"]
    assert "forecast_reference_lead_inconsistent" in _evaluate(match)["reasons"]
    match["forecast_context"]["forecast_horizon"] = FIVE_DAY_HORIZON.payload()
    match["selected"]["observation_time"] = (valid + timedelta(minutes=16)).isoformat()
    assert "observation_outside_15_minute_window" in _evaluate(match)["reasons"]


@pytest.mark.parametrize(
    ("forecast", "observation", "expected"),
    [(285.5, 283.25, 2.25), (280.25, 284.0, -3.75), (283.15, 283.15, 0.0)],
)
def test_signed_kelvin_error_and_unchanged_provenance(
    match: dict[str, Any], forecast: float, observation: float, expected: float
) -> None:
    match["forecast"]["temperature"]["value"] = forecast
    match["selected"]["temperature"]["value"] = observation
    original = deepcopy(match)
    result = _evaluate(match)
    assert result["status"] == "verified"
    assert result["reasons"] == []
    assert result["temperature_error"] == {
        "value": expected,
        "unit": "K",
        "definition": "forecast_minus_observation",
    }
    assert result["match"] == original == match
    assert result["match"]["selected"]["provenance"]["revision_digest"] == "observation-revision-1"
    assert result["match"]["input_provenance"]["observations"]["attributes"] == {
        "data_kind": "synthetic_observation_fixture"
    }
    assert result["verification_policy"]["matching_rules"] == match["selection_policy"]
    assert "not a complete operational" in result["verification_policy"]["input_availability_limit"]


@pytest.mark.parametrize("part", ["forecast", "selected"])
@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), float("-inf"), True, "285"])
def test_invalid_temperatures_never_create_a_score(
    match: dict[str, Any], part: str, value: Any
) -> None:
    match[part]["temperature"]["value"] = value
    result = _evaluate(match)
    assert result["status"] == "ineligible"
    prefix = "forecast" if part == "forecast" else "observation"
    assert f"{prefix}_temperature_missing_nonfinite_or_not_kelvin" in result["reasons"]
    assert result["temperature_error"]["value"] is None


@pytest.mark.parametrize("part", ["forecast", "selected"])
@pytest.mark.parametrize("unit", ["degC", "F", None])
def test_units_are_not_silently_converted(match: dict[str, Any], part: str, unit: Any) -> None:
    match[part]["temperature"]["unit"] = unit
    assert _evaluate(match)["temperature_error"]["value"] is None


@pytest.mark.parametrize("issued_at", ["2026-09-10T13:00:00Z", "2026-09-10T13:01:00Z"])
def test_forecast_must_be_issued_strictly_before_valid_time(
    match: dict[str, Any], issued_at: str
) -> None:
    match["issued_at"] = issued_at
    result = _evaluate(match)
    assert "forecast_not_issued_before_valid_time" in result["reasons"]
    assert result["temperature_error"]["value"] is None


@pytest.mark.parametrize("issued_at", ["2026-09-10T12:50:00Z", "2026-09-10T12:51:00Z"])
def test_observation_cannot_precede_or_equal_issuance(
    match: dict[str, Any], issued_at: str
) -> None:
    match["issued_at"] = issued_at
    match["selected"]["observation_time"] = "2026-09-10T12:50:00Z"
    result = _evaluate(match)
    assert "forecast_not_issued_before_observation_time" in result["reasons"]
    assert result["temperature_error"]["value"] is None


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (
            ("forecast", "valid_time"),
            "2026-09-10T15:00:00Z",
            "forecast_valid_time_after_verification_cutoff",
        ),
        (
            ("selected", "observation_time"),
            "2026-09-10T14:00:01Z",
            "observation_time_after_verification_cutoff",
        ),
        (
            ("selected", "provenance", "provider_available_at"),
            "2026-09-10T14:00:01Z",
            "observation_provider_available_at_after_verification_cutoff",
        ),
        (
            ("selected", "provenance", "ingested_at"),
            "2026-09-10T14:00:01Z",
            "observation_ingested_at_after_verification_cutoff",
        ),
        (("selected", "qc", "temperature_eligible"), False, "observation_temperature_qc_failed"),
        (("selected", "provenance", "revision_digest"), None, "observation_revision_unavailable"),
        (("forecast", "missing_reasons"), ["HRRR missing"], "forecast_has_explicit_missingness"),
    ],
)
def test_ineligible_inputs_have_explicit_reasons(
    match: dict[str, Any], path: tuple[str, ...], value: Any, reason: str
) -> None:
    parent = match
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    result = _evaluate(match)
    assert result["status"] == "ineligible"
    assert reason in result["reasons"]
    assert result["temperature_error"]["value"] is None


@pytest.mark.parametrize("kind", ["station_snapshots", "observations"])
def test_input_manifest_must_be_available_by_cutoff(match: dict[str, Any], kind: str) -> None:
    manifest = match["input_provenance"][kind]
    if kind == "station_snapshots":
        manifest = manifest[0]
    manifest["availability"]["available_at"] = "2026-09-10T14:00:01Z"
    result = _evaluate(match)
    expected = "station_snapshot" if kind == "station_snapshots" else "normalized_observations"
    assert f"{expected}_after_verification_cutoff" in result["reasons"]
    assert result["temperature_error"]["value"] is None


@pytest.mark.parametrize(
    "path",
    [
        ("issued_at",),
        ("forecast", "valid_time"),
        ("selected", "observation_time"),
        ("selected", "provenance", "provider_available_at"),
        ("selected", "provenance", "ingested_at"),
    ],
)
def test_naive_retained_timestamps_are_explicitly_ineligible(
    match: dict[str, Any], path: tuple[str, ...]
) -> None:
    parent = match
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = "2026-09-10T12:00:00"
    result = _evaluate(match)
    assert result["status"] == "ineligible"
    assert any("timezone_aware" in reason for reason in result["reasons"])


@pytest.mark.parametrize("argument", ["verification_cutoff", "evaluated_at"])
def test_call_timestamps_require_timezones(match: dict[str, Any], argument: str) -> None:
    kwargs = {"verification_cutoff": _CUTOFF, "evaluated_at": _CUTOFF}
    kwargs[argument] = _CUTOFF.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezones"):
        evaluate_temperature_verification(match, **kwargs)


def test_future_cutoff_is_not_eligible(match: dict[str, Any]) -> None:
    result = evaluate_temperature_verification(
        match, verification_cutoff=_CUTOFF, evaluated_at=_CUTOFF - timedelta(seconds=1)
    )
    assert result["reasons"] == ["verification_cutoff_after_evaluation"]
    assert result["temperature_error"]["value"] is None


def test_retry_has_identical_fact_and_explicit_utc_cutoff(match: dict[str, Any]) -> None:
    second = evaluate_temperature_verification(
        match,
        verification_cutoff=_CUTOFF.astimezone(timezone(timedelta(hours=-5))),
        evaluated_at=_CUTOFF + timedelta(days=1),
    )
    assert _evaluate(match) == second
    assert second["verification_cutoff"] == "2026-09-10T14:00:00Z"


@pytest.mark.parametrize("observation_time", ["2026-09-10T12:45:00Z", "2026-09-10T13:15:00Z"])
def test_matching_and_cutoff_boundaries_are_inclusive(
    match: dict[str, Any], observation_time: str
) -> None:
    match["selected"]["observation_time"] = observation_time
    match["selected"]["distance_km"] = 50.0
    for field in ("provider_available_at", "ingested_at"):
        match["selected"]["provenance"][field] = "2026-09-10T14:00:00Z"
    assert _evaluate(match)["status"] == "verified"


@pytest.mark.parametrize("distance", [50.0001, -1, None, float("nan")])
def test_invalid_distance_cannot_be_verified(match: dict[str, Any], distance: Any) -> None:
    match["selected"]["distance_km"] = distance
    result = _evaluate(match)
    assert "observation_outside_50_km_or_distance_unavailable" in result["reasons"]


def test_observation_outside_time_window_cannot_be_verified(match: dict[str, Any]) -> None:
    match["selected"]["observation_time"] = "2026-09-10T13:15:01Z"
    assert "observation_outside_15_minute_window" in _evaluate(match)["reasons"]


@pytest.mark.parametrize("cycle", ["2026-09-10T12:00:01Z", None, "2026-09-10T12:00:00"])
def test_source_cycle_must_be_known_and_not_after_issue(match: dict[str, Any], cycle: Any) -> None:
    match["forecast"]["sources"][0]["cycle"] = cycle
    result = _evaluate(match)
    assert result["status"] == "ineligible"
    assert result["temperature_error"]["value"] is None


def test_unavailable_preview_preserves_reason_without_fictitious_input_cutoff(
    match: dict[str, Any],
) -> None:
    match.update(status="unavailable", selected=None, input_provenance=None)
    match["unavailable_reason"] = "No retained observation dataset is configured."
    result = _evaluate(match)
    assert result["status"] == "unavailable"
    assert result["reasons"] == [match["unavailable_reason"]]
    assert result["verification_cutoff"] is None
    assert result["temperature_error"]["value"] is None


def test_no_acceptable_observation_preserves_dataset_provenance(match: dict[str, Any]) -> None:
    match.update(
        status="unavailable", selected=None, unavailable_reason="All candidates failed QC."
    )
    result = _evaluate(match)
    assert result["status"] == "unavailable"
    assert result["reasons"] == ["All candidates failed QC."]
    assert result["verification_cutoff"] == "2026-09-10T14:00:00Z"
    assert result["match"]["input_provenance"] == match["input_provenance"]


def test_forecast_versions_and_observation_revisions_remain_distinct(match: dict[str, Any]) -> None:
    first = _evaluate(match)
    other_forecast = deepcopy(match)
    other_forecast["issued_forecast_id"] = "a71874e5-77e8-4f0d-bccf-d6a667ce8b94"
    other_observation = deepcopy(match)
    other_observation["selected"]["provenance"]["revision_digest"] = "observation-revision-2"
    assert _evaluate(other_forecast) != first
    assert _evaluate(other_observation) != first
