"""The accumulation-status route only forwards validated coordinates to the read-only status."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from mesoforge import api
from tests.unit.test_issued_forecast_api import prepared_guidance as prepared_guidance


@pytest.fixture()
def client(prepared_guidance):
    with TestClient(api.create_app(prepared_guidance)) as test_client:
        yield test_client


def test_status_is_returned_for_a_coordinate(client, monkeypatch):
    payload = {"schema_version": "mesoforge.accumulation-status.v1", "hours": {"total": 72}}
    reader = Mock(return_value=payload)
    monkeypatch.setattr(api, "accumulation_status", reader)
    response = client.get("/accumulation-status", params={"lat": 44.98859, "lon": -93.25557})
    assert response.status_code == 200
    assert response.json() == payload
    reader.assert_called_once_with(44.98859, -93.25557)


def test_invalid_coordinates_are_rejected(client, monkeypatch):
    reader = Mock(side_effect=ValueError("latitude out of range"))
    monkeypatch.setattr(api, "accumulation_status", reader)
    response = client.get("/accumulation-status", params={"lat": 95, "lon": 0})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_coordinate"
    missing = client.get("/accumulation-status", params={"lat": "north"})
    assert missing.status_code == 422
    reader.assert_called_once_with(95.0, 0.0)


def test_storage_trouble_is_reported_without_details(client, monkeypatch):
    monkeypatch.setattr(
        api, "accumulation_status", Mock(side_effect=RuntimeError("dsn=postgresql://secret"))
    )
    response = client.get("/accumulation-status", params={"lat": 45, "lon": -93})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "accumulation_status_failed"
    assert "secret" not in response.text
