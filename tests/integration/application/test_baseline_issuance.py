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


def test_two_locations_issue_one_baseline_lineage_read_exactly_and_preserve_history(
    baseline_case,
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
        baseline_case["baseline"],
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
    assert [len(items) for items in before] == [3, 3, 3]
    repeat = forecast_from_baseline(
        baseline_case["baseline"],
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
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observation availability never gates the real immutable issuance transaction."""
    service = ForecastIssuanceService(
        object_store,
        lambda: PostgresUnitOfWork(migrated_dsn),
        code_identity={"test": "verification-isolated-baseline-integration"},
    )
    calls = []

    def verify(latitude, longitude):
        calls.append((latitude, longitude))
        if latitude == FIRST["lat"]:
            raise OSError("Prior observation storage/provider unavailable")
        return {"qpf": {"status": "retryable", "reason": "MRMS object not yet published"}}

    forbidden = baseline_tests.forbid_location_calculation(monkeypatch)
    result = forecast_from_baseline(
        baseline_case["baseline"],
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
    for row in (result["results"][0], result["results"][2]):
        saved = service.read(UUID(row["issued"]["issued_forecast_id"]))
        assert saved["forecast"] == row["forecast"]
        coords = (saved["latitude"], saved["longitude"])
        assert saved["forecast"]["hours"] == baseline_case["expected"][coords]["hours"]
    assert [
        len(items) for items in storage_tests.storage_inventory(migrated_dsn, object_store)
    ] == [
        2,
        2,
        2,
    ]
    forbidden.assert_not_called()
