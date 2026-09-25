"""Evidence-gated local temperature stages preserve control and scientific diagnostics."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.application.corrections import (
    apply_temperature_correction,
    propose_temperature_policy,
    validate_temperature_policy,
)
from mesoforge.application.local_surface_grid import build_local_surface_grid, extract_grid_point
from mesoforge.contracts.serialization import canonical_json_bytes, canonical_json_digest
from mesoforge.forecasting.coherence import BASELINE_COHERENCE, DEW_POINT, RH, TEMPERATURE
from mesoforge.forecasting.surface import relative_humidity_percent
from mesoforge.verification.site_analysis import EVIDENCE_POLICY, analyze_facts
from tests.unit.verification.test_site_analysis import DAY, history

LAT, LON = 44.98861, -93.25553
CUTOFF = datetime(2026, 10, 20, tzinfo=UTC)
CREATED = CUTOFF + timedelta(hours=1)
DECISION = CREATED + timedelta(hours=1)


def _analysis(*, qualified=True, bias=1.0):
    facts = history({DAY + timedelta(days=day): bias for day in range(12)}, range(1, 7))
    for fact in facts:
        fact.update(latitude=LAT, longitude=LON)
    return {
        **analyze_facts(facts if qualified else facts[:1]),
        "coordinate": {"latitude": LAT, "longitude": LON},
        "evidence_policy": EVIDENCE_POLICY,
        "evaluation": {
            "evidence_cutoff": CUTOFF.isoformat(),
            "evidence_availability": "verified_input_cutoff_and_fact_registration",
            "forecast_stage_scope": "raw_baseline_only",
        },
    }


def _policy(*, role="candidate", qualified=True, bias=1.0):
    policy = propose_temperature_policy(
        _analysis(qualified=qualified, bias=bias),
        policy_id="test-site-temperature",
        version="1",
        evidence_cutoff=CUTOFF,
        created_at=CREATED,
    )
    if role != "candidate":
        policy.update(lifecycle_role=role, activated_at=CREATED.isoformat())
        policy["digest"] = str(
            canonical_json_digest({k: v for k, v in policy.items() if k != "digest"})
        )
    return policy


def _forecast():
    def column(*, latitude, longitude):
        hours = []
        for horizon in range(1, 37):
            temp = 290.0 + (latitude - LAT)
            fields = {
                TEMPERATURE: {
                    "value": temp,
                    "unit": "K",
                    "policy": "temperature_control_v1",
                    "missing_reasons": [],
                },
                DEW_POINT: {
                    "value": 285.0,
                    "unit": "K",
                    "policy": "existing-dew",
                    "missing_reasons": [],
                    "status": "available",
                },
                RH: {
                    "value": relative_humidity_percent(temperature_k=temp, dew_point_k=285.0),
                    "unit": "%",
                },
                "wind_speed_10m": {"value": 2.5, "unit": "m/s"},
            }
            hours.append(
                {
                    "horizon_hours": horizon,
                    "valid_time": (DECISION + timedelta(hours=horizon)).isoformat(),
                    "temperature": {"value": temp, "unit": "K"},
                    "surface": {"fields": fields, "contributors": {"HRRR": {TEMPERATURE: 291.0}}},
                    "missing_reasons": [],
                }
            )
        return {
            "latitude": latitude,
            "longitude": longitude,
            "hours": hours,
            "target_reference_time": DECISION.isoformat(),
        }

    grid = build_local_surface_grid(latitude=LAT, longitude=LON, calculate_column=column)
    return extract_grid_point(grid, latitude=LAT, longitude=LON, copy_grid=False)


def test_candidate_sign_buckets_and_insufficient_evidence_reuse_existing_governance():
    policy = _policy()
    validate_temperature_policy(policy)
    assert policy["lifecycle_role"] == "candidate"
    assert policy["lead_buckets"]["1-6"]["delta_k"] == -1.0
    assert policy["lead_buckets"]["7-18"]["delta_k"] is None
    assert policy["lead_buckets"]["19-36"]["delta_k"] is None
    assert _policy(bias=-2.0)["lead_buckets"]["1-6"]["delta_k"] == 2.0
    sparse = _policy(qualified=False)
    assert sparse["lifecycle_role"] == "insufficient_evidence"
    assert all(row["delta_k"] is None for row in sparse["lead_buckets"].values())


@pytest.mark.parametrize(
    "policy", [None, _policy(), _policy(qualified=False), _policy(role="shadow")]
)
def test_operational_noop_returns_exact_object_without_recalculating(policy, monkeypatch):
    forecast = _forecast()
    before = canonical_json_bytes(forecast)
    monkeypatch.setattr(
        BASELINE_COHERENCE, "apply_local_fields", lambda *a, **k: pytest.fail("no-op coherence")
    )
    result, overlay = apply_temperature_correction(forecast, policy, analysis_cutoff=DECISION)
    assert result is forecast and canonical_json_bytes(result) == before
    assert overlay["changes"] == []
    assert overlay["applied_delta_k"] == 0


@pytest.mark.parametrize("mode,role", [("operational", "active"), ("shadow", "shadow")])
def test_explicit_policy_corrects_copy_on_write_and_reextracts_exact_point(mode, role):
    forecast = _forecast()
    before = canonical_json_bytes(forecast)
    policy = _policy(role=role)
    result, overlay = apply_temperature_correction(
        forecast, policy, analysis_cutoff=DECISION, mode=mode
    )
    assert overlay["status"] == "applied"
    assert len(overlay["changes"]) == 49 * 6
    assert canonical_json_bytes(forecast) == before
    assert result["hours"][0]["temperature"]["value"] == 289.0
    assert result["hours"][6]["temperature"]["value"] == 290.0
    assert result["hours"][0]["surface"]["fields"][RH]["value"] == relative_humidity_percent(
        temperature_k=289, dew_point_k=285
    )
    for original, changed in zip(
        forecast["local_grid_baseline"]["cells"],
        result["local_grid_baseline"]["cells"],
        strict=True,
    ):
        assert original["hours"][6] is changed["hours"][6]
        assert (
            original["hours"][0]["surface"]["contributors"]
            is changed["hours"][0]["surface"]["contributors"]
        )
    replay, repeated = apply_temperature_correction(
        forecast, policy, analysis_cutoff=DECISION, mode=mode
    )
    assert canonical_json_bytes(result) == canonical_json_bytes(replay)
    assert overlay == repeated


def test_corrected_cold_temperature_uses_existing_reject_not_clamp_dew_semantics():
    result, overlay = apply_temperature_correction(
        _forecast(), _policy(role="active", bias=6), analysis_cutoff=DECISION
    )
    fields = result["hours"][0]["surface"]["fields"]
    assert fields[TEMPERATURE]["value"] == 284.0
    assert fields[DEW_POINT]["value"] is None and fields[DEW_POINT]["status"] == "inconsistent"
    assert fields[RH]["value"] is None
    assert overlay["coherence"]["relationships"] == [
        "blended_dew_point_consistency",
        "relative_humidity",
    ]


def test_missing_temperature_stays_missing_and_failed_active_transform_returns_whole_parent(
    monkeypatch,
):
    forecast = _forecast()
    forecast["local_grid_baseline"]["cells"][0]["hours"][0]["temperature"]["value"] = None
    result, overlay = apply_temperature_correction(
        forecast, _policy(role="active"), analysis_cutoff=DECISION
    )
    assert overlay["status"] == "applied"
    assert result["local_grid_baseline"]["cells"][0]["hours"][0]["temperature"]["value"] is None
    before = canonical_json_bytes(forecast)
    monkeypatch.setattr(
        BASELINE_COHERENCE,
        "apply_local_fields",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("broken diagnostic")),
    )
    original, failed = apply_temperature_correction(
        forecast, _policy(role="active"), analysis_cutoff=DECISION
    )
    assert original is forecast and failed["status"] == "fallback"
    assert canonical_json_bytes(forecast) == before and failed["changes"] == []


def test_future_policy_creation_activation_and_changed_science_are_rejected():
    forecast = _forecast()
    for cutoff in (CUTOFF, CREATED - timedelta(seconds=1)):
        result, outcome = apply_temperature_correction(
            forecast, _policy(role="active"), analysis_cutoff=cutoff
        )
        assert result is forecast and outcome["status"] == "fallback"
    for mutation in ("activated_at", "digest", "threshold"):
        policy = deepcopy(_policy(role="active"))
        if mutation == "activated_at":
            policy[mutation] = (DECISION + timedelta(seconds=1)).isoformat()
        elif mutation == "threshold":
            policy["lead_buckets"]["1-6"]["evidence"]["criteria"]["min_canonical_samples"][
                "required"
            ] = 2
        policy["digest"] = (
            str(canonical_json_digest({k: v for k, v in policy.items() if k != "digest"}))
            if mutation != "digest"
            else "changed"
        )
        result, outcome = apply_temperature_correction(forecast, policy, analysis_cutoff=DECISION)
        assert result is forecast and outcome["status"] == "fallback"
    analysis = _analysis()
    analysis.pop("evaluation")
    with pytest.raises(ValueError, match="cutoff-filtered"):
        propose_temperature_policy(
            analysis, policy_id="test", version="1", evidence_cutoff=CUTOFF, created_at=CREATED
        )
