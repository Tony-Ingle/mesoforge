"""Prepare one bounded historical METAR dataset for the existing verification command."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from pyproj import Geod

from mesoforge.application.artifacts import (
    ArtifactService,
    SourceRegistrationRequest,
    TransformationInputRef,
    TransformationRequest,
)
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.issuance import validate_hour_selection
from mesoforge.application.prepared_temperature import _code_identity
from mesoforge.catalog.configuration import MesoForgeConfiguration, load_configuration_source
from mesoforge.catalog.stations import StationRecord
from mesoforge.common.identifiers import ArtifactId, Digest
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
    identity["git_commit"] = subprocess.run(  # noqa: S603
        ["git", "-C", str(_ROOT), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return identity


def load_observation_configuration() -> MesoForgeConfiguration:
    configuration, _ = load_configuration_source(
        base_path=_ROOT / "configs/base.yaml",
        environment_path=_ROOT / "configs/phase1-grasston.yaml",
        additional_overlay_paths=(_ROOT / "configs/phase2-grasston.yaml",),
    )
    return configuration


def retained_station_ids(
    configuration: MesoForgeConfiguration, latitude: float, longitude: float
) -> tuple[str, ...]:
    phase2 = configuration.phase2
    assert phase2 is not None
    geod = Geod(ellps="WGS84")
    station_ids = tuple(
        sorted(
            station.provider_icao_id
            for station in phase2.stations
            if geod.inv(longitude, latitude, station.expected_longitude, station.expected_latitude)[
                2
            ]
            <= 50_000
        )
    )
    if not station_ids:
        raise ValueError("No retained METAR stations lie within 50 km")
    return station_ids


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
    )


def acquire_for_valid_times(
    raw_dir: Path, *, latitude: float, longitude: float, valid_times: tuple[datetime, ...]
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
    if not (45.5 <= latitude <= 46.0 and -93.5 <= longitude <= -93.0):
        raise ValueError("Coordinate is outside the supported Grasston demonstration area")
    if end_valid_time - start_valid_time > timedelta(hours=6):
        raise ValueError("This bounded preparation command accepts at most six hours per dataset")
    clock = clock or SystemClock()
    start = start_valid_time.astimezone(UTC) - timedelta(minutes=15)
    end = end_valid_time.astimezone(UTC) + timedelta(minutes=15)
    if end > clock.now():
        raise ValueError("The entire window, including the 15-minute margin, must be in the past")
    if raw_dir.resolve().is_relative_to(_ROOT):
        raise ValueError("Raw observation data must be retained outside the repository")
    configuration = load_observation_configuration()
    phase2 = configuration.phase2
    assert phase2 is not None
    station_ids = retained_station_ids(configuration, latitude, longitude)
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
        "station_metadata_source": "retained_configuration",
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
) -> dict[str, Any]:
    """Use existing strict parsing/QC, retaining original provider indices after filtering."""
    phase2 = configuration.phase2
    assert phase2 is not None
    definitions = {
        s.provider_icao_id: s for s in phase2.stations if s.provider_icao_id in station_ids
    }
    rows, excluded = [], []
    for index, raw in enumerate(parse_raw_metar_response(payload)):
        reason = None
        if raw.icao_id not in definitions:
            reason = "Station was not requested from the retained catalog."
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
    definitions = tuple(s for s in phase2.stations if s.provider_icao_id in metadata["station_ids"])
    stations = register(
        "station-catalog-snapshot",
        "station-catalog-snapshot.v1",
        _JSON.serialize(
            {
                "schema_version": "station-catalog-snapshot.v1",
                "domain_id": str(phase2.domain.domain_id),
                "station_ids": [str(s.station_id) for s in definitions],
                "stations": [
                    s.model_dump(mode="json", exclude={"schema_version"}) for s in definitions
                ],
                "point_extraction_policy": phase2.point_extraction_policy.model_dump(mode="json"),
            }
        ),
        f"retained-configuration://{snapshot.configuration_digest}/metar-stations",
    )
    identity = _identity()
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
