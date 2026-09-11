"""Current four-model selection policy, with provider metadata supplied offline."""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from mesoforge.application.current_model_set import select_model_set
from mesoforge.application.prepared_ifs import IFS_CONFIGURATION
from mesoforge.forecasting.recipes import with_surface_fields
from mesoforge.guidance.sources.current_availability import (
    ProviderEvidenceError,
    TemperatureProbeResult,
)
from tests.support.phase1_fixture_transports import FixedClock, RecordingSleeper
from tests.unit.application.test_prepared_temperature import phase2_configuration

TARGET = datetime(2026, 9, 11, 10, tzinfo=UTC)
DECISION = TARGET + timedelta(minutes=20)
NOW = TARGET + timedelta(minutes=25)
LATEST = {
    "HRRR": TARGET.replace(hour=6),
    "GFS": TARGET.replace(hour=6),
    "RAP": TARGET.replace(hour=9),
    "IFS": TARGET.replace(hour=6),
}


def test_surface_discovery_keeps_temperature_cycle_policy_and_records_extended_capabilities(
    tmp_path,
):
    probe = MetadataProbe()
    probe.after_call = lambda kwargs: (
        kwargs["surface_fields"] is True or pytest.fail("missing surface flag")
    )
    clock = FixedClock(NOW)
    report = select_model_set(
        tmp_path / "surface",
        configuration=phase2_configuration(),
        decision_time=DECISION,
        transport=Mock(spec=[], downloaded_bytes=0),
        clock=clock,
        sleeper=RecordingSleeper(clock),
        probe=probe,
        surface_fields=True,
    )
    assert report["surface_fields"] is True
    assert report["selected_cycles"] == {model: _iso(cycle) for model, cycle in LATEST.items()}
    assert report["contributor_configuration"] == with_surface_fields(IFS_CONFIGURATION).model_dump(
        mode="json"
    )
    assert report["contributor_configuration"][
        "control_recipe"
    ] == IFS_CONFIGURATION.control_recipe.model_dump(mode="json")


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class MetadataProbe:
    """Application fixture: real HTTP parsing has its own focused provider tests."""

    def __init__(self, expected_decision=DECISION):
        self.calls = []
        self.expected_decision = expected_decision
        self.missing = lambda model, cycle, lead: False
        self.after_call = lambda kwargs: None

    def __call__(self, **kwargs):
        model, cycle, lead = kwargs["model"], kwargs["cycle"], kwargs["lead"]
        self.calls.append((model, cycle, lead))
        assert kwargs["decision_time"] == self.expected_decision
        index_url = f"https://fixture.invalid/{model}/{cycle:%Y%m%d%H}/f{lead:03d}.idx"
        payload = f"inventory {model} {cycle.isoformat()} {lead}\n".encode()
        available = not self.missing(model, cycle, lead)
        reason = None if available else f"Required temperature lead {lead} is unavailable"
        evidence = {
            "model": model,
            "cycle": _iso(cycle),
            "source_lead_hours": lead,
            "valid_time": _iso(cycle + timedelta(hours=lead)),
            "decision_time": _iso(kwargs["decision_time"]),
            "status": "available" if available else "unavailable",
            "selected_endpoint": "fixture",
            "endpoints": [],
            "index": {
                "url": index_url,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "content_bytes": len(payload),
                "available_at": _iso(DECISION),
                "retrieved_at": _iso(kwargs["clock"].now()),
                "availability_basis": "provider_last_modified",
            },
            "grib": {
                "url": index_url.removesuffix(".idx"),
                "content_length": 100,
                "etag": '"fixed-object-version"',
                "available_at": _iso(DECISION),
                "availability_basis": "provider_last_modified",
            },
            "selected_message": {
                "canonical_variable_id": "air_temperature_2m",
                "byte_start": 0,
                "byte_end_exclusive": 100,
            },
        }
        self.after_call(kwargs)
        return TemperatureProbeResult(available, reason, evidence, payload, {index_url: payload})


def _select(tmp_path, *, probe=None, clock=None, decision_time=DECISION):
    clock = FixedClock(NOW) if clock is None else clock
    return select_model_set(
        tmp_path / "selection",
        configuration=phase2_configuration(),
        decision_time=decision_time,
        transport=Mock(spec=[], downloaded_bytes=0),
        clock=clock,
        sleeper=RecordingSleeper(clock),
        probe=MetadataProbe() if probe is None else probe,
    )


def test_newest_complete_models_align_by_valid_time_without_changing_active_recipe(tmp_path):
    probe = MetadataProbe()
    before = IFS_CONFIGURATION.model_dump(mode="json")
    report = _select(tmp_path, probe=probe)
    assert report["status"] == "selected"
    assert report["selected_cycles"] == {model: _iso(cycle) for model, cycle in LATEST.items()}
    assert report["target_reference_time"] == _iso(TARGET)
    assert report["first_valid_time"] == _iso(TARGET + timedelta(hours=1))
    assert report["last_valid_time"] == _iso(TARGET + timedelta(hours=36))
    for model, cycle in LATEST.items():
        leads = sorted(
            lead for name, selected, lead in probe.calls if name == model and selected == cycle
        )
        hours = [
            int((cycle + timedelta(hours=lead) - TARGET).total_seconds() / 3600) for lead in leads
        ]
        assert hours == (list(range(2, 36, 3)) if model == "IFS" else list(range(1, 37)))
    assert len(probe.calls) == 36 * 3 + 12
    assert report["model_data_acquired"] is False
    assert report["contributor_configuration"] == before
    gaps = report["models"]["IFS"]["expected_native_gaps"]
    assert [gap["horizon_hours"] for gap in gaps] == [
        hour for hour in range(1, 37) if hour not in range(2, 36, 3)
    ]
    assert all(gap["reason"] for gap in gaps)
    assert all(
        report["models"][model]["expected_native_gaps"] == [] for model in ("HRRR", "GFS", "RAP")
    )
    assert IFS_CONFIGURATION.model_dump(mode="json") == before
    assert {c.model: c.weight for c in IFS_CONFIGURATION.control_recipe.contributors} == {
        "HRRR": 0.7,
        "GFS": 0.3,
    }
    assert {m.model_id: m.status for m in IFS_CONFIGURATION.models} == {
        "HRRR": "active",
        "GFS": "active",
        "RAP": "shadow",
        "IFS": "shadow",
    }
    assert not {"locations", "latitude", "longitude", "areas"}.intersection(
        inspect.signature(select_model_set).parameters
    )


@pytest.mark.parametrize("model", ["HRRR", "GFS", "RAP", "IFS"])
def test_present_final_lead_does_not_hide_missing_interior_lead(tmp_path, model):
    probe = MetadataProbe()
    missing_lead = 15 if model == "IFS" else 17
    probe.missing = lambda name, cycle, lead: (
        name == model and cycle == LATEST[model] and lead == missing_lead
    )
    report = _select(tmp_path, probe=probe)
    assert report["status"] == "selected"
    assert report["selected_cycles"][model] == _iso(LATEST[model] - timedelta(hours=6))
    assert (model, LATEST[model], missing_lead) in probe.calls
    expected_final = 39 if model == "IFS" else (37 if model == "RAP" else 40)
    assert (model, LATEST[model], expected_final) in probe.calls
    rejected, accepted = [
        candidate for candidate in report["models"][model]["candidates"] if candidate["probes"]
    ]
    assert rejected["status"] == "rejected"
    assert str(missing_lead) in rejected["reason"]
    assert accepted["status"] == "metadata_complete"


@pytest.mark.parametrize("model", ["HRRR", "GFS", "RAP", "IFS"])
def test_no_complete_required_model_returns_no_partially_selected_model_set(tmp_path, model):
    probe = MetadataProbe()
    probe.missing = lambda name, cycle, lead: name == model
    report = _select(tmp_path, probe=probe)
    assert report["status"] == "unavailable"
    assert report["selected_cycles"] == {}
    assert model in report["reason"]
    calls = [(cycle, lead) for name, cycle, lead in probe.calls if name == model]
    assert calls
    maximum_age = {"HRRR": 12, "GFS": 12, "RAP": 15, "IFS": 24}[model]
    maximum_lead = {"HRRR": 48, "GFS": 48, "RAP": 51, "IFS": 90}[model]
    assert all(TARGET - cycle <= timedelta(hours=maximum_age) for cycle, _ in calls)
    assert all(lead <= maximum_lead for _, lead in calls)
    if model == "RAP":
        assert all(cycle.hour in (3, 9, 15, 21) for cycle, _ in calls)
    if model == "IFS":
        assert all(lead % 3 == 0 for _, lead in calls)


@pytest.mark.parametrize(
    "decision",
    [TARGET - timedelta(seconds=1), NOW + timedelta(seconds=1), DECISION.replace(tzinfo=None)],
)
def test_invalid_decision_time_never_contacts_provider(tmp_path, decision):
    probe = Mock(side_effect=AssertionError("provider access before time validation"))
    with pytest.raises(ValueError):
        _select(tmp_path, decision_time=decision, probe=probe)
    probe.assert_not_called()


def test_timezone_aware_decision_uses_the_same_utc_instant(tmp_path):
    report = _select(tmp_path, decision_time=DECISION.astimezone(timezone(timedelta(hours=-5))))
    assert report["status"] == "selected"
    assert report["decision_time"] == _iso(DECISION)
    assert report["target_reference_time"] == _iso(TARGET)


def test_omitting_decision_time_uses_the_execution_clock(tmp_path):
    report = _select(tmp_path, decision_time=None, probe=MetadataProbe(expected_decision=NOW))
    assert report["status"] == "selected"
    assert report["decision_time"] == _iso(NOW)
    assert report["target_reference_time"] == _iso(TARGET)


def test_naive_execution_clock_fails_before_provider_calls(tmp_path):
    probe = Mock(side_effect=AssertionError("provider access before clock validation"))
    with pytest.raises(ValueError, match="timezone"):
        _select(tmp_path, clock=FixedClock(NOW.replace(tzinfo=None)), probe=probe)
    probe.assert_not_called()


def test_reference_expiring_during_discovery_cannot_publish_a_stale_selection(tmp_path):
    clock = FixedClock(NOW)
    probe = MetadataProbe()
    probe.after_call = lambda kwargs: clock.advance(35 * 60)
    report = _select(tmp_path, probe=probe, clock=clock)
    assert report["status"] == "unavailable"
    assert report["selected_cycles"] == {}
    assert "expired" in report["reason"].lower()
    assert len(probe.calls) == 1


def test_provider_integrity_failure_is_an_error_instead_of_an_older_fallback(tmp_path):
    evidence = {"status": "error", "reason": "ambiguous temperature inventory"}
    probe = Mock(
        side_effect=ProviderEvidenceError(
            "ambiguous temperature inventory", evidence=evidence, index_payloads={}
        )
    )
    report = _select(tmp_path, probe=probe)
    assert report["status"] == "error"
    assert report["selected_cycles"] == {}
    assert "ambiguous" in report["reason"]
    assert probe.call_count == 1


def test_retained_metadata_matches_report_and_existing_selection_is_never_overwritten(tmp_path):
    probe = MetadataProbe()
    report = _select(tmp_path, probe=probe)
    directory = tmp_path / "selection"
    assert json.loads((directory / "selection.json").read_text()) == report
    before = {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }
    assert not list(directory.rglob("*.grib2"))
    assert len(before) >= 121  # One report and all 120 original temperature inventories.
    for model in report["models"].values():
        for candidate in model["candidates"]:
            for evidence in candidate["probes"]:
                retained = evidence["retained_indexes"]
                assert len(retained) == 1
                entry = retained[0]
                payload = (directory / entry["file"]).read_bytes()
                assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
                assert entry["sha256"] == evidence["index"]["sha256"]
                assert entry["url"] == evidence["index"]["url"]
                assert len(payload) == entry["bytes"]
    forbidden = Mock(side_effect=AssertionError("overwriting selection contacted provider"))
    with pytest.raises((ValueError, FileExistsError)):
        _select(tmp_path, probe=forbidden)
    forbidden.assert_not_called()
    assert {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    } == before
