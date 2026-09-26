"""Background baselines use the existing immutable PostgreSQL/MinIO issuance path."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from mesoforge import api
from mesoforge.application.forecast_from_baseline import forecast_from_baseline
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.forecasting.conditions import build_conditions_preview
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from tests.integration.application import test_batch_issuance as storage_tests
from tests.unit.application import test_baseline_snapshot as baseline_tests
from tests.unit.application.test_batch_forecast import FIRST, LAST
from tests.unit.application.test_prepared_temperature import TARGET

pytestmark = pytest.mark.integration
baseline_case = baseline_tests.baseline_case
migrated_dsn = storage_tests.migrated_dsn
object_store = storage_tests.object_store
configured_retrieval_storage = storage_tests.configured_retrieval_storage


@pytest.fixture
def governed_baseline(baseline_case, configured_retrieval_storage: None, tmp_path: Path) -> Path:
    """The same pinned guidance rebuilt with blend governance resolved from the test store.

    Issuance refuses a baseline without resolved governance; with nothing ACTIVE the
    rebuilt fields equal the module baseline exactly.
    """
    from mesoforge.application import build_baseline as background
    from mesoforge.application.governance import configured_governance

    root = tmp_path / "governed-baseline"
    built = background.build_baseline(
        baseline_case["guidance"],
        root,
        [FIRST, LAST],
        reference_times=[TARGET],
        governance=configured_governance(),
    )
    assert built["manifest"]["blend_governance"]["status"] == "resolved"
    return root


def test_two_locations_issue_one_baseline_lineage_read_exactly_and_preserve_history(
    baseline_case,
    governed_baseline: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ForecastIssuanceService(
        object_store,
        lambda: PostgresUnitOfWork(migrated_dsn),
        code_identity={"test": "baseline-integration"},
    )
    # A legitimate historical point-only version remains readable as issued.
    historical = deepcopy(baseline_case["expected"][(FIRST["lat"], FIRST["lon"])])
    historical.pop("local_grid_baseline")
    historical.pop("local_grid")
    old = service.issue(historical, batch_run_id=uuid4(), location_index=0)
    old_saved = service.read(old.issued_forecast_id)

    forbidden = baseline_tests.forbid_location_calculation(monkeypatch)
    failed = {"lat": 45.85, "lon": -93.05}  # Valid CONUS point, not materialized in this baseline.
    locations = [FIRST, failed, LAST]
    # Explicit reissue is needed only because the historical FIRST version exists.
    result = forecast_from_baseline(
        governed_baseline,
        locations,
        reference_time=TARGET,
        request_time=datetime.now(UTC),
        issue=True,
        issuer=service,
        reissue=True,
        verify_prior=False,  # This historical readback fixture is explicitly issuance-only.
    )
    assert result["summary"] == {"ok": 2, "issued": 2, "skipped": 0, "failed": 1}
    assert result["results"][1]["error"]["code"] == "coverage_required"
    assert "issued" not in result["results"][1]
    saved_versions = []
    for row in (result["results"][0], result["results"][2]):
        issued_id = UUID(row["issued"]["issued_forecast_id"])
        saved = service.read(issued_id)
        assert saved["forecast"] == row["forecast"]
        lineage = saved["forecast"]["baseline_snapshot"]
        assert lineage["baseline_snapshot_id"] == result["baseline"]["baseline_snapshot_id"]
        assert lineage["prepared_snapshot_id"] == baseline_case["prepared"][1]["snapshot_id"]
        assert len(saved["forecast"]["local_grid_baseline"]["cells"]) == 49
        saved_versions.append(saved)

    before = storage_tests.storage_inventory(migrated_dsn, object_store)
    # Three issuances plus raw/corrected/binding artifacts per new location. No
    # provider is configured in offline tests, so no model action occurred and no AI
    # stage is retained. Learning uses the same PostgreSQL/MinIO path.
    assert [len(items) for items in before] == [3, 9, 9]
    repeat = forecast_from_baseline(
        governed_baseline,
        locations,
        reference_time=TARGET,
        issue=True,
        issuer=service,
        verify_prior=False,
    )
    assert repeat["summary"] == {"ok": 0, "issued": 0, "skipped": 2, "failed": 1}
    assert storage_tests.storage_inventory(migrated_dsn, object_store) == before
    assert service.read(old.issued_forecast_id) == old_saved

    no_writes = storage_tests.forbid_retrieval_writes_and_calculation(monkeypatch)
    control = Path(baseline_case["prepared"][1]["prepared_run"]["control_directory"])
    with TestClient(api.create_app(control)) as client:
        for saved in saved_versions:
            address = f"/issued-forecasts/{saved['issued_forecast_id']}"
            response = client.get(address)
            assert response.status_code == 200 and response.json() == saved
            conditions = client.get(address + "/conditions", params={"scope": "grid"})
            repeated = client.get(address + "/conditions", params={"scope": "grid"})
            assert conditions.status_code == repeated.status_code == 200
            assert conditions.content == repeated.content
            expected = build_conditions_preview(saved, scope="grid")
            assert conditions.json()["center_point"] == expected["center_point"]
            assert conditions.json()["cells"] == expected["cells"]
        historical_response = client.get(f"/issued-forecasts/{old.issued_forecast_id}")
        assert historical_response.status_code == 200 and historical_response.json() == old_saved
    assert storage_tests.storage_inventory(migrated_dsn, object_store) == before
    forbidden.assert_not_called()
    no_writes.assert_not_called()


def test_prior_verification_failure_does_not_block_baseline_issuance_or_later_locations(
    baseline_case,
    governed_baseline: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observation availability never gates the real immutable issuance transaction."""
    service = ForecastIssuanceService(
        object_store,
        lambda: PostgresUnitOfWork(migrated_dsn),
        code_identity={"test": "verification-isolated-baseline-integration"},
    )
    calls = []
    from mesoforge.contracts.forecast_desk import DeskConfig, DeskProviderUnavailableError
    from tests.unit.application.test_forecast_desk import (
        ASSESS,
        COMPLETE,
        PRIORITY,
        REVIEW,
        Provider,
        edit,
    )

    providers = iter(
        [
            Provider([DeskProviderUnavailableError("fixture provider unavailable")]),
            Provider(
                [
                    ASSESS,
                    PRIORITY,
                    lambda p: edit(p, operation="add", amount=0.25),
                    COMPLETE,
                    REVIEW,
                ]
            ),
        ]
    )
    monkeypatch.setattr(
        "mesoforge.application.forecast_desk_provider.provider_from_environment",
        lambda: next(providers),
    )
    monkeypatch.setattr(
        "mesoforge.application.forecast_desk_provider.config_from_environment",
        lambda: DeskConfig(max_total_tokens=300000),
    )

    def verify(latitude, longitude):
        calls.append((latitude, longitude))
        if latitude == FIRST["lat"]:
            raise OSError("Prior observation storage/provider unavailable")
        return {"qpf": {"status": "retryable", "reason": "MRMS object not yet published"}}

    forbidden = baseline_tests.forbid_location_calculation(monkeypatch)
    result = forecast_from_baseline(
        governed_baseline,
        [FIRST, {"lat": 999, "lon": 0}, LAST],
        reference_time=TARGET,
        issue=True,
        issuer=service,
        verification_runner=verify,
    )
    assert result["summary"] == {"ok": 2, "issued": 2, "skipped": 0, "failed": 1}
    assert calls == [(FIRST["lat"], FIRST["lon"]), (LAST["lat"], LAST["lon"])]
    assert result["results"][0]["previous_verification"]["status"] == "error"
    assert result["results"][2]["previous_verification"]["qpf"]["status"] == "retryable"
    first, last = result["results"][0], result["results"][2]
    assert first["learning"]["ai"]["completion_reason"] == "provider_unavailable"
    assert (
        first["forecast"]["hours"]
        == baseline_case["expected"][(FIRST["lat"], FIRST["lon"])]["hours"]
    )
    assert last["learning"]["ai"]["usage"]["accepted_edits"] == 1, last["learning"]["ai"]
    qpf = "liquid_equivalent_precipitation_amount_1h"
    old = baseline_case["expected"][(LAST["lat"], LAST["lon"])]["hours"][0]["surface"]["fields"][
        qpf
    ]
    new = last["forecast"]["hours"][0]["surface"]["fields"][qpf]
    assert new["value"] == old["value"] + 0.25
    assert new["interval_start"] == old["interval_start"]
    # An unavailable provider produced no model action: the corrected stage is issued.
    assert first["forecast"]["learning_stage"]["transformation_type"] == "deterministic_corrected"
    assert first["forecast"]["ai_desk"]["issued_checkpoint"] == "deterministic_corrected"
    assert last["forecast"]["learning_stage"]["transformation_type"] == "ai_adjusted"
    assert (
        last["forecast"]["deterministic_stage"]["variant_id"]
        == (last["forecast"]["learning_stage"]["parent_stage_id"])
    )
    for row in (result["results"][0], result["results"][2]):
        saved = service.read(UUID(row["issued"]["issued_forecast_id"]))
        assert saved["forecast"] == row["forecast"]
        coords = (saved["latitude"], saved["longitude"])
        original_hours = baseline_case["expected"][coords]["hours"]
        assert saved["forecast"]["hours"][1:] == original_hours[1:]
        assert [hour["temperature"] for hour in saved["forecast"]["hours"]] == [
            hour["temperature"] for hour in original_hours
        ]
        assert (
            saved["forecast"]["hours"][0]["surface"]["contributors"]
            == original_hours[0]["surface"]["contributors"]
        )
    # Presentation is derived from the AI-final grid and passes its content checks.
    edited = service.read(UUID(last["issued"]["issued_forecast_id"]))
    preview = build_conditions_preview(edited, scope="grid")
    assert preview["center_point"]
    control = Path(baseline_case["prepared"][1]["prepared_run"]["control_directory"])
    with TestClient(api.create_app(control)) as client:
        address = f"/issued-forecasts/{edited['issued_forecast_id']}/conditions"
        for suffix in ("", "/transitions", "/periods"):
            assert client.get(address + suffix).status_code == 200, suffix
    assert [
        len(items) for items in storage_tests.storage_inventory(migrated_dsn, object_store)
    ] == [
        2,
        10,
        10,
    ]
    forbidden.assert_not_called()
