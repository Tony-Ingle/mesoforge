"""The verification-analysis route forwards validated input to the read-only analysis."""

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


def test_analysis_is_returned_for_a_coordinate(client, monkeypatch):
    payload = {
        "schema_version": "mesoforge.site-verification-analysis.v1",
        "correction_readiness": {"status": "insufficient_evidence"},
    }
    reader = Mock(return_value=payload)
    monkeypatch.setattr(api, "analyze_site_verification", reader)
    response = client.get("/verification-analysis", params={"lat": 44.98859, "lon": -93.25557})
    assert response.status_code == 200
    assert response.json() == payload
    reader.assert_called_once_with(44.98859, -93.25557, display_timezone=None)
    client.get(
        "/verification-analysis",
        params={"lat": 44.98859, "lon": -93.25557, "display_timezone": "America/Chicago"},
    )
    assert reader.call_args.kwargs == {"display_timezone": "America/Chicago"}


def test_invalid_coordinates_and_zones_are_rejected(client, monkeypatch):
    reader = Mock(side_effect=ValueError("bad input"))
    monkeypatch.setattr(api, "analyze_site_verification", reader)
    response = client.get("/verification-analysis", params={"lat": 95, "lon": 0})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_analysis_request"
    assert client.get("/verification-analysis", params={"lat": "north"}).status_code == 422
    reader.assert_called_once()


def test_storage_trouble_is_reported_without_details(client, monkeypatch):
    monkeypatch.setattr(
        api, "analyze_site_verification", Mock(side_effect=RuntimeError("dsn=postgresql://secret"))
    )
    response = client.get("/verification-analysis", params={"lat": 45, "lon": -93})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "verification_analysis_failed"
    assert "secret" not in response.text
