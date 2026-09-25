"""Sparse background candidates inherit native evidence and never change the parent."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from mesoforge.application import candidate_baseline as candidates
from mesoforge.application.baseline_snapshot import load_baseline
from mesoforge.application.candidate_baseline import (
    build_candidate_overlay,
    candidate_point_overlay,
)
from mesoforge.contracts.serialization import canonical_json_digest
from mesoforge.forecasting.coherence import DEW_POINT, RH, TEMPERATURE
from mesoforge.forecasting.field_blend import BlendState, FieldBlendEngine
from mesoforge.forecasting.recipes import ContributorConfiguration
from tests.unit.application.test_baseline_snapshot import (
    LOCATIONS,
    REFERENCES,
)
from tests.unit.application.test_baseline_snapshot import (
    baseline_case as baseline_case,  # noqa: F401
)
from tests.unit.forecasting.test_candidate_policy import temperature_policy


def test_background_overlay_reuses_saved_native_state_and_is_reproducible(
    baseline_case, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Candidate attempted native acquisition or preparation")

    monkeypatch.setattr("requests.Session", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("mesoforge.application.build_baseline.load_preparation", forbidden)
    pinned = load_baseline(baseline_case["baseline"])
    published = pinned.pointer["published_at"]
    cutoff = datetime.fromisoformat(published) + timedelta(seconds=1)
    policy = temperature_policy(lead_start=1, lead_end=6)
    original_provenance = deepcopy(policy.provenance)
    original_engine = candidates._engine

    def engine(grid, pinned_policy):
        # A caller retains this otherwise-frozen model; changing its nested metadata
        # after execution begins must not change the policy recorded in the overlay.
        policy.provenance["caller_mutated_during_build"] = True
        assert pinned_policy.provenance == original_provenance
        return original_engine(grid, pinned_policy)

    monkeypatch.setattr(candidates, "_engine", engine)
    parent_manifest = deepcopy(pinned.manifest)
    first = build_candidate_overlay(pinned, policy, analysis_cutoff=cutoff)
    assert first["overlay"]["policy"]["provenance"] == original_provenance
    policy.provenance.clear()
    policy.provenance.update(original_provenance)
    monkeypatch.setattr(candidates, "_engine", original_engine)
    second = build_candidate_overlay(pinned, policy, analysis_cutoff=cutoff)
    assert first["overlay"] == second["overlay"]
    assert first["sha256"] == second["sha256"]
    assert first["network_calls"] == 0
    assert pinned.manifest == parent_manifest
    overlay = first["overlay"]
    assert len(overlay["domains"]) == len(LOCATIONS) * len(REFERENCES)
    assert overlay["baseline_snapshot_id"] == pinned.manifest["baseline_snapshot_id"]
    assert overlay["prepared_snapshot_id"] == pinned.manifest["prepared_snapshot"]["snapshot_id"]
    assert overlay["affected_fields"] == [TEMPERATURE, DEW_POINT, RH]
    for location in LOCATIONS:
        old = pinned.reference_view(REFERENCES[0]).forecast(
            latitude=location["lat"], longitude=location["lon"]
        )
        identity_before = canonical_json_digest(old)
        patch = candidate_point_overlay(
            overlay,
            latitude=location["lat"],
            longitude=location["lon"],
            reference_time=REFERENCES[0],
        )
        assert [hour["horizon_hours"] for hour in patch] == list(range(1, 7))
        for candidate, hour in zip(patch, old["hours"], strict=False):
            values = {
                model: {TEMPERATURE: row["fields"][TEMPERATURE]["value"]}
                for model, row in hour["surface"]["contributors"].items()
                if TEMPERATURE in row["fields"]
            }
            configuration = ContributorConfiguration.model_validate_json(
                json.dumps(old["contributor_configuration"])
            )
            reference = FieldBlendEngine(
                contributors=configuration, policy_overrides={TEMPERATURE: policy.parameters}
            ).blend_field(
                TEMPERATURE, BlendState(horizon=hour["horizon_hours"], contributors=values)
            )
            assert candidate["fields"][TEMPERATURE] == reference
            assert set(candidate["fields"]) == {TEMPERATURE, DEW_POINT, RH}
            assert "contributors" not in candidate
            assert candidate["coherence_reference"] in overlay["coherence_reports"]
        assert canonical_json_digest(old) == identity_before
    assert first["bytes"] < sum(
        row["artifact"]["uncompressed_bytes"] for row in pinned.manifest["domains"]
    )
    with pytest.raises(ValueError, match="parent policy"):
        build_candidate_overlay(
            pinned,
            temperature_policy(parent_policy="not-the-control"),
            analysis_cutoff=cutoff,
        )
    with pytest.raises(ValueError, match="after its analysis cutoff"):
        build_candidate_overlay(pinned, policy, analysis_cutoff=policy.created_at)
