"""Publish immutable point-forecast versions through the existing storage boundary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from mesoforge.common.errors import IntegrityError
from mesoforge.common.identifiers import Digest
from mesoforge.contracts.issued_forecasts import IssuedForecastRecord
from mesoforge.storage.interfaces import ArtifactObjectStore, IssuanceUnitOfWork
from mesoforge.storage.json import CanonicalJsonSerializer


def _now() -> datetime:
    return datetime.now(UTC)


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

    def read(self, issued_forecast_id: UUID) -> dict[str, Any]:
        """Read one exact saved version and verify its stored content checksum."""
        with self._uow_factory() as uow:
            record = uow.issued_forecasts.get(issued_forecast_id)
            stored = uow.stored_objects.get(record.content_digest)
        return self._serializer.deserialize(
            self._objects.get_verified(stored.storage_uri, record.content_digest)
        )
