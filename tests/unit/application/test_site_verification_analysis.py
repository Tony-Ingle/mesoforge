"""Read-only analysis over storage doubles: inventory, legacy facts, reads and repeatability."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest

from mesoforge.application import issuance
from mesoforge.application import site_verification_analysis as module
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.site_verification_analysis import analyze_site_verification
from mesoforge.common.identifiers import Digest
from mesoforge.contracts.issued_forecasts import IssuedForecastRecord
from tests.unit.application.test_accumulation_status import manifest

LAT, LON = 44.98859, -93.25557
TARGET = datetime(2026, 9, 16, 22, tzinfo=UTC)
VERSION_A = UUID(int=10)
VERSION_B = UUID(int=11)
VERSION_C = UUID(int=12)
NOW = datetime(2026, 9, 17, 6, tzinfo=UTC)


def issued(version: UUID, target: datetime, minutes: int = 40, lat: float = LAT, lon: float = LON):
    return IssuedForecastRecord(
        issued_forecast_id=version,
        batch_run_id=UUID(int=1),
        location_index=0,
        latitude=lat,
        longitude=lon,
        issued_at=target + timedelta(minutes=minutes),
        target_reference_time=target,
        content_digest=Digest.of_bytes(str(version).encode()),
    )


def payload(
    record: IssuedForecastRecord,
    horizon: int,
    *,
    forecast: float,
    observed: float,
    station: str = "KMIC",
    revision: str = "rev-1",
    raw_artifact: str = "art_raw_1",
    zone: str | None = "America/Chicago",
    lat: float = LAT,
    lon: float = LON,
) -> dict[str, Any]:
    valid = record.target_reference_time + timedelta(hours=horizon)
    return {
        "schema_version": "issued-temperature-verification.v1",
        "status": "verified",
        "reasons": [],
        "temperature_error": {
            "value": forecast - observed,
            "unit": "K",
            "definition": "forecast_minus_observation",
        },
        "verification_cutoff": (valid + timedelta(minutes=30)).isoformat(),
        "verification_policy": {"policy_id": "issued-temperature-verification.v1"},
        "issued_forecast_digest": f"sha256:{record.issued_forecast_id}",
        "code_identity": {"git_commit": "a" * 40},
        "match": {
            "issued_forecast_id": str(record.issued_forecast_id),
            "issued_at": record.issued_at.isoformat().replace("+00:00", "Z"),
            "status": "matched",
            "selection_policy": {"max_distance_km": 50, "max_time_difference_minutes": 15},
            "forecast": {
                "latitude": lat,
                "longitude": lon,
                "valid_time": valid.isoformat().replace("+00:00", "Z"),
                "horizon_hours": horizon,
                "temperature": {"value": forecast, "unit": "K"},
                "sources": [
                    {"model": "HRRR", "temperature": {"value": forecast + 0.2, "unit": "K"}},
                    {"model": "GFS", "temperature": {"value": forecast - 0.4, "unit": "K"}},
                ],
                "shadow_sources": [{"model": "IFS", "temperature": {"value": None, "unit": "K"}}],
                "surface": {
                    "fields": {
                        "wind_speed_10m": {"value": 3.4, "unit": "m/s"},
                        "cloud_area_fraction": {"value": None, "unit": "1"},
                    }
                },
            },
            "forecast_context": {
                "target_reference_time": record.target_reference_time.isoformat(),
                "hourly_report": {"display_timezone": zone, "hours": ["large"] * 3},
            },
            "selected": {
                "station_id": station,
                "catalog_station_id": f"station.{station.lower()}",
                "network": "METAR",
                "provider": "aviationweather.gov",
                "latitude": 45.0623,
                "longitude": -93.35108,
                "elevation_m": 263,
                "distance_km": 11.1248,
                "observation_time": (valid - timedelta(minutes=7)).isoformat(),
                "time_difference_seconds": -420,
                "temperature": {"value": observed, "unit": "K"},
                "provenance": {
                    "revision_digest": f"sha256:{revision}",
                    "logical_observation_digest": f"sha256:logical-{station}-{valid.isoformat()}",
                    "raw_record_digest": "sha256:record",
                    "raw_artifact_id": raw_artifact,
                },
            },
            "input_provenance": {"observations": {"artifact_id": f"obs-for-{raw_artifact}"}},
            "candidates": [],
        },
    }


class Storage:
    """Issuance rows, fact manifests and payload bytes; any write or commit is a failure."""

    def __init__(self, records):
        self.records = tuple(records)
        self.indexed: list[Any] = []
        self.legacy: list[Any] = []
        self.payloads: dict[str, bytes] = {}
        self.loads: list[str] = []
        self.queries: list[tuple] = []

    def add(self, body: dict[str, Any] | bytes, *, indexed: bool = True, minutes: int = 0):
        attributes = None
        if indexed and isinstance(body, dict):
            forecast = body["match"]["forecast"]
            attributes = {
                "issued_forecast_id": body["match"]["issued_forecast_id"],
                "valid_time": forecast["valid_time"],
                "horizon_hours": forecast["horizon_hours"],
                "latitude": forecast["latitude"],
                "longitude": forecast["longitude"],
                "verification_status": "verified",
            }
        elif indexed:
            attributes = {"issued_forecast_id": "unreadable", "latitude": LAT, "longitude": LON}
        item = manifest(
            "issued-temperature-verification",
            "issued-temperature-verification.v1",
            attributes,
            registered_at=NOW - timedelta(hours=2) + timedelta(minutes=minutes),
        )
        self.payloads[str(item.artifact_id)] = (
            body if isinstance(body, bytes) else json.dumps(body).encode()
        )
        (self.indexed if indexed else self.legacy).append(item)
        return item

    def factory(self):
        storage = self

        class UnitOfWork:
            issued_forecasts = SimpleNamespace(list_for_coordinate=storage._list)
            artifacts = SimpleNamespace(
                find_issued_temperature_verifications=storage._indexed,
                find_unindexed_issued_temperature_verifications=storage._legacy,
            )

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def commit(self):
                raise AssertionError("The analysis never commits")

        return UnitOfWork()

    def _list(self, latitude, longitude, *, limit):
        self.queries.append(("issued", latitude, longitude, limit))
        return tuple(r for r in self.records if (r.latitude, r.longitude) == (latitude, longitude))

    def _indexed(self, *, latitude, longitude):
        self.queries.append(("indexed", latitude, longitude))
        return tuple(self.indexed)

    def _legacy(self, *, limit):
        self.queries.append(("legacy", limit))
        return tuple(self.legacy[:limit])

    def load(self, item):
        self.loads.append(str(item.artifact_id))
        return self.payloads[str(item.artifact_id)]


def analyze(storage: Storage, **kwargs: Any) -> dict[str, Any]:
    return analyze_site_verification(
        LAT,
        LON,
        now=kwargs.pop("now", NOW),
        unit_of_work_factory=storage.factory,
        load_payload=storage.load,
        **kwargs,
    )


@pytest.fixture()
def history() -> Storage:
    a, b = issued(VERSION_A, TARGET, 42), issued(VERSION_B, TARGET, 49)
    c = issued(VERSION_C, TARGET + timedelta(hours=4), 45)
    storage = Storage([a, b, c])
    for record in (a, b):  # identical re-issued versions, hours 1..2
        storage.add(payload(record, 1, forecast=296.0, observed=295.0))
        storage.add(payload(record, 2, forecast=294.5, observed=295.0))
    # A later, wider acquisition re-verified hour 1 of both versions.
    for record in (a, b):
        storage.add(
            payload(record, 1, forecast=296.0, observed=295.0, raw_artifact="art_raw_2"),
            minutes=46,
        )
    storage.add(payload(c, 1, forecast=290.9, observed=291.0))
    return storage


def test_facts_become_samples_without_reading_any_issued_forecast(history, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("No issued forecast object may be read"))
    monkeypatch.setattr(issuance, "read_issued_forecast", forbidden)
    monkeypatch.setattr(ForecastIssuanceService, "read", forbidden)
    monkeypatch.setattr(ForecastIssuanceService, "select_hours", forbidden)
    monkeypatch.setattr(ForecastIssuanceService, "issue", forbidden)
    monkeypatch.setattr("requests.Session.request", forbidden)
    result = analyze(history)
    forbidden.assert_not_called()
    assert result["schema_version"] == "mesoforge.site-verification-analysis.v1"
    assert result["analysis_policy"]["id"] == "mesoforge-site-verification-analysis.v1"
    assert result["canonicalization_policy"]["id"] == "mesoforge-verification-canonicalization.v1"
    assert result["inventory"] == {
        "issued_versions_for_coordinate": 3,
        "indexed_facts": 7,
        "legacy_unindexed_facts_examined": 0,
        "legacy_unindexed_facts_for_coordinate": 0,
        "legacy_scan": {
            "limit": 200,
            "truncated": False,
            "identity_source": "bounded read of each unindexed immutable fact payload",
        },
        "stored_facts_for_coordinate": 7,
        "analytically_usable_facts": 7,
        "excluded_facts": 0,
        "excluded_by_reason": {},
        "ambiguous_opportunities_or_samples": 0,
    }
    canonical = result["canonicalization"]
    assert canonical["verified_opportunities"] == 5
    assert canonical["opportunities_with_multiple_facts"] == 2
    assert canonical["fact_classification"] == {
        "identical_evidence_reacquired": 2,
        "single_fact": 3,
    }
    assert canonical["sample_classification"] == {
        "identical_reissued_versions": 2,
        "single_version": 1,
    }
    assert result["overall"]["n"] == 3  # seven facts, five opportunities, three samples
    assert result["overall"]["bias_k"] == pytest.approx((1.0 - 0.5 - 0.1) / 3)
    assert result["lead_buckets"]["1-6"]["n"] == 3
    assert result["lead_buckets"]["19-36"]["bias_k"] is None
    assert result["correction_readiness"]["status"] == "insufficient_evidence"
    assert result["reads"] == {
        "issuance_metadata_rows": 3,
        "fact_payloads_read": 7,
        "fact_payload_bytes": sum(len(body) for body in history.payloads.values()),
        "issuance_payloads_read": 0,
        "provider_calls": 0,
        "writes": 0,
        "note": result["reads"]["note"],
    }
    assert len(history.loads) == len(set(history.loads)) == 7  # each fact read exactly once
    assert history.queries == [("issued", LAT, LON, None), ("indexed", LAT, LON), ("legacy", 201)]
    sample = result["samples"][0]
    assert sample["observation"]["observations_artifact_id"] == "obs-for-art_raw_1"
    assert sample["context"]["fields"] == {"wind_speed_10m": 3.4, "cloud_area_fraction": None}
    assert sample["context"]["model_temperatures_k"] == {"HRRR": 296.2, "GFS": 295.6, "IFS": None}
    assert result["regime_readiness"]["dimensions"]["wind_speed"]["available_samples"] == 3


def test_legacy_unindexed_facts_are_recovered_from_payloads_within_a_bound(history, monkeypatch):
    a = history.records[0]
    other = issued(UUID(int=99), TARGET, lat=45.0, lon=-93.0)
    # Same evidence as an indexed fact, saved before attributes existed.
    history.add(payload(a, 2, forecast=294.5, observed=295.0), indexed=False, minutes=-90)
    history.add(
        payload(other, 1, forecast=290.0, observed=289.0, lat=45.0, lon=-93.0), indexed=False
    )
    history.add(b"not json", indexed=False)
    result = analyze(history)
    assert result["inventory"]["indexed_facts"] == 7
    assert result["inventory"]["legacy_unindexed_facts_examined"] == 3
    assert result["inventory"]["legacy_unindexed_facts_for_coordinate"] == 1
    assert result["inventory"]["stored_facts_for_coordinate"] == 8
    assert result["inventory"]["analytically_usable_facts"] == 8
    assert result["canonicalization"]["opportunities_with_multiple_facts"] == 3
    assert result["overall"]["n"] == 3  # the legacy fact adds evidence, not a sample
    hour_two = next(s for s in result["samples"] if s["horizon_hours"] == 2)
    legacy_id = str(history.legacy[0].artifact_id)
    assert hour_two["canonical_fact_id"] == legacy_id  # earliest registered
    assert legacy_id in hour_two["provenance"]["versions"][0]["fact_ids"]

    monkeypatch.setattr(module, "LEGACY_SCAN_LIMIT", 2)
    bounded = analyze(history)
    assert bounded["inventory"]["legacy_scan"] == {
        "limit": 2,
        "truncated": True,
        "identity_source": "bounded read of each unindexed immutable fact payload",
    }
    assert bounded["inventory"]["legacy_unindexed_facts_examined"] == 2


def test_facts_disagreeing_with_issuance_metadata_or_unreadable_are_excluded(history):
    stranger = issued(UUID(int=77), TARGET)
    history.add(payload(stranger, 3, forecast=293.0, observed=292.0))
    wrong_target = payload(history.records[2], 2, forecast=290.0, observed=290.5)
    wrong_target["match"]["forecast_context"]["target_reference_time"] = TARGET.isoformat()
    history.add(wrong_target)
    broken = history.add(b"{truncated")
    result = analyze(history)
    assert result["inventory"]["indexed_facts"] == 10
    assert result["inventory"]["analytically_usable_facts"] == 7
    assert result["inventory"]["excluded_by_reason"] == {
        "issued_forecast_not_found_for_coordinate": 1,
        "payload_unreadable": 1,
        "target_reference_time_disagrees_with_issuance_metadata": 1,
    }
    assert {"artifact_id": str(broken.artifact_id), "reason": "payload_unreadable"} in (
        result["canonicalization"]["excluded_facts"]
    )
    assert result["overall"]["n"] == 3


def test_repeated_analysis_is_identical_apart_from_the_evaluation_block(history):
    first = analyze(history, now=NOW)
    second = analyze(history, now=NOW + timedelta(hours=5))
    assert first["evaluation"]["evaluated_at"] == "2026-09-17T06:00:00Z"
    assert second["evaluation"]["evaluated_at"] == "2026-09-17T11:00:00Z"
    strip = lambda result: {k: v for k, v in result.items() if k != "evaluation"}  # noqa: E731
    assert strip(first) == strip(second)
    json.dumps(first)  # JSON-serializable without special encoders
    requested = analyze(history, display_timezone="UTC")
    assert requested["time_of_day"]["zone"] == {
        "name": "UTC",
        "source": "request",
        "saved_report_zones": ["America/Chicago"],
    }
    assert requested["overall"] == first["overall"]


def test_invalid_requests_never_reach_storage():
    factory = Mock(side_effect=AssertionError("Invalid input reached storage"))
    loader = Mock(side_effect=AssertionError("Invalid input reached object storage"))
    for latitude, longitude in ((95.0, 0.0), (0.0, 181.0), (float("nan"), 0.0)):
        with pytest.raises(ValueError):
            analyze_site_verification(
                latitude, longitude, unit_of_work_factory=factory, load_payload=loader
            )
    with pytest.raises(ValueError):
        analyze_site_verification(
            LAT,
            LON,
            display_timezone="Mars/Olympus",
            unit_of_work_factory=factory,
            load_payload=loader,
        )
    with pytest.raises(ValueError):
        analyze_site_verification(
            LAT, LON, now=datetime(2026, 9, 17), unit_of_work_factory=factory, load_payload=loader
        )
    factory.assert_not_called()
    loader.assert_not_called()


def test_cli_prints_canonical_json_and_reports_errors(monkeypatch, capsys):
    result = {"schema_version": "mesoforge.site-verification-analysis.v1", "overall": {"n": 0}}
    monkeypatch.setattr(module, "analyze_site_verification", Mock(return_value=result))
    assert module.main(["--lat", "44.98859", "--lon", "-93.25557"]) == 0
    assert capsys.readouterr().out == (
        '{"overall":{"n":0},"schema_version":"mesoforge.site-verification-analysis.v1"}\n'
    )
    module.analyze_site_verification.assert_called_once_with(
        44.98859, -93.25557, display_timezone=None
    )
    monkeypatch.setattr(module, "analyze_site_verification", Mock(side_effect=ValueError("zone")))
    assert module.main(["--lat", "45", "--lon", "-93", "--display-timezone", "Nope"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_analysis_request"
    monkeypatch.setattr(module, "analyze_site_verification", Mock(side_effect=RuntimeError("dsn")))
    assert module.main(["--lat", "45", "--lon", "-93"]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "verification_analysis_failed" and "dsn" not in error["message"]
