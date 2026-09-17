"""Publish immutable point-forecast versions through the existing storage boundary."""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.common.identifiers import Digest
from mesoforge.common.time import IntervalClosure, IntervalDefinition
from mesoforge.contracts.issued_forecasts import IssuedForecastRecord
from mesoforge.storage.interfaces import ArtifactObjectStore, IssuanceUnitOfWork
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore


def _now() -> datetime:
    return datetime.now(UTC)


def issued_forecast_context(forecast: dict[str, Any]) -> dict[str, Any]:
    """Point-hour audit context; the exact issuance retains the complete spatial grid.

    Keep its local_grid checksum, geometry and extraction metadata here, without
    duplicating every other cell/hour in each selection or verification result.
    """
    return {
        key: value for key, value in forecast.items() if key not in ("hours", "local_grid_baseline")
    }


def validate_hour_selection(
    latitude: float, longitude: float, start_valid_time: datetime, end_valid_time: datetime
) -> IntervalDefinition:
    """Validate exact geographic coordinates and an aware, start-inclusive time window."""
    for value, bound in ((latitude, 90), (longitude, 180)):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not -bound <= value <= bound
        ):
            raise ValueError("latitude and longitude must be finite geographic coordinates")
    return IntervalDefinition(
        start=start_valid_time,
        end=end_valid_time,
        closure=IntervalClosure.left_closed_right_open,
    )


# Every issued version is validated to hold exactly these horizons, each valid at the
# target reference time plus its horizon, so issuance metadata alone bounds its hours.
ISSUED_HORIZON_HOURS = (1, 36)
VERSION_PREFILTER = "target_reference_time_plus_horizons_1_to_36"


def possible_valid_window(target_reference_time: datetime) -> tuple[datetime, datetime]:
    """Earliest and latest valid times any saved hour of one version can have."""
    first, last = ISSUED_HORIZON_HOURS
    return (
        target_reference_time + timedelta(hours=first),
        target_reference_time + timedelta(hours=last),
    )


def version_may_overlap(record: IssuedForecastRecord, window: IntervalDefinition) -> bool:
    """Metadata-only test against a start-inclusive, end-exclusive window; no object read."""
    first, last = possible_valid_window(record.target_reference_time)
    return first < window.end and last >= window.start


class ForecastIssuanceService:
    """Store verified forecast bytes first, then publish one metadata transaction."""

    def __init__(
        self,
        object_store: ArtifactObjectStore,
        uow_factory: Callable[[], IssuanceUnitOfWork],
        *,
        code_identity: dict[str, Any],
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._objects = object_store
        self._uow_factory = uow_factory
        self._code_identity = code_identity
        self._clock = clock
        self._serializer = CanonicalJsonSerializer()

    def issue(
        self, forecast: dict[str, Any], *, batch_run_id: UUID, location_index: int
    ) -> IssuedForecastRecord:
        """Create a new version even if another issuance has identical numerical values."""
        issued_at = self._clock()
        if issued_at.tzinfo is None:
            raise ValueError("Issuance time must be timezone-aware")
        issued_at = issued_at.astimezone(UTC)
        issued_forecast_id = uuid4()
        target = datetime.fromisoformat(forecast["target_reference_time"])
        if target.tzinfo is None:
            raise ValueError("Target reference time must be timezone-aware")
        if [hour["horizon_hours"] for hour in forecast["hours"]] != list(range(1, 37)):
            raise ValueError("Issued temperature forecasts must contain hours 1..36")
        metadata = {
            "schema_version": "issued-forecast.v1",
            "issued_forecast_id": str(issued_forecast_id),
            "batch_run_id": str(batch_run_id),
            "location_index": location_index,
            "latitude": forecast["latitude"],
            "longitude": forecast["longitude"],
            "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
            "target_reference_time": target.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        payload = self._serializer.serialize(
            {**metadata, "code_identity": self._code_identity, "forecast": forecast}
        )
        digest = Digest.of_bytes(payload)
        record = IssuedForecastRecord(
            issued_forecast_id=issued_forecast_id,
            batch_run_id=batch_run_id,
            location_index=location_index,
            latitude=forecast["latitude"],
            longitude=forecast["longitude"],
            issued_at=issued_at,
            target_reference_time=target,
            content_digest=digest,
        )
        stored = self._objects.put_if_absent(digest, payload, "application/json")
        # A failed upload/readback must never become a successful PostgreSQL issuance.
        if self._objects.get_verified(stored.storage_uri, digest) != payload:
            raise IntegrityError("Stored issued forecast differs from the serialized forecast")
        with self._uow_factory() as uow:
            uow.stored_objects.add_if_absent(stored)
            uow.issued_forecasts.add(record)
            uow.commit()
        return record

    def find_versions(
        self, *, latitude: float, longitude: float, target_reference_time: datetime
    ) -> tuple[IssuedForecastRecord, ...]:
        """Metadata-only lookup of saved versions sharing one coordinate and target time."""
        if target_reference_time.tzinfo is None or target_reference_time.utcoffset() is None:
            raise ValueError("target_reference_time must include a timezone")
        with self._uow_factory() as uow:
            records = uow.issued_forecasts.list_for_coordinate(latitude, longitude, limit=None)
        return tuple(
            record for record in records if record.target_reference_time == target_reference_time
        )

    def read(self, issued_forecast_id: UUID) -> dict[str, Any]:
        """Read one exact saved version and verify its stored content checksum."""
        with self._uow_factory() as uow:
            record = uow.issued_forecasts.get(issued_forecast_id)
            try:
                stored = uow.stored_objects.get(record.content_digest)
            except NotFound as exc:
                raise IntegrityError("Issued forecast object metadata is missing") from exc
        try:
            payload = self._objects.get_verified(stored.storage_uri, record.content_digest)
        except NotFound as exc:
            raise IntegrityError("Issued forecast payload is missing") from exc
        return self._serializer.deserialize(payload)

    def select_hours(
        self,
        *,
        latitude: float,
        longitude: float,
        start_valid_time: datetime,
        end_valid_time: datetime,
    ) -> dict[str, Any]:
        """Select saved hours by actual valid time, retaining every matching issued version."""
        window = validate_hour_selection(latitude, longitude, start_valid_time, end_valid_time)
        with self._uow_factory() as uow:
            records = uow.issued_forecasts.list_for_coordinate(latitude, longitude, limit=None)
        # Reject versions from metadata first; only possibly overlapping payloads are read.
        candidates = [record for record in records if version_may_overlap(record, window)]
        results: list[dict[str, Any]] = []
        for record in candidates:
            saved = self.read(record.issued_forecast_id)
            forecast = saved["forecast"]
            context = issued_forecast_context(forecast)
            for hour in forecast["hours"]:
                valid_time = datetime.fromisoformat(hour["valid_time"])
                if valid_time.tzinfo is None:
                    raise IntegrityError("Saved forecast hour has no valid-time timezone")
                if window.start <= valid_time < window.end:
                    results.append(
                        {
                            "issued": record.model_dump(mode="json"),
                            "code_identity": saved["code_identity"],
                            "forecast_context": context,
                            "hour": hour,
                        }
                    )
        results.sort(
            key=lambda row: (
                datetime.fromisoformat(row["hour"]["valid_time"]),
                datetime.fromisoformat(row["issued"]["issued_at"]),
                row["issued"]["issued_forecast_id"],
            )
        )
        return {
            "latitude": latitude,
            "longitude": longitude,
            "start_valid_time": window.start.isoformat().replace("+00:00", "Z"),
            "end_valid_time": window.end.isoformat().replace("+00:00", "Z"),
            "interval_closure": window.closure.value,
            "version_scan": {
                "versions_for_coordinate": len(records),
                "versions_read": len(candidates),
                "prefilter": VERSION_PREFILTER,
            },
            "results": results,
        }


def _configured_reader() -> ForecastIssuanceService:
    """Use existing configured storage without bucket creation or issuance setup."""
    dsn = resolve_database_dsn("MESOFORGE_DATABASE_DSN")
    objects = S3ArtifactObjectStore(
        bucket=os.environ["MESOFORGE_S3_BUCKET"],
        endpoint_url=os.environ["MESOFORGE_S3_ENDPOINT"],
        access_key=os.environ["MESOFORGE_S3_ACCESS_KEY"],
        secret_key=os.environ["MESOFORGE_S3_SECRET_KEY"],
        ensure_bucket=False,
    )
    # Readback returns the stored code identity; current issuance identity is unused.
    return ForecastIssuanceService(objects, lambda: PostgresUnitOfWork(dsn), code_identity={})


def read_issued_forecast(issued_forecast_id: UUID) -> dict[str, Any]:
    """Read one exact saved version through the existing configured storage path."""
    return _configured_reader().read(issued_forecast_id)


def select_issued_forecast_hours(
    *, latitude: float, longitude: float, start_valid_time: datetime, end_valid_time: datetime
) -> dict[str, Any]:
    """Read matching saved hours without creating buckets, objects, or database rows."""
    validate_hour_selection(latitude, longitude, start_valid_time, end_valid_time)
    return _configured_reader().select_hours(
        latitude=latitude,
        longitude=longitude,
        start_valid_time=start_valid_time,
        end_valid_time=end_valid_time,
    )
