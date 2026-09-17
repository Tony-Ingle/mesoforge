"""Metadata-only accumulation counts; storage doubles record exactly which reads happen."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from mesoforge.application import accumulation_status as module
from mesoforge.application.accumulation_status import HOUR_STATES, accumulation_status
from mesoforge.application.station_discovery import POLICY_VERSION
from mesoforge.common.identifiers import ArtifactId, ConfigurationSnapshotId, Digest
from mesoforge.contracts.artifacts import ArtifactManifest, Availability
from mesoforge.contracts.issued_forecasts import IssuedForecastRecord

LAT, LON = 44.98859, -93.25557
TARGET = datetime(2026, 9, 16, 22, tzinfo=UTC)
CONFIGURATION_DIGEST = Digest.of_bytes(b"observation-configuration")


def record(target: datetime, *, issued_at: datetime | None = None) -> IssuedForecastRecord:
    return IssuedForecastRecord(
        issued_forecast_id=uuid4(),
        batch_run_id=uuid4(),
        location_index=0,
        latitude=LAT,
        longitude=LON,
        issued_at=issued_at or target + timedelta(minutes=40),
        target_reference_time=target,
        content_digest=Digest.of_bytes(target.isoformat().encode()),
    )


def manifest(
    artifact_type: str,
    schema: str,
    attributes: dict | None,
    *,
    registered_at: datetime = TARGET,
    quality_state: str = "valid",
) -> ArtifactManifest:
    return ArtifactManifest(
        artifact_id=ArtifactId.generate(),
        artifact_type=artifact_type,
        artifact_schema_version=schema,
        media_type="application/json",
        byte_size=2,
        content_digest=Digest.of_bytes(b"{}"),
        storage_uri="s3://bucket/objects/x",
        created_at=registered_at,
        registered_at=registered_at,
        availability=Availability(
            available_at=registered_at, authority="mesoforge.derived", method="test"
        ),
        configuration_snapshot_id=ConfigurationSnapshotId.from_digest(CONFIGURATION_DIGEST),
        configuration_digest=CONFIGURATION_DIGEST,
        code_revision="a" * 40,
        environment_digest=Digest.of_bytes(b"environment"),
        quality_state=quality_state,
        attributes=attributes,
    )


def fact(
    issued: IssuedForecastRecord | str,
    horizon: int,
    *,
    registered_at: datetime = TARGET,
    quality_state: str = "valid",
    status: str = "verified",
) -> ArtifactManifest:
    identifier = issued if isinstance(issued, str) else str(issued.issued_forecast_id)
    valid = TARGET + timedelta(hours=horizon)
    return manifest(
        "issued-temperature-verification",
        "issued-temperature-verification.v1",
        {
            "issued_forecast_id": identifier,
            "valid_time": valid.isoformat(),
            "horizon_hours": horizon,
            "latitude": LAT,
            "longitude": LON,
            "verification_status": status,
        },
        registered_at=registered_at,
        quality_state=quality_state,
    )


def metar_source(
    start: datetime,
    end: datetime,
    *,
    station_ids: list[str] | None = None,
    quality_state: str = "valid",
    registered_at: datetime = TARGET,
) -> ArtifactManifest:
    return manifest(
        "aviationweather-metar-response",
        "aviationweather-metar-response.v1",
        {
            "data_kind": "real_metar_observations",
            "latitude": LAT,
            "longitude": LON,
            "query_window_start": (start - timedelta(minutes=15)).isoformat(),
            "query_window_end": (end + timedelta(minutes=15)).isoformat(),
            "station_ids": station_ids or ["KMSP", "KSTP"],
            "station_discovery_artifact_id": "art_discovery",
            "station_discovery": {"candidates": [{"icao": "KMSP"}, {"icao": "KSTP"}]},
            "station_metadata_exclusions": [{"icao": "KZZZ"}],
            "acquired_at": (end + timedelta(minutes=20)).isoformat(),
        },
        quality_state=quality_state,
        registered_at=registered_at,
    )


def discovery_source() -> ArtifactManifest:
    return manifest(
        "aviationweather-stationinfo-response",
        "aviationweather-stationinfo-response.v1",
        {
            "latitude": LAT,
            "longitude": LON,
            "policy_version": POLICY_VERSION,
            "discovery_id": "discovery-1",
            "acquired_at": TARGET.isoformat(),
        },
    )


class FakeUnitOfWork:
    def __init__(self, records=(), facts=(), sources=(), discoveries=()):
        self.calls = []
        self.commits = 0
        self.issued_forecasts = SimpleNamespace(list_for_coordinate=self._records)
        self.artifacts = SimpleNamespace(
            find_issued_temperature_verifications=self._facts,
            find_real_metar_sources=self._sources,
            find_station_discovery_sources=self._discoveries,
        )
        self._data = (tuple(records), tuple(facts), tuple(sources), tuple(discoveries))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def commit(self):
        raise AssertionError("Status reads never commit")

    def _records(self, latitude, longitude, *, limit):
        self.calls.append(("issued", latitude, longitude, limit))
        return self._data[0]

    def _facts(self, *, latitude, longitude):
        self.calls.append(("facts", latitude, longitude))
        return self._data[1]

    def _sources(self, *, latitude, longitude, configuration_digest):
        self.calls.append(("sources", latitude, longitude, str(configuration_digest)))
        return self._data[2]

    def _discoveries(self, *, latitude, longitude, policy_version):
        self.calls.append(("discoveries", latitude, longitude, policy_version))
        return self._data[3]


def status(uow, *, now):
    return accumulation_status(LAT, LON, now=now, unit_of_work_factory=lambda: uow)


def test_no_history_gives_zero_counts_and_reads_only_metadata(monkeypatch):
    monkeypatch.setattr(module, "compute_configuration_digest", lambda c: CONFIGURATION_DIGEST)
    uow = FakeUnitOfWork()
    result = status(uow, now=TARGET)
    assert result["schema_version"] == "mesoforge.accumulation-status.v1"
    assert result["coordinate"] == {"latitude": LAT, "longitude": LON}
    assert result["evaluated_at"] == "2026-09-16T22:00:00Z"
    assert result["issuances"] == {
        "count": 0,
        "earliest_issued_at": None,
        "latest_issued_at": None,
        "earliest_target_reference_time": None,
        "latest_target_reference_time": None,
        "distinct_target_reference_times": 0,
        "targets_with_multiple_versions": 0,
    }
    assert result["hours"] == {"total": 0, "eligible": 0, **dict.fromkeys(HOUR_STATES, 0)}
    assert result["verified_by_lead_bucket"] == {"1-6": 0, "7-18": 0, "19-36": 0}
    assert result["verification_facts"]["count"] == 0
    assert result["station_evidence"]["source"] is None
    assert result["per_issuance"] == []
    assert uow.calls == [
        ("issued", LAT, LON, None),
        ("facts", LAT, LON),
        ("sources", LAT, LON, str(CONFIGURATION_DIGEST)),
        ("discoveries", LAT, LON, POLICY_VERSION),
    ]
    json.dumps(result)  # JSON-serializable without special encoders


def test_hours_move_from_pending_to_eligible_to_verified(monkeypatch):
    monkeypatch.setattr(module, "compute_configuration_digest", lambda c: CONFIGURATION_DIGEST)
    issued = record(TARGET)

    # Just after issuance: nothing can be matched yet.
    early = status(FakeUnitOfWork([issued]), now=TARGET + timedelta(minutes=45))
    assert early["hours"] == {
        "total": 36,
        "eligible": 0,
        "pending": 36,
        "verified": 0,
        "no_retained_observations": 0,
        "retained_observations_without_fact": 0,
    }

    # Hour 2's window (valid + 15 min) closes exactly at 00:15Z; hour 3's has not.
    now = TARGET + timedelta(hours=2, minutes=15)
    eligible = status(FakeUnitOfWork([issued]), now=now)
    assert eligible["hours"]["pending"] == 34
    assert eligible["hours"]["eligible"] == 2
    assert eligible["hours"]["no_retained_observations"] == 2
    assert eligible["hours"]["verified"] == 0

    # A retained acquisition covering hour 1 distinguishes "attempted" from "never retained".
    source = metar_source(TARGET + timedelta(hours=1), TARGET + timedelta(hours=1))
    covered = status(FakeUnitOfWork([issued], sources=[source]), now=now)
    assert covered["hours"]["retained_observations_without_fact"] == 1
    assert covered["hours"]["no_retained_observations"] == 1
    assert covered["observation_coverage"]["retained_acquisitions"] == 1
    assert covered["observation_coverage"]["earliest_query_window_start"] == "2026-09-16T22:45:00Z"

    # A saved fact for hour 1 makes it verified; hour 2 stays eligible without observations.
    verified = status(FakeUnitOfWork([issued], facts=[fact(issued, 1)], sources=[source]), now=now)
    assert verified["hours"] == {
        "total": 36,
        "eligible": 2,
        "verified": 1,
        "pending": 34,
        "no_retained_observations": 1,
        "retained_observations_without_fact": 0,
    }
    assert verified["verified_by_lead_bucket"] == {"1-6": 1, "7-18": 0, "19-36": 0}
    assert verified["verification_facts"]["count"] == 1
    assert verified["verification_facts"]["hours_with_multiple_facts"] == 0
    assert verified["per_issuance"] == [
        {
            "issued_forecast_id": str(issued.issued_forecast_id),
            "issued_at": "2026-09-16T22:40:00Z",
            "target_reference_time": "2026-09-16T22:00:00Z",
            "hours": {
                "verified": 1,
                "pending": 34,
                "no_retained_observations": 1,
                "retained_observations_without_fact": 0,
            },
        }
    ]


def test_lead_buckets_and_multiple_versions_for_one_target(monkeypatch):
    monkeypatch.setattr(module, "compute_configuration_digest", lambda c: CONFIGURATION_DIGEST)
    first = record(TARGET, issued_at=TARGET + timedelta(minutes=30))
    second = record(TARGET, issued_at=TARGET + timedelta(minutes=50))
    later = record(TARGET + timedelta(hours=1), issued_at=TARGET + timedelta(hours=1, minutes=30))
    facts = [fact(first, horizon) for horizon in (1, 6, 7, 18, 19, 36)] + [fact(second, 6)]
    result = status(
        FakeUnitOfWork([later, second, first], facts=facts), now=TARGET + timedelta(days=3)
    )
    assert result["issuances"]["count"] == 3
    assert result["issuances"]["earliest_issued_at"] == "2026-09-16T22:30:00Z"
    assert result["issuances"]["latest_issued_at"] == "2026-09-16T23:30:00Z"
    assert result["issuances"]["earliest_target_reference_time"] == "2026-09-16T22:00:00Z"
    assert result["issuances"]["latest_target_reference_time"] == "2026-09-16T23:00:00Z"
    assert result["issuances"]["distinct_target_reference_times"] == 2
    assert result["issuances"]["targets_with_multiple_versions"] == 1
    assert result["hours"]["total"] == 108
    assert result["hours"]["pending"] == 0
    assert result["hours"]["verified"] == 7
    assert result["hours"]["no_retained_observations"] == 101
    assert result["verified_by_lead_bucket"] == {"1-6": 3, "7-18": 2, "19-36": 2}
    assert [row["issued_forecast_id"] for row in result["per_issuance"]] == [
        str(first.issued_forecast_id),
        str(second.issued_forecast_id),
        str(later.issued_forecast_id),
    ]
    assert [row["hours"]["verified"] for row in result["per_issuance"]] == [6, 1, 0]


def test_replayed_and_unmatched_facts_never_inflate_verified_hours(monkeypatch):
    monkeypatch.setattr(module, "compute_configuration_digest", lambda c: CONFIGURATION_DIGEST)
    issued = record(TARGET)
    facts = [
        fact(issued, 3, registered_at=TARGET + timedelta(hours=4)),
        # Same hour under a different code identity: a legitimate second fact, one hour.
        fact(issued, 3, registered_at=TARGET + timedelta(hours=9)),
        fact(str(UUID(int=99)), 3),  # Belongs to a version this coordinate never issued.
        fact(issued, 4, quality_state="invalid"),
        fact(issued, 5, status="unavailable"),
        manifest("issued-temperature-verification", "issued-temperature-verification.v1", None),
    ]
    result = status(FakeUnitOfWork([issued], facts=facts), now=TARGET + timedelta(days=2))
    assert result["hours"]["verified"] == 1
    assert result["verification_facts"] == {
        "count": 3,
        "earliest_registered_at": "2026-09-16T22:00:00Z",
        "latest_registered_at": "2026-09-17T07:00:00Z",
        "hours_with_multiple_facts": 1,
        "facts_without_an_enumerated_hour": 1,
        "facts_without_usable_attributes": 3,
    }


def test_station_evidence_prefers_retained_acquisition_then_discovery(monkeypatch):
    monkeypatch.setattr(module, "compute_configuration_digest", lambda c: CONFIGURATION_DIGEST)
    older = metar_source(TARGET, TARGET, station_ids=["KOLD"], registered_at=TARGET)
    newest = metar_source(
        TARGET + timedelta(hours=2),
        TARGET + timedelta(hours=2),
        station_ids=["KMSP"],
        registered_at=TARGET + timedelta(hours=3),
    )
    invalid = metar_source(
        TARGET + timedelta(hours=5),
        TARGET + timedelta(hours=5),
        station_ids=["KBAD"],
        quality_state="invalid",
        registered_at=TARGET + timedelta(hours=6),
    )
    with_sources = status(
        FakeUnitOfWork(sources=[older, newest, invalid], discoveries=[discovery_source()]),
        now=TARGET,
    )
    assert with_sources["station_evidence"] == {
        "source": "retained_metar_acquisition",
        "artifact_id": str(newest.artifact_id),
        "acquired_at": "2026-09-17T00:20:00+00:00",
        "station_ids": ["KMSP"],
        "station_discovery_artifact_id": "art_discovery",
        "discovery_candidates": 2,
        "metadata_exclusions": 1,
    }
    assert with_sources["observation_coverage"]["retained_acquisitions"] == 2

    discovery = discovery_source()
    only_discovery = status(FakeUnitOfWork(discoveries=[discovery]), now=TARGET)
    assert only_discovery["station_evidence"]["source"] == "station_discovery_response"
    assert only_discovery["station_evidence"]["artifact_id"] == str(discovery.artifact_id)
    assert only_discovery["station_evidence"]["discovery_id"] == "discovery-1"


def test_invalid_coordinates_and_naive_times_are_rejected_before_storage():
    factory = Mock(side_effect=AssertionError("Invalid input reached storage"))
    for latitude, longitude in ((95.0, 0.0), (0.0, 181.0), (float("nan"), 0.0), (True, 0.0)):
        with pytest.raises(ValueError):
            accumulation_status(latitude, longitude, unit_of_work_factory=factory)
    with pytest.raises(ValueError):
        accumulation_status(LAT, LON, now=datetime(2026, 9, 16), unit_of_work_factory=factory)
    factory.assert_not_called()


def test_cli_prints_canonical_json_and_reports_errors(monkeypatch, capsys):
    payload = {"schema_version": "mesoforge.accumulation-status.v1", "hours": {"total": 0}}
    monkeypatch.setattr(module, "accumulation_status", Mock(return_value=payload))
    assert module.main(["--lat", "44.98859", "--lon", "-93.25557"]) == 0
    out = capsys.readouterr().out
    assert out == '{"hours":{"total":0},"schema_version":"mesoforge.accumulation-status.v1"}\n'
    module.accumulation_status.assert_called_once_with(44.98859, -93.25557)

    monkeypatch.setattr(module, "accumulation_status", Mock(side_effect=ValueError("bad")))
    assert module.main(["--lat", "95", "--lon", "0"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_coordinate"

    monkeypatch.setattr(module, "accumulation_status", Mock(side_effect=RuntimeError("dsn")))
    assert module.main(["--lat", "45", "--lon", "-93"]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "accumulation_status_failed" and "dsn" not in error["message"]
