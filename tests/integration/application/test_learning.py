"""Compact shared learning artifacts use real PostgreSQL/MinIO, never providers."""

import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.application.governance import GovernanceService
from mesoforge.application.learning import LearningService
from mesoforge.common.identifiers import ArtifactId
from mesoforge.contracts.forecast_variants import seal_variant
from mesoforge.contracts.policy_governance import correction_scope
from mesoforge.forecasting.coherence import QPF
from tests.integration.application import test_issued_qpf_verification as qpf_tests
from tests.support.governance import (
    append_event,
    fixture_evaluation,
    past_policy,
    shift_forecast,
)
from tests.support.observation_preview import complete_storage_inventory
from tests.unit.application.test_corrections import DECISION, _analysis, _forecast, _policy
from tests.unit.contracts.test_forecast_variants import body

pytestmark = pytest.mark.integration
migrated_dsn = qpf_tests.migrated_dsn
object_store = qpf_tests.object_store
infrastructure = qpf_tests.infrastructure


def test_real_stage_persistence_noop_governed_active_and_policy_immutability(
    infrastructure, monkeypatch
):
    env = infrastructure
    now = datetime.now(UTC)
    decided = (now - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    service = LearningService(env.verifier)
    governance = GovernanceService(service)
    fixture = _forecast()
    fixture["baseline_snapshot"] = {
        "baseline_snapshot_id": "fixture-baseline",
        "prepared_snapshot_id": "fixture-prepared",
        "forecast_analysis_cutoff": DECISION.isoformat(),
        "field_policies": {"temperature": "temperature_control_v1"},
    }
    forecast = shift_forecast(fixture, decided - DECISION)
    analysis = _analysis(qualified=False)
    analysis["evaluation"]["evidence_cutoff"] = decided.isoformat()
    analysis["samples"] = []  # no qualified evidence at this past decision
    monkeypatch.setattr(service, "evidence", lambda *args: analysis)
    original = deepcopy(forecast)
    result, report = service.local_stage(forecast)
    assert report["status"] == "no_policy" and not report["failures"]
    assert result["hours"] == original["hours"]
    assert result["local_grid_baseline"] == original["local_grid_baseline"]
    saved = service.read(ArtifactId(report["operational_reference"]["artifact_id"]))
    assert saved["payload"] == report["operational_stage"]
    assert saved["byte_size"] < 15_000

    times = {
        "evidence_cutoff": decided - timedelta(days=3),
        "created_at": decided - timedelta(days=2),
    }
    policy = past_policy(_policy(), **times)
    with ThreadPoolExecutor(max_workers=2) as executor:
        registered = list(executor.map(lambda _: service.register_policy(policy), range(2)))
    assert registered[0]["artifact_id"] == registered[1]["artifact_id"]
    with pytest.raises(ValueError, match="different immutable policy"):
        service.register_policy(past_policy(_policy(bias=2), **times))
    # Storing a policy never executes it: the governed scope resolves nothing yet.
    scope = correction_scope(forecast["latitude"], forecast["longitude"])
    resolved = governance.resolve("temperature_correction", scope, datetime.now(UTC))
    assert resolved["status"] == "resolved_none"
    governance.register(ArtifactId(registered[0]["artifact_id"]), actor="it", reason="shadow")
    activated = append_event(
        env.verifier.factory,
        "ACTIVATED",
        family="temperature_correction",
        scope_key=scope,
        policy=registered[0]["artifact_id"],
        content_digest=registered[0]["content_digest"],
        evaluation=fixture_evaluation(service)["artifact_id"],
    )
    # A later job's request time follows the database stamp of the activation.
    time.sleep(max(0.0, (activated.recorded_at - datetime.now(UTC)).total_seconds()) + 0.05)
    request = datetime.now(UTC)
    current = deepcopy(forecast)
    current["baseline_snapshot"]["forecast_analysis_cutoff"] = request.isoformat()
    coordinate = (forecast["latitude"], forecast["longitude"])
    snapshot = governance.correction_snapshot([coordinate], request)
    corrected, corrected_report = service.local_stage(current, governance=snapshot.scope(scope))
    assert corrected_report["status"] == "applied", corrected_report["failures"]
    assert corrected["hours"][0]["temperature"]["value"] == 289.0
    stage = service.read(ArtifactId(corrected_report["operational_reference"]["artifact_id"]))
    assert stage["payload"]["governance_resolution"]["status"] == "resolved_active"
    assert stage["payload"]["policy"]["digest"] == registered[0]["content_digest"]
    assert forecast == original
    assert service.read(ArtifactId(saved["artifact_id"]))["payload"] == saved["payload"]


def test_qpf_shared_canonical_evaluation_binding_repeat_and_read_only(infrastructure):
    env = infrastructure
    service = LearningService(env.verifier)
    env.verifier.verify(
        env.issued.issued_forecast_id, qpf_tests.VALID, extraction_id=env.extraction_id
    )
    value = body(
        fields=[QPF],
        baseline_snapshot_id="retained-baseline",
        prepared_snapshot_id="fixture",
        lifecycle_role="active",
        transformation_type="deterministic_corrected",
        policy={"id": "no-policy", "version": "1", "digest": None},
        location={"latitude": qpf_tests.LAT, "longitude": qpf_tests.LON},
        reference_time="2026-09-24T11:00:00Z",
        analysis_cutoff="2026-09-24T11:00:00Z",
        evidence_required=False,
        evidence_status="no_policy",
        evidence_cutoff=None,
        policy_created_at=None,
        policy_activated_at=None,
        created_at=datetime.now(UTC).isoformat(),
        overlay={"inherit_unchanged": True, "predictions": []},
    )
    control = seal_variant(
        {
            **value,
            "parent_stage_id": None,
            "transformation_type": "active_baseline",
            "evidence_status": "baseline",
            "overlay": {
                "inherit_unchanged": False,
                "predictions": [
                    {
                        "field": QPF,
                        "valid_time": qpf_tests.VALID.isoformat(),
                        "interval_start": (qpf_tests.VALID - timedelta(hours=1)).isoformat(),
                        "interval_end": qpf_tests.VALID.isoformat(),
                        "unit": "mm",
                        "value": 2.5,
                    }
                ],
            },
        }
    )
    stage = seal_variant({**value, "parent_stage_id": control["variant_id"]})
    control_saved = service.save(
        "forecast-variant", control, attributes=service._attributes(control)
    )
    retained = service.save("forecast-variant", stage, attributes=service._attributes(stage))
    report = {
        "operational_reference": service._reference(retained),
        "control_reference": service._reference(control_saved),
    }
    issued = {"issued_forecast_id": str(env.issued.issued_forecast_id)}
    binding = service.bind(issued, report)
    before = complete_storage_inventory(env.dsn, env.objects)
    assert service.bind(issued, report) == binding
    result = service.analyze(
        QPF,
        qpf_tests.LAT,
        qpf_tests.LON,
        start=qpf_tests.VALID,
        end=qpf_tests.VALID + timedelta(hours=1),
    )
    assert result["shared_sample_count"] == 1, result
    metrics = next(iter(result["comparisons"].values()))
    assert metrics["control"] == metrics["variant"]
    assert metrics["control"]["mae"] == 1.0
    assert result["writes"] == 0
    assert complete_storage_inventory(env.dsn, env.objects) == before
