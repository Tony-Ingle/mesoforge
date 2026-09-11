"""Read one saved hour and preview a temperature proxy from retained artifacts."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from uuid import UUID

from mesoforge.application.issuance import issued_forecast_context, read_issued_forecast
from mesoforge.application.phase2_replay import parse_station_snapshot
from mesoforge.catalog.configuration import ObservationNormalizationPolicy
from mesoforge.catalog.stations import StationDefinition
from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.contracts.observations_v2 import NormalizedObservationV2
from mesoforge.observations.selection import select_temperature_observation
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore


def _retained_inputs(
    artifact_id: ArtifactId,
) -> tuple[
    list[NormalizedObservationV2],
    dict[ArtifactId, tuple[StationDefinition, ...]],
    ObservationNormalizationPolicy,
    dict[str, Any],
]:
    """Use existing manifest/configuration repositories and checksum-verified S3 reads."""
    objects = S3ArtifactObjectStore(
        bucket=os.environ["MESOFORGE_S3_BUCKET"],
        endpoint_url=os.environ["MESOFORGE_S3_ENDPOINT"],
        access_key=os.environ["MESOFORGE_S3_ACCESS_KEY"],
        secret_key=os.environ["MESOFORGE_S3_SECRET_KEY"],
        ensure_bucket=False,
    )
    serializer = CanonicalJsonSerializer()
    with PostgresUnitOfWork(resolve_database_dsn("MESOFORGE_DATABASE_DSN")) as uow:

        def load(identifier: ArtifactId, kind: str, schema: str) -> tuple[ArtifactManifest, bytes]:
            manifest = uow.artifacts.get(identifier)
            if (
                manifest.artifact_type != kind
                or manifest.artifact_schema_version != schema
                or manifest.media_type != "application/json"
                or manifest.quality_state == "invalid"
            ):
                raise IntegrityError("Retained observation input has an unsupported contract")
            payload = objects.get_verified(manifest.storage_uri, manifest.content_digest)
            if len(payload) != manifest.byte_size:
                raise IntegrityError("Retained observation input size disagrees with its manifest")
            return manifest, payload

        manifest, payload = load(
            artifact_id, "normalized-metar-observations", "metar-observations.v2"
        )
        rows = serializer.deserialize(payload)["rows"]
        if not isinstance(rows, list):
            raise IntegrityError("Retained observation rows must be a list")
        observations = [
            NormalizedObservationV2.model_validate_json(serializer.serialize(row)) for row in rows
        ]
        configuration = uow.configurations.get(manifest.configuration_snapshot_id)
        if (
            configuration.configuration_digest != manifest.configuration_digest
            or Digest.of_bytes(serializer.serialize(configuration.canonical_json))
            != manifest.configuration_digest
        ):
            raise IntegrityError("Observation QC configuration checksum mismatch")
        phase2 = configuration.canonical_json["phase2"]
        if not isinstance(phase2, dict):
            raise IntegrityError("Observation QC configuration is missing")
        policy = ObservationNormalizationPolicy.model_validate_json(
            serializer.serialize(phase2["observation_normalization_policy"])
        )
        stations = {}
        station_manifests = []
        for identifier in sorted({row.station_snapshot_artifact_id for row in observations}):
            station_manifest, station_payload = load(
                identifier, "station-catalog-snapshot", "station-catalog-snapshot.v1"
            )
            stations[identifier] = parse_station_snapshot(station_payload).definitions
            station_manifests.append(station_manifest.model_dump(mode="json"))
    return (
        observations,
        stations,
        policy,
        {
            "observations": manifest.model_dump(mode="json"),
            "station_snapshots": station_manifests,
            "observation_qc_policy": policy.model_dump(mode="json"),
        },
    )


def preview_observation_match(issued_forecast_id: UUID, valid_time: datetime) -> dict[str, Any]:
    """Preview one immutable hour; never calculate error, acquire inputs, or write storage."""
    if valid_time.tzinfo is None or valid_time.utcoffset() is None:
        raise ValueError("valid_time must include a timezone")
    saved = read_issued_forecast(issued_forecast_id)
    forecast = saved["forecast"]
    hour = next(
        (h for h in forecast["hours"] if datetime.fromisoformat(h["valid_time"]) == valid_time),
        None,
    )
    if hour is None:
        raise NotFound("No saved forecast hour exists at this valid time")
    result = {
        "issued_forecast_id": saved["issued_forecast_id"],
        "issued_at": saved["issued_at"],
        "forecast": {"latitude": forecast["latitude"], "longitude": forecast["longitude"], **hour},
        "forecast_context": issued_forecast_context(forecast),
        "forecast_code_identity": saved["code_identity"],
        "selection_policy": {
            "max_distance_km": 50,
            "max_time_difference_minutes": 15,
            "boundaries": "inclusive",
            "ranking": ["distance", "absolute_time_difference", "station_id"],
            "revisions": "latest retained revision per logical observation, before QC",
            "notice": (
                "Observation proxy preview only; no forecast error or verification eligibility."
            ),
        },
        "input_provenance": None,
        "status": "unavailable",
        "unavailable_reason": None,
        "selected": None,
        "candidates": [],
    }
    if hour["temperature"]["value"] is None:
        return {**result, "unavailable_reason": "The saved forecast temperature is missing."}
    identifier = os.environ.get("MESOFORGE_OBSERVATIONS_ARTIFACT_ID")
    if not identifier:
        return {**result, "unavailable_reason": "No retained observation dataset is configured."}
    try:
        observations, stations, policy, provenance = _retained_inputs(ArtifactId(identifier))
    except NotFound as exc:
        raise IntegrityError(
            "Configured observation input or its retained metadata is missing"
        ) from exc
    selection = select_temperature_observation(
        latitude=forecast["latitude"],
        longitude=forecast["longitude"],
        valid_time=valid_time,
        observations=observations,
        station_snapshots=stations,
        policy=policy,
    )
    return {**result, "input_provenance": provenance, **selection}
