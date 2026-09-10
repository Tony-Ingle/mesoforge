"""Discover and retain nearby METAR station metadata for coordinates, on demand."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from mesoforge.application.artifacts import (
    ArtifactService,
    SourceRegistrationRequest,
    TransformationInputRef,
    TransformationRequest,
)
from mesoforge.application.batch_forecast import _coordinates, load_locations
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.prepared_temperature import _code_identity
from mesoforge.application.spatial_coverage import validate_coordinate
from mesoforge.catalog.configuration import MesoForgeConfiguration
from mesoforge.common.errors import IntegrityError
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.contracts.artifacts import ArtifactManifest, Availability
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.observations.acquisition import RequestRateLimiter
from mesoforge.observations.sources.stationinfo import (
    BoundedStationInfoTransport,
    StationDiscovery,
    discover_metar_stations,
)
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore

_JSON = CanonicalJsonSerializer()
_SCHEMA = "metar-station-candidates.v1"
_ACTIVITY = "discover-metar-stations"
POLICY_VERSION = "awc-stationinfo-wgs84-50km.v1"
_RADIUS_KM = 50.0
_ROOT = Path(__file__).resolve().parents[3]


def _identity() -> dict[str, Any]:
    result = _code_identity()
    for name in (
        "application/station_discovery.py",
        "observations/sources/stationinfo.py",
        "observations/acquisition.py",
    ):
        result["source_sha256"][name] = hashlib.sha256(
            (_ROOT / "src/mesoforge" / name).read_bytes()
        ).hexdigest()
    result["git_commit"] = subprocess.run(  # noqa: S603
        ["git", "-C", str(_ROOT), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return result


class StationDiscoveryService:
    """Coordinate cache using immutable artifacts and their existing provenance edges."""

    def __init__(
        self,
        *,
        configuration: MesoForgeConfiguration,
        artifacts: ArtifactService,
        unit_of_work_factory: Callable[[], Any],
        idempotency_lock: Any,
        discover: Callable[[float, float], StationDiscovery],
        code_identity: Callable[[], dict[str, Any]] = _identity,
    ) -> None:
        self._configuration = configuration
        self._artifacts = artifacts
        self._factory = unit_of_work_factory
        self._lock = idempotency_lock
        self._discover = discover
        self._identity = code_identity

    def _retained(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        with self._factory() as uow:
            sources = uow.artifacts.find_station_discovery_sources(
                latitude=latitude, longitude=longitude, policy_version=POLICY_VERSION
            )
            for source in sources:
                if source.quality_state == "invalid":
                    continue
                for activity in uow.activities.consumers_of(source.artifact_id):
                    if activity.activity_type != _ACTIVITY or activity.status != "succeeded":
                        continue
                    for output in activity.outputs:
                        manifest, payload = self._artifacts.load_verified_payload(
                            output.artifact_id
                        )
                        if (
                            manifest.artifact_schema_version != _SCHEMA
                            or manifest.quality_state == "invalid"
                        ):
                            continue
                        saved: dict[str, Any] = _JSON.deserialize(payload)
                        if (
                            saved["latitude"] != latitude
                            or saved["longitude"] != longitude
                            or saved["policy_version"] != POLICY_VERSION
                            or saved["schema_version"] != _SCHEMA
                            or saved["radius_km"] != _RADIUS_KM
                        ):
                            raise IntegrityError("Retained station discovery identity mismatch")
                        raw_ids = {row["artifact_id"] for row in saved["raw_provenance"]}
                        if raw_ids != {str(ref.artifact_id) for ref in activity.inputs}:
                            raise IntegrityError("Retained station discovery lineage mismatch")
                        for row in saved["raw_provenance"]:
                            raw, data = self._artifacts.load_verified_payload(
                                ArtifactId(row["artifact_id"])
                            )
                            if (
                                str(raw.content_digest) != row["content_digest"]
                                or len(data) != row["byte_size"]
                                or raw.quality_state == "invalid"
                                or raw.attributes != row["source_metadata"]
                            ):
                                raise IntegrityError(
                                    "Retained station discovery provenance mismatch"
                                )
                        return self._result(manifest, saved, reused=True)
        return None

    @staticmethod
    def _result(
        manifest: ArtifactManifest, saved: dict[str, Any], *, reused: bool
    ) -> dict[str, Any]:
        return {
            **saved,
            "discovery_artifact_id": str(manifest.artifact_id),
            "content_digest": str(manifest.content_digest),
            "reused": reused,
            "discovery_calls": 0 if reused else len(saved["raw_provenance"]),
            "downloaded_bytes": 0
            if reused
            else sum(row["byte_size"] for row in saved["raw_provenance"]),
        }

    def get_station_candidates(
        self, latitude: float, longitude: float, *, refresh: bool = False
    ) -> dict[str, Any]:
        validate_coordinate(latitude, longitude)
        # Normalize numeric spellings so 45 and 45.0 share a lock as well as a cache key.
        latitude, longitude = float(latitude), float(longitude)
        key = {"latitude": latitude, "longitude": longitude, "policy_version": POLICY_VERSION}
        with self._lock.acquire(Digest.of_bytes(_JSON.serialize(key))):
            if not refresh:
                retained = self._retained(latitude, longitude)
                if retained is not None:
                    return retained
            discovered = self._discover(latitude, longitude)
            if not discovered.responses:
                raise ValueError("Station discovery requires retained provider response evidence")
            # A failed provider request never becomes a cached empty candidate list.
            acquired = max(response.completed_at for response in discovered.responses)
            snapshot = ConfigurationService(self._factory).register(self._configuration)
            identity = self._identity()
            environment = Digest.of_bytes(_JSON.serialize(identity))
            discovery_id = str(uuid4())
            inputs, provenance = [], []
            for index, response in enumerate(discovered.responses):
                metadata = {
                    **key,
                    "discovery_id": discovery_id,
                    "response_index": index,
                    "acquired_at": response.completed_at.isoformat(),
                    "url": response.url,
                    "status_code": response.status_code,
                    "headers": response.headers,
                }
                raw = self._artifacts.register_source(
                    SourceRegistrationRequest(
                        source_authority="aviationweather.gov",
                        source_locator=response.url,
                        source_revision=f"{discovery_id}:{index}",
                        artifact_type="aviationweather-stationinfo-response",
                        artifact_schema_version="aviationweather-stationinfo-response.v1",
                        media_type="application/json",
                        expected_content_digest=Digest.of_bytes(response.payload),
                        created_at=response.completed_at,
                        availability=Availability(
                            available_at=response.completed_at,
                            ingested_at=response.completed_at,
                            authority="mesoforge",
                            method="local-acquisition-complete",
                        ),
                        configuration_snapshot_id=snapshot.configuration_snapshot_id,
                        configuration_digest=snapshot.configuration_digest,
                        code_revision=identity["git_commit"],
                        environment_digest=environment,
                        attributes=metadata,
                    ),
                    response.payload,
                )
                inputs.append(
                    TransformationInputRef(role=f"raw-{index}", artifact_id=raw.artifact_id)
                )
                provenance.append(
                    {
                        "artifact_id": str(raw.artifact_id),
                        "content_digest": str(raw.content_digest),
                        "byte_size": len(response.payload),
                        "source_metadata": metadata,
                    }
                )
            saved = {
                "schema_version": _SCHEMA,
                **key,
                "discovery_id": discovery_id,
                "radius_km": _RADIUS_KM,
                "acquired_at": acquired.isoformat(),
                "metadata_source": "aviationweather.gov/api/data/stationinfo",
                "query_bounds": list(discovered.bounds),
                "candidates": list(discovered.candidates),
                "excluded": list(discovered.excluded),
                "raw_provenance": provenance,
                "code_identity": identity,
            }

            def transform(*payloads: bytes) -> dict[str, Any]:
                if any(
                    str(Digest.of_bytes(data)) != row["content_digest"]
                    for data, row in zip(payloads, provenance, strict=True)
                ):
                    raise IntegrityError("Station discovery raw checksum mismatch")
                return saved

            def validate(value: dict[str, Any]) -> None:
                if _JSON.serialize(value) != _JSON.serialize(saved):
                    raise IntegrityError("Station discovery output mismatch")

            result = self._artifacts.execute_raw_transformation(
                TransformationRequest(
                    activity_type=_ACTIVITY,
                    activity_version="v1",
                    inputs=tuple(inputs),
                    output_role="station-candidates",
                    output_artifact_type="metar-station-candidates",
                    output_artifact_schema_version=_SCHEMA,
                    output_media_type="application/json",
                    parameters={"discovery": saved},
                    configuration_snapshot_id=snapshot.configuration_snapshot_id,
                    configuration_digest=snapshot.configuration_digest,
                    code_revision=identity["git_commit"],
                    environment_digest=environment,
                ),
                transform,
                _JSON,
                input_loader=bytes,
                output_validator=validate,
            )
            _, payload = self._artifacts.load_verified_payload(result.output.artifact_id)
            return self._result(result.output, _JSON.deserialize(payload), reused=False)


def configured_service() -> StationDiscoveryService:
    from mesoforge.application.prepared_observations import load_observation_configuration

    configuration = load_observation_configuration()
    assert configuration.phase2 is not None
    settings = configuration.phase2.aviationweather
    limiter = RequestRateLimiter(settings.min_request_interval_seconds)
    dsn = resolve_database_dsn("MESOFORGE_DATABASE_DSN")
    factory = cast(Any, lambda: PostgresUnitOfWork(dsn))
    lock = PostgresIdempotencyLock(dsn)
    objects = S3ArtifactObjectStore(
        bucket=os.environ["MESOFORGE_S3_BUCKET"],
        endpoint_url=os.environ["MESOFORGE_S3_ENDPOINT"],
        access_key=os.environ["MESOFORGE_S3_ACCESS_KEY"],
        secret_key=os.environ["MESOFORGE_S3_SECRET_KEY"],
        ensure_bucket=False,
    )

    def discover(latitude: float, longitude: float) -> StationDiscovery:
        transport = BoundedStationInfoTransport()
        try:
            return discover_metar_stations(
                settings,
                latitude=latitude,
                longitude=longitude,
                transport=cast(Any, transport),
                clock=SystemClock(),
                sleeper=SystemSleeper(),
                rate_limiter=limiter,
            )
        finally:
            transport.close()

    return StationDiscoveryService(
        configuration=configuration,
        artifacts=ArtifactService(
            unit_of_work_factory=factory,
            object_store=cast(Any, objects),
            idempotency_lock=lock,
        ),
        unit_of_work_factory=factory,
        idempotency_lock=lock,
        discover=discover,
    )


def get_station_candidates(latitude: float, longitude: float) -> dict[str, Any]:
    validate_coordinate(latitude, longitude)
    return configured_service().get_station_candidates(latitude, longitude)


def run_batch(
    config_path: Path, *, service: StationDiscoveryService | None = None
) -> dict[str, Any]:
    locations = load_locations(config_path)
    results = []
    for index, item in enumerate(locations):
        try:
            latitude, longitude = _coordinates(item)
            validate_coordinate(latitude, longitude)
            service = service or configured_service()
            result = service.get_station_candidates(latitude, longitude)
            results.append({"index": index, "status": "ok", **result})
        except Exception as exc:
            results.append({"index": index, "status": "error", "location": item, "error": str(exc)})
    return {"results": results, "failed": sum(row["status"] == "error" for row in results)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    args = parser.parse_args(argv)
    if (args.config is not None and (args.lat is not None or args.lon is not None)) or (
        args.config is None and (args.lat is None or args.lon is None)
    ):
        parser.error("Supply --config, or both --lat and --lon")
    try:
        result = (
            run_batch(args.config)
            if args.config is not None
            else get_station_candidates(args.lat, args.lon)
        )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
