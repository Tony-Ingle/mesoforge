"""Retained observation previews against actual PostgreSQL and MinIO adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mesoforge import api
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.guidance import acquisition_v2
from mesoforge.observations import acquisition
from mesoforge.storage.s3 import S3ArtifactObjectStore, content_addressed_key
from tests.integration.application import test_batch_issuance as issuance_tests
from tests.support.observation_preview import (
    complete_storage_inventory,
    seed_observation_preview_inputs,
)
from tests.unit.application.test_batch_forecast import FIRST

pytestmark = pytest.mark.integration

migrated_dsn = issuance_tests.migrated_dsn
object_store = issuance_tests.object_store
service = issuance_tests.service
prepared_guidance = issuance_tests.prepared_guidance
configured_retrieval_storage = issuance_tests.configured_retrieval_storage

VALID_TIME = datetime(2026, 8, 30, 13, tzinfo=UTC)


def forbid_preview_writes_and_downloads(monkeypatch: pytest.MonkeyPatch):
    forbidden = issuance_tests.forbid_retrieval_writes_and_calculation(monkeypatch)
    for owner, names in (
        (acquisition, ("acquire_stationinfo", "acquire_metar_batch")),
        (acquisition_v2, ("acquire_hrrr_phase2_lead", "acquire_gfs_lead", "acquire_nbm_lead")),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)
    return forbidden


def test_preview_selects_retained_observation_and_explains_candidates_without_any_writes(
    prepared_guidance: Path,
    migrated_dsn: str,
    service: ForecastIssuanceService,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = seed_observation_preview_inputs(migrated_dsn, object_store, VALID_TIME)
    monkeypatch.setenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID", str(observations.artifact_id))
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    record = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    original = service.read(record.issued_forecast_id)
    before = complete_storage_inventory(migrated_dsn, object_store)
    forbidden = forbid_preview_writes_and_downloads(monkeypatch)
    with TestClient(api.create_app(prepared_guidance)) as client:
        url = f"/issued-forecasts/{record.issued_forecast_id}/observation-match"
        response = client.get(url, params={"valid_time": "2026-08-30T13:00:00Z"})
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["status"] == "matched"
        assert preview["issued_forecast_id"] == str(record.issued_forecast_id)
        assert preview["issued_at"] == original["issued_at"]
        assert preview["forecast"]["latitude"] == FIRST["lat"]
        assert preview["forecast"]["longitude"] == FIRST["lon"]
        assert preview["forecast"]["valid_time"] == forecast["hours"][0]["valid_time"]
        assert preview["forecast"]["temperature"] == forecast["hours"][0]["temperature"]
        selected = preview["selected"]
        assert selected["station_id"] == "KROS"
        assert selected["network"] == "METAR"
        assert selected["latitude"] == 45.69624
        assert selected["longitude"] == -92.95427
        assert selected["elevation_m"] == 282.0
        assert 16.0 < selected["distance_km"] < 16.3
        assert selected["observation_time"] == "2026-08-30T13:10:00Z"
        # Independent conversion: the raw test input is 20 degrees Celsius.
        assert selected["temperature"] == {"value": 293.15, "unit": "K"}
        assert selected["qc"]["state"] == "eligible"
        assert selected["qc"]["flags"] == []
        candidates = {row["station_id"]: row for row in preview["candidates"]}
        assert set(candidates) == {"KCBG", "KJMR", "KROS"}
        assert candidates["KJMR"]["status"] == "excluded"
        assert "temperature_out_of_range" in candidates["KJMR"]["qc"]["flags"]
        assert candidates["KCBG"]["status"] == "eligible_not_selected"
        assert candidates["KCBG"]["observation_time"] == "2026-08-30T13:00:00Z"
        assert candidates["KCBG"]["distance_km"] > selected["distance_km"]
        assert selected["provenance"]["raw_artifact_id"]
        assert selected["provenance"]["revision_digest"].startswith("sha256:")
        assert preview["input_provenance"]
        assert "synthetic_observation_fixture" in response.text

        repeated = client.get(url, params={"valid_time": "2026-08-30T14:00:00+01:00"})
        assert repeated.status_code == 200
        assert repeated.json() == preview
        unavailable = client.get(url, params={"valid_time": "2026-08-30T15:00:00Z"})
        assert unavailable.status_code == 200
        missing = unavailable.json()
        assert missing["status"] == "unavailable"
        assert missing["selected"] is None
        assert missing["unavailable_reason"]
        assert all(row["status"] == "excluded" for row in missing["candidates"])
        assert client.get(f"/issued-forecasts/{record.issued_forecast_id}").json() == original
    forbidden.assert_not_called()
    # Captures every table, including artifacts and activity/error metadata:
    # unchanged counts alone would not detect updates to existing records.
    assert complete_storage_inventory(migrated_dsn, object_store) == before


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_preview_fails_closed_on_damaged_retained_observations_without_mutation(
    prepared_guidance: Path,
    migrated_dsn: str,
    service: ForecastIssuanceService,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    observations = seed_observation_preview_inputs(migrated_dsn, object_store, VALID_TIME)
    monkeypatch.setenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID", str(observations.artifact_id))
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    record = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    key = content_addressed_key(observations.content_digest)
    if damage == "corrupt":
        object_store._client.put_object(Bucket=object_store._bucket, Key=key, Body=b"{}")
    else:
        object_store._client.delete_object(Bucket=object_store._bucket, Key=key)
    before = complete_storage_inventory(migrated_dsn, object_store)
    forbidden = forbid_preview_writes_and_downloads(monkeypatch)
    with TestClient(api.create_app(prepared_guidance)) as client:
        response = client.get(
            f"/issued-forecasts/{record.issued_forecast_id}/observation-match",
            params={"valid_time": "2026-08-30T13:00:00Z"},
        )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "observation_match_failed"
    assert "selected" not in response.json()
    forbidden.assert_not_called()
    assert complete_storage_inventory(migrated_dsn, object_store) == before


def test_preview_unknown_id_and_hour_do_not_create_storage_or_hide_as_unavailable(
    prepared_guidance: Path,
    migrated_dsn: str,
    service: ForecastIssuanceService,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    record = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    monkeypatch.delenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID", raising=False)
    before = complete_storage_inventory(migrated_dsn, object_store)
    forbidden = forbid_preview_writes_and_downloads(monkeypatch)
    with TestClient(api.create_app(prepared_guidance)) as client:
        for identifier, time in (
            (uuid4(), "2026-08-30T13:00:00Z"),
            (record.issued_forecast_id, "2026-08-30T13:30:00Z"),
        ):
            response = client.get(
                f"/issued-forecasts/{identifier}/observation-match", params={"valid_time": time}
            )
            assert response.status_code == 404
            assert response.json()["error"]["message"]
        response = client.get(
            f"/issued-forecasts/{record.issued_forecast_id}/observation-match",
            params={"valid_time": "2026-08-30T13:00:00Z"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "unavailable"
        assert response.json()["unavailable_reason"]
    forbidden.assert_not_called()
    assert complete_storage_inventory(migrated_dsn, object_store) == before
