"""Station discovery cache through the existing artifact service and storage doubles."""

from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mesoforge.application import station_discovery as application
from mesoforge.application.artifacts import ArtifactService
from mesoforge.application.prepared_observations import load_observation_configuration
from mesoforge.application.station_discovery import (
    StationDiscoveryService,
    run_batch,
)
from mesoforge.common.errors import IntegrityError
from mesoforge.common.identifiers import ArtifactId
from mesoforge.observations.acquisition import FetchedResponse
from mesoforge.observations.sources.stationinfo import StationDiscovery
from tests.support.in_memory_uow import (
    InMemoryIdempotencyLock,
    InMemoryObjectStore,
    InMemoryUnitOfWorkFactory,
)


class DiscoveryFactory(InMemoryUnitOfWorkFactory):
    """Only the newly added repository query needs a local test double."""

    def __call__(self):
        uow = super().__call__()

        def find(*, latitude, longitude, policy_version):
            key = dict(latitude=latitude, longitude=longitude, policy_version=policy_version)
            return tuple(
                row
                for row in reversed(list(self.artifacts.values()))
                if row.artifact_type == "aviationweather-stationinfo-response"
                and all((row.attributes or {}).get(name) == value for name, value in key.items())
            )

        uow.artifacts.find_station_discovery_sources = find
        return uow


def discovered(latitude, longitude, *, empty=False):
    candidate = {
        "station_id": "KTST",
        "network": "METAR",
        "lat": latitude,
        "lon": longitude,
        "elevation_m": None,
        "distance_km": 0.0,
        "site_name": "Test station",
        "site_types": ["METAR"],
        "provider_priority": None,
    }
    candidates = () if empty else (candidate,)
    payload = json.dumps(list(candidates)).encode()
    return StationDiscovery(
        bounds=((latitude - 0.5, longitude - 0.5, latitude + 0.5, longitude + 0.5),),
        responses=(
            FetchedResponse(
                url=f"https://aviationweather.gov/api/data/stationinfo?bbox={latitude},{longitude}",
                status_code=200,
                payload=payload,
                headers={"Content-Type": "application/json"},
                completed_at=datetime(2026, 9, 11, tzinfo=UTC),
            ),
        ),
        candidates=candidates,
        excluded=(),
    )


@pytest.fixture()
def case():
    factory, objects, lock = DiscoveryFactory(), InMemoryObjectStore(), InMemoryIdempotencyLock()
    artifacts = ArtifactService(
        unit_of_work_factory=factory, object_store=objects, idempotency_lock=lock
    )
    provider = Mock(side_effect=discovered)
    configuration = load_observation_configuration()

    def create():
        return StationDiscoveryService(
            configuration=configuration,
            artifacts=artifacts,
            unit_of_work_factory=factory,
            idempotency_lock=lock,
            discover=provider,
            code_identity=lambda: {"git_commit": "a" * 40, "test_fixture": True},
        )

    return SimpleNamespace(
        service=create(),
        create=create,
        provider=provider,
        artifacts=artifacts,
        factory=factory,
        objects=objects,
    )


def inventory(case):
    state = case.factory.__dict__ | {
        "_guard": None,
        "configurations": {key: vars(value) for key, value in case.factory.configurations.items()},
    }
    return copy.deepcopy((state, case.objects.objects))


@pytest.mark.parametrize(
    "latitude,longitude",
    [(36.7378, -119.7871), (37.6872, -97.3301), (35.7796, -78.6382), (45.8, -93.1)],
)
def test_new_coordinate_persists_raw_metadata_and_reuses_without_writes(case, latitude, longitude):
    first = case.service.get_station_candidates(latitude, longitude)
    assert first["reused"] is False and first["discovery_calls"] == 1
    assert first["candidates"][0]["elevation_m"] is None
    assert first["radius_km"] == 50 and first["latitude"] == latitude
    assert first["metadata_source"] == "aviationweather.gov/api/data/stationinfo"
    raw = first["raw_provenance"][0]
    raw_manifest, raw_bytes = case.artifacts.load_verified_payload(ArtifactId(raw["artifact_id"]))
    assert raw_bytes == discovered(latitude, longitude).responses[0].payload
    assert raw_manifest.attributes["url"].startswith("https://aviationweather.gov/")
    assert raw_manifest.attributes["acquired_at"] == "2026-09-11T00:00:00+00:00"
    manifest, saved_bytes = case.artifacts.load_verified_payload(
        ArtifactId(first["discovery_artifact_id"])
    )
    saved = json.loads(saved_bytes)
    assert saved["candidates"] == first["candidates"]
    assert "reused" not in saved and str(manifest.content_digest) == first["content_digest"]
    before = inventory(case)
    repeat = case.create().get_station_candidates(latitude, longitude)
    assert repeat["reused"] and repeat["discovery_calls"] == repeat["downloaded_bytes"] == 0
    assert repeat["discovery_artifact_id"] == first["discovery_artifact_id"]
    assert inventory(case) == before
    case.provider.assert_called_once_with(latitude, longitude)


def test_successful_empty_discovery_is_cached_but_provider_failure_is_not(case):
    case.provider.side_effect = [
        RuntimeError("provider unavailable"),
        discovered(45, -93, empty=True),
    ]
    with pytest.raises(RuntimeError, match="provider unavailable"):
        case.service.get_station_candidates(45, -93)
    assert not case.factory.artifacts and not case.objects.objects
    first = case.service.get_station_candidates(45, -93)
    assert first["candidates"] == []
    repeat = case.service.get_station_candidates(45.0, -93.0)
    assert repeat["reused"] and case.provider.call_count == 2


def test_refresh_preserves_previous_snapshot_and_new_coordinate_is_distinct(case):
    first = case.service.get_station_candidates(45, -93)
    refreshed = case.service.get_station_candidates(45, -93, refresh=True)
    other = case.service.get_station_candidates(44, -93)
    assert len({row["discovery_artifact_id"] for row in (first, refreshed, other)}) == 3
    assert (
        case.service.get_station_candidates(45, -93)["discovery_artifact_id"]
        == refreshed["discovery_artifact_id"]
    )
    _, old = case.artifacts.load_verified_payload(ArtifactId(first["discovery_artifact_id"]))
    assert json.loads(old)["discovery_id"] == first["discovery_id"]
    assert case.provider.call_count == 3


@pytest.mark.parametrize("raw", [False, True])
def test_corrupt_retained_payload_fails_closed_without_rediscovery(case, raw):
    first = case.service.get_station_candidates(45, -93)
    digest = first["raw_provenance"][0]["content_digest"] if raw else first["content_digest"]
    case.objects.objects[digest] = b"corrupt"
    with pytest.raises(IntegrityError, match="checksum mismatch"):
        case.service.get_station_candidates(45, -93)
    assert case.provider.call_count == 1


def test_concurrent_calls_recheck_cache_inside_coordinate_lock(case):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: case.service.get_station_candidates(45, -93), range(2))
        )
    assert case.provider.call_count == 1
    assert results[0]["discovery_artifact_id"] == results[1]["discovery_artifact_id"]
    assert sorted(result["reused"] for result in results) == [False, True]


def test_coordinate_only_batch_continues_after_invalid_location(case, tmp_path):
    config = tmp_path / "locations.json"
    config.write_text(
        json.dumps(
            {
                "locations": [
                    {"lat": 36.7378, "lon": -119.7871},
                    {"lat": 95, "lon": -93},
                    {"lat": 35.7796, "lon": -78.6382},
                ]
            }
        ),
        encoding="utf-8",
    )
    result = run_batch(config, service=case.service)
    assert [row["status"] for row in result["results"]] == ["ok", "error", "ok"]
    assert result["failed"] == 1 and case.provider.call_count == 2
    repeat = run_batch(config, service=case.service)
    assert case.provider.call_count == 2
    assert all(row["reused"] for row in repeat["results"] if row["status"] == "ok")


@pytest.mark.parametrize("locations", [[], [{"lat": 95, "lon": -93}]])
def test_no_valid_coordinate_needs_storage_or_provider_configuration(
    monkeypatch, tmp_path, locations
):
    factory = Mock(side_effect=AssertionError("Infrastructure must not be created"))
    monkeypatch.setattr(application, "configured_service", factory)
    config = tmp_path / "locations.json"
    config.write_text(json.dumps({"locations": locations}), encoding="utf-8")
    result = run_batch(config)
    assert result["failed"] == len(locations)
    factory.assert_not_called()
