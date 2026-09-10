"""Read-only preview wiring over saved versions and explicit offline observation inputs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mesoforge import api
from mesoforge.application import observation_preview
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.catalog.configuration import ObservationNormalizationPolicy
from mesoforge.catalog.stations import StationDefinition
from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.contracts.observations_v2 import NormalizedObservationV2
from tests.support.in_memory_uow import InMemoryObjectStore, InMemoryUnitOfWork
from tests.unit import test_issued_forecast_api as retrieval_tests

prepared_guidance = retrieval_tests.prepared_guidance
saved_versions = retrieval_tests.saved_versions

_VALID_TIME = "2026-08-30T13:00:00Z"
_OBSERVATIONS = ArtifactId("art_00000000-0000-0000-0000-000000000010")
_SNAPSHOT = ArtifactId("art_00000000-0000-0000-0000-000000000011")
_RAW = ArtifactId("art_00000000-0000-0000-0000-000000000012")


@pytest.fixture()
def retained_inputs():
    station = StationDefinition.model_validate(
        {
            "station_id": "station.test",
            "provider_icao_id": "KTST",
            "expected_latitude": 45.81,
            "expected_longitude": -93.1,
            "expected_elevation_m": 290.0,
            "site_name": "Synthetic offline preview station",
            "provider_site_types": ("METAR",),
            "provider_priority": 0,
        }
    )
    event = datetime.fromisoformat(_VALID_TIME)
    observation = NormalizedObservationV2.model_validate(
        {
            "logical_observation_digest": Digest.of_bytes(b"synthetic observation identity"),
            "revision_digest": Digest.of_bytes(b"synthetic observation revision"),
            "station_id": station.station_id,
            "provider_station_id": station.provider_icao_id,
            "event_time": event,
            "report_time": event,
            "provider_available_at": event + timedelta(minutes=1),
            "ingested_at": event + timedelta(minutes=2),
            "metar_type": "METAR",
            "raw_observation": "SYNTHETIC TEST INPUT",
            "raw_record_digest": Digest.of_bytes(b"synthetic raw record"),
            "raw_artifact_id": _RAW,
            "raw_record_index": 0,
            "station_snapshot_artifact_id": _SNAPSHOT,
            "latitude_degrees": station.expected_latitude,
            "longitude_degrees": station.expected_longitude,
            "elevation_m": station.expected_elevation_m,
            "temperature_k": 284.15,
            "mesoforge_qc_state": "eligible",
        }
    )
    return (
        [observation],
        {_SNAPSHOT: (station,)},
        ObservationNormalizationPolicy(policy_id="metar-normalization.v1"),
        {"observations": {"artifact_id": str(_OBSERVATIONS)}, "notice": "Synthetic test inputs"},
    )


@pytest.fixture()
def preview_client(prepared_guidance, saved_versions, monkeypatch):
    service, _, _, _ = saved_versions
    monkeypatch.setattr(observation_preview, "read_issued_forecast", service.read)
    monkeypatch.setenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID", str(_OBSERVATIONS))
    app = api.create_app(prepared_guidance)
    forbidden = Mock(side_effect=AssertionError("Preview attempted calculation or mutation"))
    for owner, name in (
        (PreparedPointForecast, "forecast"),
        (PreparedPointForecast, "from_directory"),
        (ForecastIssuanceService, "issue"),
        (InMemoryObjectStore, "put_if_absent"),
        (InMemoryUnitOfWork, "commit"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    with TestClient(app) as client:
        yield client
    forbidden.assert_not_called()


def _url(identifier) -> str:
    return f"/issued-forecasts/{identifier}/observation-match"


def test_preview_preserves_each_saved_hour_and_context_without_writes(
    preview_client, saved_versions, retained_inputs, monkeypatch
):
    _, factory, objects, records = saved_versions
    before = dict(objects.objects)
    inputs = Mock(return_value=retained_inputs)
    monkeypatch.setattr(observation_preview, "_retained_inputs", inputs)
    for record in records:
        original = json.loads(before[record.content_digest])
        response = preview_client.get(
            _url(record.issued_forecast_id), params={"valid_time": _VALID_TIME}
        )
        repeated = preview_client.get(
            _url(record.issued_forecast_id), params={"valid_time": _VALID_TIME}
        )
        assert response.status_code == repeated.status_code == 200
        assert response.json() == repeated.json()
        payload = response.json()
        assert payload["status"] == "matched"
        assert payload["issued_forecast_id"] == str(record.issued_forecast_id)
        assert payload["issued_at"] == original["issued_at"]
        assert payload["forecast"] == {
            "latitude": original["latitude"],
            "longitude": original["longitude"],
            **original["forecast"]["hours"][0],
        }
        assert payload["forecast"]["temperature"]["value"] == pytest.approx(283.0, abs=1e-10)
        assert payload["forecast"]["temperature"]["unit"] == "K"
        assert payload["forecast_context"] == {
            key: value for key, value in original["forecast"].items() if key != "hours"
        }
        assert payload["forecast_code_identity"] == original["code_identity"]
        assert payload["input_provenance"] == retained_inputs[3]
        selected = payload["selected"]
        assert selected["station_id"] == "KTST"
        assert selected["temperature"] == {"value": 284.15, "unit": "K"}
        assert selected["observation_time"] == _VALID_TIME
        assert selected["qc"]["temperature_eligible"] is True
        assert selected["provenance"]["raw_artifact_id"] == str(_RAW)
        assert payload["unavailable_reason"] is None
        assert "error" not in payload
        assert "verification" not in payload
    assert inputs.call_count == 4
    assert all(call.args == (_OBSERVATIONS,) for call in inputs.call_args_list)
    assert objects.objects == before
    assert len(factory.issued_forecasts) == len(factory.stored_objects) == 2


@pytest.mark.parametrize(
    "identifier,valid_time",
    [
        ("not-a-uuid", _VALID_TIME),
        ("123", _VALID_TIME),
        (str(uuid4()), None),
        (str(uuid4()), ""),
        (str(uuid4()), "not-a-time"),
        (str(uuid4()), "2026-08-30T13:00:00"),
    ],
)
def test_invalid_query_returns_specific_422_before_readback(
    preview_client, monkeypatch, identifier, valid_time
):
    forbidden = Mock(side_effect=AssertionError("Invalid query attempted readback"))
    monkeypatch.setattr(observation_preview, "read_issued_forecast", forbidden)
    params = {} if valid_time is None else {"valid_time": valid_time}
    response = preview_client.get(_url(identifier), params=params)
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_observation_match_query",
            "message": "Provide an issued-forecast UUID and timezone-aware ISO valid_time.",
        }
    }
    forbidden.assert_not_called()


@pytest.mark.parametrize("unknown", ["id", "hour"])
def test_unknown_id_or_valid_time_returns_404_before_observation_loading(
    preview_client, saved_versions, monkeypatch, unknown
):
    _, _, _, records = saved_versions
    forbidden = Mock(side_effect=AssertionError("Unknown hour attempted observation loading"))
    monkeypatch.setattr(observation_preview, "_retained_inputs", forbidden)
    identifier = uuid4() if unknown == "id" else records[0].issued_forecast_id
    valid_time = _VALID_TIME if unknown == "id" else "2026-08-30T13:01:00Z"
    response = preview_client.get(_url(identifier), params={"valid_time": valid_time})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "issued_forecast_hour_not_found"
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("secret DSN"),
        IntegrityError("secret object path"),
        NotFound("secret configured artifact"),
    ],
)
def test_input_storage_failures_are_sanitized_500_not_unknown_forecast(
    preview_client, saved_versions, monkeypatch, failure
):
    _, _, _, records = saved_versions
    monkeypatch.setattr(observation_preview, "_retained_inputs", Mock(side_effect=failure))
    response = preview_client.get(
        _url(records[0].issued_forecast_id), params={"valid_time": _VALID_TIME}
    )
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "observation_match_failed",
            "message": "Could not read and verify the retained observation-match inputs.",
        }
    }
    assert "secret" not in response.text


def test_corrupt_saved_forecast_fails_without_loading_observations(
    preview_client, saved_versions, monkeypatch
):
    _, _, objects, records = saved_versions
    objects.objects[records[0].content_digest] = b"corrupt"
    forbidden = Mock(side_effect=AssertionError("Corrupt forecast attempted observation loading"))
    monkeypatch.setattr(observation_preview, "_retained_inputs", forbidden)
    response = preview_client.get(
        _url(records[0].issued_forecast_id), params={"valid_time": _VALID_TIME}
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "observation_match_failed"
    forbidden.assert_not_called()


def test_missing_forecast_temperature_is_unavailable_before_observation_loading(
    preview_client, saved_versions, monkeypatch
):
    service, _, _, records = saved_versions
    saved = service.read(records[0].issued_forecast_id)
    saved["forecast"]["hours"][0]["temperature"]["value"] = None
    saved["forecast"]["hours"][0]["missing_reasons"] = ["HRRR: prepared guidance file is missing"]
    monkeypatch.setattr(observation_preview, "read_issued_forecast", Mock(return_value=saved))
    forbidden = Mock(side_effect=AssertionError("Missing forecast attempted observation loading"))
    monkeypatch.setattr(observation_preview, "_retained_inputs", forbidden)
    response = preview_client.get(
        _url(records[0].issued_forecast_id), params={"valid_time": _VALID_TIME}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "unavailable"
    assert payload["unavailable_reason"] == "The saved forecast temperature is missing."
    assert payload["forecast"]["missing_reasons"] == ["HRRR: prepared guidance file is missing"]
    assert payload["selected"] is None
    forbidden.assert_not_called()


def test_unconfigured_dataset_is_explicitly_unavailable_without_loading_inputs(
    preview_client, saved_versions, monkeypatch
):
    _, _, _, records = saved_versions
    monkeypatch.delenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID")
    forbidden = Mock(
        side_effect=AssertionError("Unconfigured preview attempted observation loading")
    )
    monkeypatch.setattr(observation_preview, "_retained_inputs", forbidden)
    response = preview_client.get(
        _url(records[0].issued_forecast_id), params={"valid_time": _VALID_TIME}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "unavailable"
    assert payload["unavailable_reason"] == "No retained observation dataset is configured."
    assert payload["selected"] is None
    assert payload["input_provenance"] is None
    forbidden.assert_not_called()


def test_no_observation_match_preserves_forecast_and_candidate_explanations(
    preview_client, saved_versions, retained_inputs, monkeypatch
):
    _, _, _, records = saved_versions
    _, stations, policy, provenance = retained_inputs
    monkeypatch.setattr(
        observation_preview,
        "_retained_inputs",
        Mock(return_value=([], stations, policy, provenance)),
    )
    response = preview_client.get(
        _url(records[0].issued_forecast_id), params={"valid_time": _VALID_TIME}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "unavailable"
    assert payload["selected"] is None
    assert payload["forecast"]["valid_time"] == _VALID_TIME
    assert payload["unavailable_reason"]
    assert payload["candidates"][0]["reasons"] == ["no_retained_observation"]


def test_timezone_offset_selects_exact_saved_instant(preview_client, saved_versions, monkeypatch):
    _, _, _, records = saved_versions
    monkeypatch.delenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID")
    response = preview_client.get(
        _url(records[0].issued_forecast_id), params={"valid_time": "2026-08-30T08:00:00-05:00"}
    )
    assert response.status_code == 200
    assert response.json()["forecast"]["valid_time"] == _VALID_TIME


def test_direct_application_rejects_naive_time_before_readback(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Naive time attempted readback"))
    monkeypatch.setattr(observation_preview, "read_issued_forecast", forbidden)
    with pytest.raises(ValueError, match="timezone"):
        observation_preview.preview_observation_match(uuid4(), datetime(2026, 8, 30, 13))
    forbidden.assert_not_called()
