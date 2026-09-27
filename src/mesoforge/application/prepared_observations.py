"""Prepare one bounded historical METAR dataset for the existing verification command."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from mesoforge.application.artifacts import (
    ArtifactService,
    SourceRegistrationRequest,
    TransformationInputRef,
    TransformationRequest,
)
from mesoforge.application.code_revision import current_code_revision
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.issuance import validate_hour_selection
from mesoforge.application.phase2_replay import parse_station_snapshot
from mesoforge.application.prepared_temperature import _code_identity
from mesoforge.application.station_discovery import get_station_candidates
from mesoforge.catalog.configuration import MesoForgeConfiguration, load_configuration_source
from mesoforge.catalog.stations import StationDefinition, StationRecord
from mesoforge.common.identifiers import ArtifactId, Digest, StationId
from mesoforge.contracts.artifacts import Availability
from mesoforge.contracts.observations_v2 import NormalizedObservationV2, RawMetarRecordV2
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.observations.acquisition import acquire_metar_batch
from mesoforge.observations.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.observations.normalization_v2 import normalize_metar_record_v2
from mesoforge.observations.sources.aviationweather import (
    RequestsAviationWeatherHttpTransport,
    parse_raw_metar_response,
)
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore

_ROOT = Path(__file__).resolve().parents[3]
_JSON = CanonicalJsonSerializer()


def _identity() -> dict[str, Any]:
    identity = _code_identity()
    for name in (
        "application/prepared_observations.py",
        "application/station_discovery.py",
        "application/phase2_replay.py",
        "catalog/stations.py",
        "observations/sources/stationinfo.py",
        "observations/acquisition.py",
        "observations/sources/aviationweather.py",
        "observations/normalization_v2.py",
        "observations/normalization.py",
        "observations/quality.py",
        "contracts/observations_v2.py",
        "contracts/observations.py",
    ):
        identity["source_sha256"][name] = hashlib.sha256(
            (_ROOT / "src/mesoforge" / name).read_bytes()
        ).hexdigest()
    identity["git_commit"] = current_code_revision(_ROOT)
    return identity


def load_observation_configuration() -> MesoForgeConfiguration:
    configuration, _ = load_configuration_source(
        base_path=_ROOT / "configs/base.yaml",
        environment_path=_ROOT / "configs/phase1-grasston.yaml",
        additional_overlay_paths=(_ROOT / "configs/phase2-grasston.yaml",),
    )
    return configuration


def discovery_definitions(discovery: dict[str, Any]) -> tuple[StationDefinition, ...]:
    """Adapt discovered metadata to the existing QC contract without inventing elevation.

    Candidates with unavailable elevation remain saved in discovery; they cannot
    satisfy the existing metadata-tolerance QC and are explicitly excluded below.
    """
    definitions = []
    for candidate in sorted(discovery["candidates"], key=lambda row: row["station_id"]):
        if candidate["elevation_m"] is None:
            continue
        definitions.append(
            StationDefinition(
                station_id=StationId("station." + candidate["station_id"].lower()),
                provider_icao_id=candidate["station_id"],
                expected_latitude=float(candidate["lat"]),
                expected_longitude=float(candidate["lon"]),
                expected_elevation_m=float(candidate["elevation_m"]),
                site_name=candidate["site_name"] or candidate["station_id"],
                provider_site_types=tuple(candidate["site_types"]),
                # This legacy field is unused by temperature matching (distance/time/ID).
                provider_priority=0,
            )
        )
    return tuple(definitions)


def discovery_exclusions(discovery: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "station_id": candidate["station_id"],
            "reason": "Station elevation unavailable for existing metadata-tolerance QC.",
        }
        for candidate in discovery["candidates"]
        if candidate["elevation_m"] is None
    ]


def _bundle_definitions(
    metadata: dict[str, Any],
    configuration: MesoForgeConfiguration,
) -> tuple[StationDefinition, ...]:
    if "station_discovery" in metadata:
        return discovery_definitions(metadata["station_discovery"])
    # Read retained pre-discovery bundles exactly as originally prepared.
    assert configuration.phase2 is not None
    return tuple(
        s for s in configuration.phase2.stations if s.provider_icao_id in metadata["station_ids"]
    )


def acquire_bundle(
    raw_dir: Path,
    *,
    latitude: float,
    longitude: float,
    start_valid_time: datetime,
    end_valid_time: datetime,
    transport: HttpTransport | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    station_discovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _acquire_bundle(
        raw_dir,
        latitude=latitude,
        longitude=longitude,
        start_valid_time=start_valid_time,
        end_valid_time=end_valid_time,
        transport=transport,
        clock=clock,
        sleeper=sleeper,
        station_discovery=station_discovery,
    )


def acquire_for_valid_times(
    raw_dir: Path,
    *,
    latitude: float,
    longitude: float,
    valid_times: tuple[datetime, ...],
    station_discovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pad the actual first/last eligible valid times, including a single valid hour."""
    if not valid_times:
        raise ValueError("No valid times require observation acquisition")
    return _acquire_bundle(
        raw_dir,
        latitude=latitude,
        longitude=longitude,
        start_valid_time=min(valid_times),
        end_valid_time=max(valid_times),
        single_hour=True,
        station_discovery=station_discovery,
    )


def _acquire_bundle(
    raw_dir: Path,
    *,
    latitude: float,
    longitude: float,
    start_valid_time: datetime,
    end_valid_time: datetime,
    transport: HttpTransport | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    single_hour: bool = False,
    station_discovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch once through the retained adapter; save bytes before parsing any records."""
    if single_hour and start_valid_time == end_valid_time:
        validate_hour_selection(
            latitude,
            longitude,
            start_valid_time - timedelta(minutes=15),
            end_valid_time + timedelta(minutes=15),
        )
    else:
        validate_hour_selection(latitude, longitude, start_valid_time, end_valid_time)
    if end_valid_time - start_valid_time > timedelta(hours=6):
        raise ValueError("This bounded preparation command accepts at most six hours per dataset")
    clock = clock or SystemClock()
    start = start_valid_time.astimezone(UTC) - timedelta(minutes=15)
    end = end_valid_time.astimezone(UTC) + timedelta(minutes=15)
    if end > clock.now():
        raise ValueError("The entire window, including the 15-minute margin, must be in the past")
    if raw_dir.resolve().is_relative_to(_ROOT):
        raise ValueError("Raw observation data must be retained outside the repository")
    if raw_dir.exists():
        raise FileExistsError(
            "Raw observation directory already exists; retained files are preserved"
        )
    configuration = load_observation_configuration()
    phase2 = configuration.phase2
    assert phase2 is not None
    discovery = station_discovery or get_station_candidates(latitude, longitude)
    if discovery["latitude"] != latitude or discovery["longitude"] != longitude:
        raise ValueError("Saved station discovery belongs to a different forecast coordinate")
    definitions = discovery_definitions(discovery)
    station_ids = tuple(station.provider_icao_id for station in definitions)
    if not station_ids:
        raise ValueError("No discovered METAR candidates can satisfy the existing metadata QC")
    # Copy only the query duration; the retained Phase 2 defaults stay unchanged.
    settings = phase2.aviationweather.model_copy(
        update={"metar_window_hours": (end - start).total_seconds() / 3600}
    )
    raw_dir.mkdir(parents=True, exist_ok=False)
    config_bytes = _JSON.serialize(configuration.model_dump(mode="json"))
    (raw_dir / "configuration.json").write_bytes(config_bytes)
    identity = _identity()
    fetched = acquire_metar_batch(
        settings,
        transport=transport or cast(HttpTransport, RequestsAviationWeatherHttpTransport()),
        clock=clock,
        sleeper=sleeper or SystemSleeper(),
        station_ids=station_ids,
        query_date=end,
    )
    (raw_dir / "metar.json").write_bytes(fetched.payload)
    metadata = {
        "data_kind": "real_metar_observations",
        "latitude": latitude,
        "longitude": longitude,
        "start_valid_time": start_valid_time.astimezone(UTC).isoformat(),
        "end_valid_time": end_valid_time.astimezone(UTC).isoformat(),
        "query_window_start": start.isoformat(),
        "query_window_end": end.isoformat(),
        "station_ids": list(station_ids),
        "station_metadata_source": discovery["metadata_source"],
        "station_discovery_artifact_id": discovery["discovery_artifact_id"],
        "station_discovery": {
            key: value
            for key, value in discovery.items()
            if key not in {"reused", "discovery_calls", "downloaded_bytes"}
        },
        "station_metadata_exclusions": discovery_exclusions(discovery),
        "url": fetched.url,
        "status_code": fetched.status_code,
        "headers": fetched.headers,
        "acquired_at": fetched.completed_at.isoformat(),
        "raw_bytes": len(fetched.payload),
        "raw_digest": str(Digest.of_bytes(fetched.payload)),
        "configuration_digest": str(Digest.of_bytes(config_bytes)),
        "acquisition_code_identity": identity,
        "empty_response_note": "The existing adapter represents HTTP 204 as canonical [].",
    }
    (raw_dir / "manifest.json").write_bytes(_JSON.serialize(metadata))
    return metadata


def load_bundle(raw_dir: Path) -> tuple[dict[str, Any], MesoForgeConfiguration, bytes]:
    """Checksum-check retained inputs without constructing an HTTP transport."""
    metadata = json.loads((raw_dir / "manifest.json").read_bytes())
    payload = (raw_dir / "metar.json").read_bytes()
    config_bytes = (raw_dir / "configuration.json").read_bytes()
    if (
        str(Digest.of_bytes(payload)) != metadata["raw_digest"]
        or len(payload) != metadata["raw_bytes"]
        or str(Digest.of_bytes(config_bytes)) != metadata["configuration_digest"]
    ):
        raise ValueError("Retained METAR bytes or configuration checksum mismatch")
    configuration = MesoForgeConfiguration.model_validate_json(config_bytes)
    return metadata, configuration, payload


def normalize_rows(
    payload: bytes,
    *,
    configuration: MesoForgeConfiguration,
    station_ids: tuple[str, ...],
    raw_artifact_id: ArtifactId,
    station_snapshot_artifact_id: ArtifactId,
    ingested_at: datetime,
    query_window_start: datetime,
    query_window_end: datetime,
    station_definitions: tuple[StationDefinition, ...] | None = None,
) -> dict[str, Any]:
    """Use existing strict parsing/QC, retaining original provider indices after filtering."""
    phase2 = configuration.phase2
    assert phase2 is not None
    definitions = {
        s.provider_icao_id: s
        for s in (phase2.stations if station_definitions is None else station_definitions)
        if s.provider_icao_id in station_ids
    }
    rows, excluded = [], []
    for index, raw in enumerate(parse_raw_metar_response(payload)):
        reason = None
        if raw.icao_id not in definitions:
            reason = "Station was not requested from the retained station snapshot."
        elif not query_window_start <= raw.obs_time <= query_window_end:
            reason = "Observation event time is outside the padded acquisition window."
        if reason:
            excluded.append({"raw_record_index": index, "reason": reason})
            continue
        definition = definitions[raw.icao_id]
        station = StationRecord(
            station_id=definition.station_id,
            provider_icao_id=definition.provider_icao_id,
            latitude=definition.expected_latitude,
            longitude=definition.expected_longitude,
            elevation_m=definition.expected_elevation_m,
            site_name=definition.site_name,
            site_types=definition.provider_site_types,
        )
        row = normalize_metar_record_v2(
            raw=RawMetarRecordV2.model_validate(raw.model_dump(mode="python"), strict=True),
            station=station,
            station_id=station.station_id,
            policy=phase2.observation_normalization_policy,
            raw_artifact_id=raw_artifact_id,
            raw_record_index=index,
            station_snapshot_artifact_id=station_snapshot_artifact_id,
            ingested_at=ingested_at,
            query_window_start=query_window_start,
            query_window_end=query_window_end,
        )
        rows.append(row.model_dump(mode="json"))
    return {"rows": rows, "excluded": excluded}


def prepare_bundle(raw_dir: Path) -> dict[str, Any]:
    """Register raw/configuration evidence and derive the existing observation artifact."""
    metadata, configuration, payload = load_bundle(raw_dir)
    phase2 = configuration.phase2
    assert phase2 is not None
    dsn = resolve_database_dsn("MESOFORGE_DATABASE_DSN")
    factory = cast(Any, lambda: PostgresUnitOfWork(dsn))
    snapshot = ConfigurationService(factory).register(configuration)
    objects = S3ArtifactObjectStore(
        bucket=os.environ["MESOFORGE_S3_BUCKET"],
        endpoint_url=os.environ["MESOFORGE_S3_ENDPOINT"],
        access_key=os.environ["MESOFORGE_S3_ACCESS_KEY"],
        secret_key=os.environ["MESOFORGE_S3_SECRET_KEY"],
        ensure_bucket=False,
    )
    artifacts = ArtifactService(
        unit_of_work_factory=factory,
        object_store=cast(Any, objects),
        idempotency_lock=PostgresIdempotencyLock(dsn),
    )
    acquired = datetime.fromisoformat(metadata["acquired_at"])
    original_identity = metadata["acquisition_code_identity"]

    def register(kind: str, schema: str, data: bytes, locator: str) -> Any:
        return artifacts.register_source(
            SourceRegistrationRequest(
                source_authority="aviationweather.gov"
                if kind == "aviationweather-metar-response"
                else "mesoforge.retained-configuration",
                source_locator=locator,
                source_revision=metadata["acquired_at"],
                artifact_type=kind,
                artifact_schema_version=schema,
                media_type="application/json",
                expected_content_digest=Digest.of_bytes(data),
                created_at=acquired,
                availability=Availability(
                    available_at=acquired,
                    ingested_at=acquired,
                    authority="mesoforge",
                    method="local-acquisition-complete",
                ),
                configuration_snapshot_id=snapshot.configuration_snapshot_id,
                configuration_digest=snapshot.configuration_digest,
                code_revision=original_identity["git_commit"],
                environment_digest=Digest.of_bytes(_JSON.serialize(original_identity)),
                attributes=metadata,
            ),
            data,
        )

    raw = register(
        "aviationweather-metar-response",
        "aviationweather-metar-response.v1",
        payload,
        metadata["url"],
    )
    identity = _identity()
    definitions = _bundle_definitions(metadata, configuration)
    station_payload = {
        "schema_version": "station-catalog-snapshot.v1",
        "domain_id": "coordinate-metar.v1"
        if "station_discovery" in metadata
        else str(phase2.domain.domain_id),
        "station_ids": [str(s.station_id) for s in definitions],
        "stations": [s.model_dump(mode="json", exclude={"schema_version"}) for s in definitions],
        "point_extraction_policy": phase2.point_extraction_policy.model_dump(mode="json"),
    }
    if "station_discovery" in metadata:
        discovery = metadata["station_discovery"]
        discovery_id = ArtifactId(discovery["discovery_artifact_id"])

        def station_transform(discovery_bytes: bytes) -> dict[str, Any]:
            if str(Digest.of_bytes(discovery_bytes)) != discovery["content_digest"]:
                raise ValueError("Station discovery checksum mismatch")
            saved = _JSON.deserialize(discovery_bytes)
            if saved["candidates"] != discovery["candidates"]:
                raise ValueError("Retained discovery candidates disagree with immutable source")
            return station_payload

        def validate_station_snapshot(value: dict[str, Any]) -> None:
            parse_station_snapshot(_JSON.serialize(value))

        stations = artifacts.execute_raw_transformation(
            TransformationRequest(
                activity_type="prepare-discovered-station-snapshot",
                activity_version="v1",
                inputs=(TransformationInputRef(role="discovery", artifact_id=discovery_id),),
                output_role="stations",
                output_artifact_type="station-catalog-snapshot",
                output_artifact_schema_version="station-catalog-snapshot.v1",
                output_media_type="application/json",
                parameters={"station_snapshot": station_payload},
                configuration_snapshot_id=snapshot.configuration_snapshot_id,
                configuration_digest=snapshot.configuration_digest,
                code_revision=identity["git_commit"],
                environment_digest=Digest.of_bytes(_JSON.serialize(identity)),
            ),
            station_transform,
            _JSON,
            input_loader=bytes,
            output_validator=validate_station_snapshot,
        ).output
    else:
        stations = register(
            "station-catalog-snapshot",
            "station-catalog-snapshot.v1",
            _JSON.serialize(station_payload),
            f"retained-configuration://{snapshot.configuration_digest}/metar-stations",
        )
    request = TransformationRequest(
        activity_type="prepare-retained-metar",
        activity_version="v1",
        inputs=(
            TransformationInputRef(role="raw", artifact_id=raw.artifact_id),
            TransformationInputRef(role="stations", artifact_id=stations.artifact_id),
        ),
        output_role="observations",
        output_artifact_type="normalized-metar-observations",
        output_artifact_schema_version="metar-observations.v2",
        output_media_type="application/json",
        parameters={"source": metadata, "preparation_code_identity": identity},
        configuration_snapshot_id=snapshot.configuration_snapshot_id,
        configuration_digest=snapshot.configuration_digest,
        code_revision=identity["git_commit"],
        environment_digest=Digest.of_bytes(_JSON.serialize(identity)),
    )

    def transform(raw_bytes: bytes, station_bytes: bytes) -> dict[str, Any]:
        if Digest.of_bytes(station_bytes) != stations.content_digest:
            raise ValueError("Station snapshot checksum mismatch")
        result = normalize_rows(
            raw_bytes,
            configuration=configuration,
            station_ids=tuple(metadata["station_ids"]),
            raw_artifact_id=raw.artifact_id,
            station_snapshot_artifact_id=stations.artifact_id,
            ingested_at=acquired,
            query_window_start=datetime.fromisoformat(metadata["query_window_start"]),
            query_window_end=datetime.fromisoformat(metadata["query_window_end"]),
            station_definitions=definitions,
        )
        return {**result, "source_provenance": metadata}

    def validate(value: dict[str, Any]) -> None:
        for row in value["rows"]:
            NormalizedObservationV2.model_validate_json(_JSON.serialize(row))

    result = artifacts.execute_raw_transformation(
        request, transform, _JSON, input_loader=bytes, output_validator=validate
    )
    _, normalized_bytes = artifacts.load_verified_payload(result.output.artifact_id)
    normalized = _JSON.deserialize(normalized_bytes)
    return {
        "observations_artifact_id": str(result.output.artifact_id),
        "raw_artifact_id": str(raw.artifact_id),
        "source": metadata,
        "normalized_rows": len(normalized["rows"]),
        "excluded": normalized["excluded"],
        "station_observation_counts": {
            station: sum(row["provider_station_id"] == station for row in normalized["rows"])
            for station in metadata["station_ids"]
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument(
        "--from-raw", action="store_true", help="Reuse this retained bundle; no HTTP"
    )
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--start-valid-time", type=datetime.fromisoformat)
    parser.add_argument("--end-valid-time", type=datetime.fromisoformat)
    args = parser.parse_args(argv)
    inputs = (args.lat, args.lon, args.start_valid_time, args.end_valid_time)
    if (args.from_raw and any(v is not None for v in inputs)) or (
        not args.from_raw and any(v is None for v in inputs)
    ):
        parser.error("Supply coordinate/window for acquisition, or --from-raw alone for rebuilding")
    try:
        if not args.from_raw:
            acquire_bundle(
                args.raw_dir,
                latitude=args.lat,
                longitude=args.lon,
                start_valid_time=args.start_valid_time,
                end_valid_time=args.end_valid_time,
            )
        result = prepare_bundle(args.raw_dir)
    except Exception as exc:
        print(json.dumps({"error": str(exc), "raw_dir": str(args.raw_dir)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
