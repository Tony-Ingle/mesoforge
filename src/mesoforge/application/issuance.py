"""Publish immutable point-forecast versions through the existing storage boundary."""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
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
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore

# Preserve the existing forward-run key so both issuance entry points coordinate.
FORWARD_RUN_LOCK = Digest.of_bytes(b"mesoforge.forward-run.v1")


class ForecastExpiredError(ValueError):
    """A prospective baseline's first valid hour passed before issuance began."""


def acquire_issuance_run_lock(*, wait: bool = False) -> AbstractContextManager[None]:
    """Serialize the decision-window lookup and issuance across PostgreSQL sessions.

    Forward runs retain their non-blocking overlap behavior. Snapshot issuance waits
    and then rechecks saved versions, so a concurrent follower skips a prior primary
    issuance unless the caller explicitly requested a reissue.
    """
    lock = PostgresIdempotencyLock(resolve_database_dsn("MESOFORGE_DATABASE_DSN"))
    return lock.acquire(FORWARD_RUN_LOCK) if wait else lock.try_acquire(FORWARD_RUN_LOCK)


def _now() -> datetime:
    return datetime.now(UTC)


def issued_forecast_context(forecast: dict[str, Any]) -> dict[str, Any]:
    """Point-hour audit context; the exact issuance retains the complete spatial grid.

    Keep its local_grid checksum, geometry and extraction metadata here, without
    duplicating every other cell/hour in each selection or verification result.
    """
    context = {
        key: value for key, value in forecast.items() if key not in ("hours", "local_grid_baseline")
    }
    for stage_key, reference_key in (
        ("learning_stage", "learning_reference"),
        ("deterministic_stage", "deterministic_reference"),
        ("baseline_stage", "baseline_stage_reference"),
    ):
        stage = context.get(stage_key)
        if not isinstance(stage, dict):
            continue
        # Point-hour verification needs exact identity/status, not every other
        # corrected cell. This is deliberately not a sealed variant payload;
        # the full immutable stage remains in its artifact and issued forecast.
        overlay = stage.get("overlay", {})
        correction = overlay.get("correction", {})
        context[stage_key] = {
            "representation": "summary_reference_not_sealed_variant",
            "source_schema_version": stage.get("schema_version"),
            **{
                key: stage.get(key)
                for key in (
                    "variant_id",
                    "parent_stage_id",
                    "transformation_type",
                    "lifecycle_role",
                    "fields",
                    "policy",
                    "baseline_snapshot_id",
                    "prepared_snapshot_id",
                    "parent_grid_sha256",
                    "location",
                    "reference_time",
                    "analysis_cutoff",
                    "evidence_cutoff",
                    "evidence_status",
                    "policy_created_at",
                    "policy_activated_at",
                    "created_at",
                )
            },
            "authoritative_artifact": forecast.get(reference_key),
            "overlay": {
                "inherit_unchanged": overlay.get("inherit_unchanged"),
                "predictions": bool(overlay.get("predictions")),
                "correction": {
                    "status": correction.get("status"),
                    "changes": bool(correction.get("changes")),
                },
            },
        }
        if stage_key == "baseline_stage":
            context[stage_key]["baseline_temperature_predictions"] = [
                {key: row.get(key) for key in ("field", "value", "unit", "valid_time")}
                for row in overlay.get("predictions", [])
                if row.get("field") == "air_temperature_2m"
            ]
    if isinstance(context.get("ai_desk"), dict):
        desk = context["ai_desk"]
        ai_stage = forecast.get("learning_stage", {}).get("transformation_type") == "ai_adjusted"
        context["ai_desk"] = {
            "representation": "summary_reference_not_full_audit",
            # Only a retained AI stage is authoritative; a corrected fallback has none.
            "authoritative_artifact": forecast.get("learning_reference") if ai_stage else None,
            "issued_checkpoint": desk.get(
                "issued_checkpoint", "latest_valid_ai_checkpoint" if ai_stage else None
            ),
            **{
                key: desk.get(key)
                for key in (
                    "policy",
                    "provider",
                    "model",
                    "context_digest",
                    "completion_reason",
                    "validation",
                )
            },
            "accepted_edit_count": len(desk.get("accepted_recipes", [])),
        }
    return context


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


def refuse_ungoverned_issuance(uow: Any, latitude: float, longitude: float) -> None:
    """Development/replay issuance carries no governed stage; never issue past ACTIVE policy.

    Configured issuance pins governance through the baseline path. Any other path is
    refused for a coordinate with an ACTIVE correction, or while any blend policy is
    ACTIVE, and fails closed when the governed state cannot be read. With nothing
    active (the default), behavior is unchanged.
    """
    from mesoforge.contracts.policy_governance import (
        BLEND_POLICY,
        GOVERNANCE_LOCK_TIMEOUT_SECONDS,
        TEMPERATURE_CORRECTION,
        GovernanceBlockedError,
        correction_scope,
        head_at,
    )

    governance = getattr(uow, "governance", None)
    try:
        if governance is None:
            raise RuntimeError("issuance storage has no governance repository")
        for family, keys in (
            (TEMPERATURE_CORRECTION, (correction_scope(latitude, longitude),)),
            (BLEND_POLICY, None),
        ):
            governance.lock(family, shared=True, timeout_seconds=GOVERNANCE_LOCK_TIMEOUT_SECONDS)
            events = governance.events(family, keys)
            for key in {row.scope_key for row in events}:
                head = head_at([row for row in events if row.scope_key == key])
                if head is not None and head.policy_artifact_id is not None:
                    raise GovernanceBlockedError(
                        "governed_policy_active_use_baseline_path",
                        "A governed policy is ACTIVE; issue through forecast_from_baseline",
                    )
    except GovernanceBlockedError:
        raise
    except Exception as exc:
        raise GovernanceBlockedError(
            "governance_unavailable", "Governed state could not be read; nothing was issued"
        ) from exc


def refuse_revoked_issuance(uow: Any, forecast: dict[str, Any]) -> None:
    """Refuse a configured forecast whose pinned governed state was rolled back since.

    Runs inside the issuance metadata transaction under the shared governance family
    locks, which stay held until commit. A rollback committed before this check blocks
    the issuance; a rollback recorded afterwards follows the issuance. Nothing is read
    when the forecast pinned no chain head (the default, nothing active).
    """
    from mesoforge.contracts.policy_governance import (
        GOVERNANCE_LOCK_TIMEOUT_SECONDS,
        GovernanceBlockedError,
        pinned_scopes,
        rolled_back_after,
    )

    pinned = pinned_scopes(forecast)
    if not pinned:
        return
    governance = getattr(uow, "governance", None)
    try:
        if governance is None:
            raise RuntimeError("issuance storage has no governance repository")
        for family in sorted({row[0] for row in pinned}):
            governance.lock(family, shared=True, timeout_seconds=GOVERNANCE_LOCK_TIMEOUT_SECONDS)
            scopes = tuple(sorted({row[1] for row in pinned if row[0] == family}))
            events = governance.events(family, scopes)
            for pinned_family, scope_key, scope_seq in pinned:
                if pinned_family == family and rolled_back_after(events, scope_key, scope_seq):
                    raise GovernanceBlockedError(
                        "policy_rolled_back_before_issuance",
                        "A governed policy this forecast pinned was rolled back before issuance",
                    )
    except GovernanceBlockedError:
        raise
    except Exception as exc:
        raise GovernanceBlockedError(
            "governance_unavailable", "Governed state could not be read; nothing was issued"
        ) from exc


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
        for lineage in ("prepared_snapshot", "baseline_snapshot"):
            cutoff = forecast.get(lineage, {}).get("forecast_analysis_cutoff")
            if cutoff is not None:
                analysis_cutoff = datetime.fromisoformat(cutoff)
                if analysis_cutoff.tzinfo is None or analysis_cutoff.utcoffset() is None:
                    raise ValueError("Forecast analysis cutoff must be timezone-aware")
                if analysis_cutoff > issued_at:
                    raise ValueError("Forecast analysis cutoff cannot follow issuance time")
        target = datetime.fromisoformat(forecast["target_reference_time"])
        if target.tzinfo is None:
            raise ValueError("Target reference time must be timezone-aware")
        if [hour["horizon_hours"] for hour in forecast["hours"]] != list(range(1, 37)):
            raise ValueError("Issued temperature forecasts must contain hours 1..36")
        if forecast.get("baseline_snapshot", {}).get("reference_time_source") == "request_hour":
            # Readiness preceded extraction, correction, the desk and presentation.
            # Check the actual issuance clock again before any immutable write. An
            # explicit replay reference (or a legacy payload without this lineage)
            # retains its historical contract; never backdate or shorten a normal job.
            first_valid = datetime.fromisoformat(forecast["hours"][0]["valid_time"])
            if first_valid.tzinfo is None or first_valid.utcoffset() is None:
                raise ValueError("First forecast valid time must be timezone-aware")
            if first_valid <= issued_at:
                raise ForecastExpiredError(
                    "Prospective forecast first valid hour is no longer future; nothing was issued"
                )
        issued_forecast_id = uuid4()
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
            if "baseline_snapshot" not in forecast:
                refuse_ungoverned_issuance(uow, forecast["latitude"], forecast["longitude"])
            else:
                refuse_revoked_issuance(uow, forecast)
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
