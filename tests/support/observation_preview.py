"""Explicit synthetic retained observations for preview tests and local demonstrations.

This helper writes only during test/demo setup. Forecast HTTP handlers never call it.
Station definitions come from the retained Phase 2 configuration; the observations
are invented test data and are labeled accordingly in every artifact manifest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from mesoforge.application.artifacts import ArtifactService, SourceRegistrationRequest
from mesoforge.application.configuration import ConfigurationService
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.catalog.stations import StationRecord
from mesoforge.common.identifiers import Digest
from mesoforge.contracts.artifacts import ArtifactManifest, Availability
from mesoforge.contracts.observations_v2 import RawMetarRecordV2
from mesoforge.observations.normalization_v2 import normalize_metar_record_v2
from mesoforge.observations.sources.aviationweather import parse_raw_metar_response
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore

ROOT = Path(__file__).resolve().parents[2]
JSON = CanonicalJsonSerializer()
NOTICE = "Synthetic observation fixtures; these are not measured weather observations."


def seed_observation_preview_inputs(
    dsn: str, object_store: S3ArtifactObjectStore, valid_time: datetime
) -> ArtifactManifest:
    """Register one normalized observation input and its raw/station provenance.

    KJMR fails temperature range QC; KROS reports 20 C ten minutes later;
    farther KCBG reports 10 C at the requested time. This deliberately tests
    station distance priority over observation time proximity.
    """
    valid_time = valid_time.astimezone(UTC)
    configuration, _ = load_configuration_source(
        base_path=ROOT / "configs/base.yaml",
        environment_path=ROOT / "configs/phase1-grasston.yaml",
        additional_overlay_paths=(ROOT / "configs/phase2-grasston.yaml",),
    )
    phase2 = configuration.phase2
    assert phase2 is not None
    snapshot = ConfigurationService(lambda: PostgresUnitOfWork(dsn)).register(configuration)
    artifacts = ArtifactService(
        unit_of_work_factory=lambda: PostgresUnitOfWork(dsn),
        object_store=object_store,
        idempotency_lock=PostgresIdempotencyLock(dsn),
    )
    now = datetime.now(UTC)

    def register(kind: str, schema: str, payload: Any) -> ArtifactManifest:
        encoded = JSON.serialize(payload)
        return artifacts.register_source(
            SourceRegistrationRequest(
                source_authority="mesoforge.test-fixture",
                source_locator=f"test-fixture://observation-preview/{valid_time.isoformat()}/{kind}",
                source_revision="observation-preview-fixture.v1",
                artifact_type=kind,
                artifact_schema_version=schema,
                media_type="application/json",
                expected_content_digest=Digest.of_bytes(encoded),
                created_at=now,
                availability=Availability(
                    available_at=now,
                    authority="mesoforge.test-fixture",
                    method="explicit-local-fixture-registration",
                    ingested_at=now,
                ),
                configuration_snapshot_id=snapshot.configuration_snapshot_id,
                configuration_digest=snapshot.configuration_digest,
                code_revision="a" * 40,
                environment_digest=Digest.of_bytes(b"observation-preview-test-fixture"),
                attributes={"data_kind": "synthetic_observation_fixture", "notice": NOTICE},
            ),
            encoded,
        )

    stations = register(
        "station-catalog-snapshot",
        "station-catalog-snapshot.v1",
        {
            "schema_version": "station-catalog-snapshot.v1",
            "domain_id": str(phase2.domain.domain_id),
            "station_ids": [str(station.station_id) for station in phase2.stations],
            "stations": [
                station.model_dump(mode="json", exclude={"schema_version"})
                for station in phase2.stations
            ],
            "point_extraction_policy": phase2.point_extraction_policy.model_dump(mode="json"),
        },
    )
    raw_rows = []
    for station in phase2.stations:
        time = valid_time + timedelta(minutes=10 if station.provider_icao_id == "KROS" else 0)
        temp = {"KCBG": 10.0, "KJMR": 400.0, "KROS": 20.0}[station.provider_icao_id]
        raw_rows.append(
            {
                "icaoId": station.provider_icao_id,
                "obsTime": int(time.timestamp()),
                "reportTime": time.isoformat(),
                "receiptTime": (time + timedelta(minutes=1)).isoformat(),
                "temp": temp,
                "dewp": 5.0,
                "wdir": 180.0,
                "wspd": 5.0,
                "qcField": 0.0,
                "metarType": "METAR",
                "rawOb": f"SYNTHETIC FIXTURE {station.provider_icao_id} {time.isoformat()} {temp}C",
                "lat": station.expected_latitude,
                "lon": station.expected_longitude,
                "elev": station.expected_elevation_m,
            }
        )
    raw = register("aviationweather-metar-response", "aviationweather-metar-response.v1", raw_rows)
    raw_records = parse_raw_metar_response(JSON.serialize(raw_rows))
    by_id = {station.provider_icao_id: station for station in phase2.stations}
    rows = []
    for index, raw_record in enumerate(raw_records):
        definition = by_id[raw_record.icao_id]
        station = StationRecord(
            station_id=definition.station_id,
            provider_icao_id=definition.provider_icao_id,
            latitude=definition.expected_latitude,
            longitude=definition.expected_longitude,
            elevation_m=definition.expected_elevation_m,
            site_name=definition.site_name,
            site_types=definition.provider_site_types,
        )
        normalized = normalize_metar_record_v2(
            raw=RawMetarRecordV2.model_validate(raw_record.model_dump(mode="python"), strict=True),
            station=station,
            station_id=station.station_id,
            policy=phase2.observation_normalization_policy,
            raw_artifact_id=raw.artifact_id,
            raw_record_index=index,
            station_snapshot_artifact_id=stations.artifact_id,
            ingested_at=now,
            query_window_start=valid_time - timedelta(hours=1),
            query_window_end=valid_time + timedelta(hours=1),
        )
        rows.append(normalized.model_dump(mode="json"))
    return register("normalized-metar-observations", "metar-observations.v2", {"rows": rows})


def complete_storage_inventory(dsn: str, object_store: S3ArtifactObjectStore) -> dict[str, Any]:
    """Capture all PostgreSQL row contents and MinIO object identities, read-only."""
    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as connection:
            tables = {}
            # The table names come from PostgreSQL, then SQLAlchemy quotes them.
            for name in sa.inspect(connection).get_table_names():
                table = sa.Table(name, sa.MetaData(), autoload_with=connection)
                rows = connection.execute(sa.select(table)).mappings()
                tables[name] = sorted((dict(row) for row in rows), key=repr)
    finally:
        engine.dispose()
    paginator = object_store._client.get_paginator("list_objects_v2")
    objects = sorted(
        (item["Key"], item["ETag"], item["Size"])
        for page in paginator.paginate(Bucket=object_store._bucket)
        for item in page.get("Contents", [])
    )
    return {"tables": tables, "objects": objects}
