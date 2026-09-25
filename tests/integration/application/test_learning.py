"""Compact shared learning artifacts use real PostgreSQL/MinIO, never providers."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.application.learning import LearningService
from mesoforge.common.identifiers import ArtifactId
from mesoforge.contracts.forecast_variants import seal_variant
from mesoforge.forecasting.coherence import QPF
from tests.integration.application import test_issued_qpf_verification as qpf_tests
from tests.support.observation_preview import complete_storage_inventory
from tests.unit.application.test_corrections import DECISION, _analysis, _forecast, _policy
from tests.unit.contracts.test_forecast_variants import body

pytestmark = pytest.mark.integration
migrated_dsn = qpf_tests.migrated_dsn
object_store = qpf_tests.object_store
infrastructure = qpf_tests.infrastructure


def test_real_stage_persistence_noop_active_shadow_and_policy_immutability(
    infrastructure, monkeypatch
):
    env = infrastructure
    service = LearningService(env.verifier, clock=lambda: DECISION + timedelta(minutes=1))
    analysis = _analysis(qualified=False)
    analysis["evaluation"]["evidence_cutoff"] = DECISION.isoformat()
    monkeypatch.setattr(service, "evidence", lambda *args: analysis)
    forecast = _forecast()
    forecast["baseline_snapshot"] = {
        "baseline_snapshot_id": "fixture-baseline",
        "prepared_snapshot_id": "fixture-prepared",
        "forecast_analysis_cutoff": DECISION.isoformat(),
        "field_policies": {"temperature": "temperature_control_v1"},
    }
    original = deepcopy(forecast)
    result, report = service.local_stage(forecast)
    assert report["status"] == "no_policy" and not report["failures"]
    assert result["hours"] == original["hours"]
    assert result["local_grid_baseline"] == original["local_grid_baseline"]
    saved = service.read(ArtifactId(report["operational_reference"]["artifact_id"]))
    assert saved["payload"] == report["operational_stage"]
    assert saved["byte_size"] < 15_000

    policy = _policy(role="active")
    with ThreadPoolExecutor(max_workers=2) as executor:
        registered = list(executor.map(lambda _: service.register_policy(policy), range(2)))
    assert registered[0]["artifact_id"] == registered[1]["artifact_id"]
    changed = _policy(role="active", bias=2)
    with pytest.raises(ValueError, match="different immutable policy"):
        service.register_policy(changed)
    corrected, corrected_report = service.local_stage(
        forecast, policy_ids=[registered[0]["artifact_id"]]
    )
    assert corrected_report["status"] == "applied", corrected_report["failures"]
    assert corrected["hours"][0]["temperature"]["value"] == 289.0
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
