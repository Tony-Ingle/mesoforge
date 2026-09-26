"""Behaviorally complete in-memory test doubles for
storage.interfaces-shaped protocols, used only by unit tests (plan
Section 4.9/Task 10: "in-memory test doubles defined under tests only").
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from mesoforge.common.errors import Conflict, IntegrityError, NotFound
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.contracts.issued_forecasts import IssuedForecastRecord
from mesoforge.contracts.policy_governance import (
    FAMILIES,
    GovernanceConflict,
    GovernanceEvent,
    validate_append,
)
from mesoforge.contracts.provenance import ActivityManifest


@dataclass
class _StoredObject:
    content_digest: str
    storage_uri: str
    media_type: str
    byte_size: int


class InMemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, _StoredObject] = {}
        self.fail_next_put = False
        self.corrupt_next_get = False

    def put_if_absent(self, content_digest: str, data: bytes, media_type: str) -> _StoredObject:
        if self.fail_next_put:
            self.fail_next_put = False
            raise RuntimeError("simulated upload failure")

        key = f"objects/sha256/{content_digest[7:9]}/{content_digest[9:]}"
        storage_uri = f"s3://mesoforge-test/{key}"
        if content_digest not in self.objects:
            self.objects[content_digest] = data
            self.metadata[content_digest] = _StoredObject(
                content_digest=content_digest,
                storage_uri=storage_uri,
                media_type=media_type,
                byte_size=len(data),
            )
        return self.metadata[content_digest]

    def get_verified(self, storage_uri: str, expected_digest: str) -> bytes:
        for digest, meta in self.metadata.items():
            if meta.storage_uri == storage_uri:
                data = self.objects[digest]
                if self.corrupt_next_get:
                    self.corrupt_next_get = False
                    actual_digest = "sha256:" + "0" * 64
                else:
                    actual_digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
                if actual_digest != expected_digest:
                    raise IntegrityError(
                        f"checksum mismatch for {storage_uri!r}: expected "
                        f"{expected_digest!r}, got {actual_digest!r}"
                    )
                return data
        raise NotFound(f"no object at {storage_uri!r}")


class InMemoryIdempotencyLock:
    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, digest: str) -> threading.Lock:
        with self._guard:
            if digest not in self._locks:
                self._locks[digest] = threading.Lock()
            return self._locks[digest]

    @contextmanager
    def acquire(self, digest: str):
        lock = self._lock_for(digest)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()


class _InMemoryGridRepository:
    def __init__(self, store: dict[str, tuple[str, dict]]) -> None:
        self._store = store

    def add_if_absent(self, grid_id: str, definition_digest: str, canonical_json: dict) -> object:
        existing = self._store.get(grid_id)
        if existing is not None:
            if existing[0] != definition_digest:
                raise Conflict(
                    f"grid_id {grid_id!r} already registered with a different definition"
                )
            return existing
        self._store[grid_id] = (definition_digest, canonical_json)
        return self._store[grid_id]

    def get(self, grid_id: str) -> object:
        if grid_id not in self._store:
            raise NotFound(f"grid_id {grid_id!r} not found")
        return self._store[grid_id]


class _InMemoryStoredObjectRepository:
    def __init__(self, store: dict[str, object]) -> None:
        self._store = store

    def add_if_absent(self, stored_object: object) -> object:
        digest = stored_object.content_digest  # type: ignore[attr-defined]
        if digest not in self._store:
            self._store[digest] = stored_object
        return self._store[digest]

    def get(self, content_digest: str) -> object:
        if content_digest not in self._store:
            raise NotFound(f"stored object {content_digest!r} not found")
        return self._store[content_digest]


class _InMemoryArtifactRepository:
    def __init__(
        self, store: dict[str, ArtifactManifest], stored_objects: dict[str, object]
    ) -> None:
        self._store = store
        self._stored_objects = stored_objects

    def add(self, manifest: ArtifactManifest) -> ArtifactManifest:
        self._store[manifest.artifact_id] = manifest
        return manifest

    def add_derived(self, **kwargs: object) -> ArtifactManifest:
        """In-memory stand-in for PostgresArtifactRepository.add_derived
        (finding 3, Codex review t_9bb13e2b). There is no real database
        transaction here, so this double approximates
        ``transaction_timestamp()`` with one shared ``datetime.now(UTC)``
        call used consistently for both registered_at and the derived
        available_at -- sufficient for the unit-level ordering/error-branch
        tests in tests/unit/application/test_artifact_service.py. The
        actual "one PostgreSQL transaction_timestamp()" invariant is
        proven only against real PostgreSQL, in
        tests/integration/application/test_artifact_registration.py."""
        from datetime import UTC, datetime

        from mesoforge.contracts.artifacts import Availability

        now = datetime.now(UTC)
        parent_available_ats = kwargs["parent_available_ats"]
        activity_completed_at = kwargs["activity_completed_at"]
        available_at = max([*parent_available_ats, activity_completed_at, now])  # type: ignore[list-item]
        content_digest = kwargs["content_digest"]
        stored_object = self._stored_objects[content_digest]  # type: ignore[index]
        manifest = ArtifactManifest(
            artifact_id=kwargs["artifact_id"],  # type: ignore[arg-type]
            artifact_type=kwargs["artifact_type"],  # type: ignore[arg-type]
            artifact_schema_version=kwargs["artifact_schema_version"],  # type: ignore[arg-type]
            media_type=stored_object.media_type,  # type: ignore[attr-defined]
            byte_size=stored_object.byte_size,  # type: ignore[attr-defined]
            content_digest=content_digest,  # type: ignore[arg-type]
            storage_uri=stored_object.storage_uri,  # type: ignore[attr-defined]
            created_at=kwargs["created_at"],  # type: ignore[arg-type]
            registered_at=now,
            availability=Availability(
                available_at=available_at,
                authority=kwargs["availability_authority"],  # type: ignore[arg-type]
                method=kwargs["availability_method"],  # type: ignore[arg-type]
            ),
            run_id=kwargs["run_id"],  # type: ignore[arg-type]
            configuration_snapshot_id=kwargs["configuration_snapshot_id"],  # type: ignore[arg-type]
            configuration_digest=kwargs["configuration_digest"],  # type: ignore[arg-type]
            code_revision=kwargs["code_revision"],  # type: ignore[arg-type]
            environment_digest=kwargs["environment_digest"],  # type: ignore[arg-type]
            quality_state=kwargs["quality_state"],  # type: ignore[arg-type]
            attributes=kwargs["attributes"],  # type: ignore[arg-type]
        )
        self._store[manifest.artifact_id] = manifest
        return manifest

    def get(self, artifact_id: str) -> ArtifactManifest:
        if artifact_id not in self._store:
            raise NotFound(f"artifact {artifact_id!r} not found")
        return self._store[artifact_id]

    def get_many(self, ids: tuple[str, ...]) -> tuple[ArtifactManifest, ...]:
        return tuple(self.get(i) for i in ids)

    def find_by_source_registration_digest(self, digest: str) -> ArtifactManifest | None:
        for manifest in self._store.values():
            if manifest.source_registration_digest == digest:
                return manifest
        return None

    def find_mrms_extractions(
        self, *, product_time: object, limit: int = 1000
    ) -> tuple[ArtifactManifest, ...]:
        from datetime import UTC, datetime

        if not isinstance(product_time, datetime) or product_time.tzinfo is None:
            raise ValueError("MRMS lookup requires an aware product time")
        return tuple(
            manifest
            for manifest in self._store.values()
            if manifest.artifact_type == "mrms-coordinate-extraction"
            and (manifest.attributes or {}).get("product_time")
            in (None, product_time.astimezone(UTC).isoformat())
        )[:limit]

    def find_learning_artifacts(
        self, artifact_type: str, *, attributes: dict[str, object], limit: int = 1000
    ) -> tuple[ArtifactManifest, ...]:
        if (
            artifact_type
            not in {"learning-policy", "learning-overlay", "forecast-variant", "learning-binding"}
            or not 1 <= limit <= 10000
        ):
            raise ValueError("Invalid learning artifact query")
        matches = tuple(
            sorted(
                (
                    row
                    for row in self._store.values()
                    if row.artifact_type == artifact_type
                    and all(row.attributes.get(key) == value for key, value in attributes.items())
                ),
                key=lambda row: (row.registered_at, row.artifact_id),
            )
        )
        if len(matches) > limit:
            raise ValueError("Learning artifact query is truncated; narrow its scope")
        return matches


class _InMemoryConfigurationSnapshot:
    def __init__(self, configuration_snapshot_id: str, configuration_digest: str) -> None:
        self.configuration_snapshot_id = configuration_snapshot_id
        self.configuration_digest = configuration_digest


class _InMemoryConfigurationRepository:
    def __init__(self, store: dict[str, _InMemoryConfigurationSnapshot]) -> None:
        self._store = store

    def add_if_absent(self, snapshot: object) -> object:
        digest = snapshot.configuration_digest  # type: ignore[attr-defined]
        if digest not in self._store:
            self._store[digest] = _InMemoryConfigurationSnapshot(
                snapshot.configuration_snapshot_id,  # type: ignore[attr-defined]
                digest,
            )
        return self._store[digest]

    def get(self, snapshot_id: str) -> _InMemoryConfigurationSnapshot:
        for snapshot in self._store.values():
            if snapshot.configuration_snapshot_id == snapshot_id:
                return snapshot
        raise NotFound(f"configuration snapshot {snapshot_id!r} not found")


class _InMemoryRunRepository:
    def __init__(self, store: dict[str, object]) -> None:
        self._store = store

    def add(self, manifest: object) -> object:
        self._store[manifest.run_id] = manifest  # type: ignore[attr-defined]
        return manifest

    def get(self, run_id: str) -> object:
        if run_id not in self._store:
            raise NotFound(f"run {run_id!r} not found")
        return self._store[run_id]


class _InMemoryActivityRepository:
    def __init__(self, store: dict[str, ActivityManifest]) -> None:
        self._store = store

    def add_started(self, manifest: ActivityManifest) -> ActivityManifest:
        self._store[manifest.activity_id] = manifest
        return manifest

    def finish_succeeded(self, activity_id, outputs, completed_at) -> ActivityManifest:
        existing = self._store[activity_id]
        updated = existing.model_copy(
            update={"status": "succeeded", "completed_at": completed_at, "outputs": outputs}
        )
        self._store[activity_id] = updated
        return updated

    def finish_failed(self, activity_id, error, completed_at) -> ActivityManifest:
        existing = self._store[activity_id]
        updated = existing.model_copy(
            update={"status": "failed", "completed_at": completed_at, "error": error}
        )
        self._store[activity_id] = updated
        return updated

    def find_succeeded_by_idempotency(self, digest: str) -> ActivityManifest | None:
        for manifest in self._store.values():
            if manifest.idempotency_digest == digest and manifest.status == "succeeded":
                return manifest
        return None

    def producer_of(self, artifact_id: str) -> ActivityManifest | None:
        for manifest in self._store.values():
            for output in manifest.outputs:
                if output.artifact_id == artifact_id:
                    return manifest
        return None

    def consumers_of(self, artifact_id: str) -> tuple[ActivityManifest, ...]:
        return tuple(
            m for m in self._store.values() if any(i.artifact_id == artifact_id for i in m.inputs)
        )


class _InMemoryIssuedForecastRepository:
    def __init__(self, store: dict[UUID, IssuedForecastRecord]) -> None:
        self._store = store

    def add(self, record: IssuedForecastRecord) -> IssuedForecastRecord:
        if record.issued_forecast_id in self._store:
            raise Conflict("issued forecast already exists")
        self._store[record.issued_forecast_id] = record
        return record

    def get(self, issued_forecast_id: UUID) -> IssuedForecastRecord:
        if issued_forecast_id not in self._store:
            raise NotFound(f"issued forecast {issued_forecast_id} not found")
        return self._store[issued_forecast_id]

    def list_for_coordinate(
        self, latitude: float, longitude: float, *, limit: int | None = 100
    ) -> tuple[IssuedForecastRecord, ...]:
        records = (
            row
            for row in self._store.values()
            if row.latitude == latitude and row.longitude == longitude
        )
        return tuple(sorted(records, key=lambda row: row.issued_at, reverse=True)[:limit])


class _InMemoryGovernanceRepository:
    """Python mirror of migration 0005: buffered per unit of work, applied on commit.

    Race, visibility, trigger and lock behavior is proven only against PostgreSQL.
    This double enforces the same append rules (``validate_append``), stamps
    ``recorded_at`` from the factory's injectable monotonic database clock and
    discards writes of a unit of work that never commits.
    """

    def __init__(self, factory: InMemoryUnitOfWorkFactory) -> None:
        self._factory = factory
        self.pending: list[GovernanceEvent] = []

    def _visible(self) -> list[GovernanceEvent]:
        return [*self._factory.governance_events, *self.pending]

    def lock(self, family: str, *, shared: bool, timeout_seconds: float) -> None:
        if family not in FAMILIES:
            raise ValueError(f"unknown governance family {family!r}")
        if self._factory.governance_unavailable:
            raise GovernanceConflict("governance_busy", "simulated governance outage")

    def db_now(self) -> datetime:
        return self._factory.governance_now()

    def append(self, event: GovernanceEvent) -> GovernanceEvent:
        existing = self._visible()
        if any(row.request_key == event.request_key for row in existing):
            raise GovernanceConflict("request_key_conflict")
        if any(
            row.event_type == "RETIRED" == event.event_type
            and row.policy_artifact_id == event.policy_artifact_id
            for row in existing
        ):
            raise GovernanceConflict("already_retired")
        artifacts = self._factory.artifacts
        for identifier in (event.policy_artifact_id, event.evaluation_artifact_id):
            if identifier is None:
                continue
            manifest = artifacts.get(identifier)
            stamped = self._factory.governance_stamp(dry_run=True)
            if manifest is None or (
                max(manifest.registered_at, manifest.availability.available_at) > stamped
            ):
                raise GovernanceConflict("artifact_not_available")
            if (
                identifier == event.policy_artifact_id
                and manifest.content_digest != event.policy_content_digest
            ):
                raise GovernanceConflict("policy_digest_mismatch")
        stamped_event = event.model_copy(update={"recorded_at": self._factory.governance_stamp()})
        validate_append(existing, stamped_event)
        self.pending.append(stamped_event)
        return stamped_event

    def get(self, event_id: str) -> GovernanceEvent:
        for row in self._visible():
            if row.event_id == event_id:
                return row
        raise NotFound(f"governance event {event_id!r} not found")

    def find_by_request_key(self, key: str) -> GovernanceEvent | None:
        return next((row for row in self._visible() if row.request_key == key), None)

    def events(
        self, family: str, scope_keys: tuple[str, ...] | None = None
    ) -> tuple[GovernanceEvent, ...]:
        rows = [
            row
            for row in self._visible()
            if row.family == family and (scope_keys is None or row.scope_key in scope_keys)
        ]
        return tuple(sorted(rows, key=lambda row: (row.scope_key, row.scope_seq)))

    def policy_events(self, policy_artifact_id: str) -> tuple[GovernanceEvent, ...]:
        rows = [row for row in self._visible() if row.policy_artifact_id == policy_artifact_id]
        return tuple(sorted(rows, key=lambda row: row.recorded_at or datetime.min))


class InMemoryUnitOfWork:
    def __init__(self, factory: InMemoryUnitOfWorkFactory) -> None:
        self._factory = factory
        self.grids = _InMemoryGridRepository(factory.grids)
        self.stored_objects = _InMemoryStoredObjectRepository(factory.stored_objects)
        self.artifacts = _InMemoryArtifactRepository(factory.artifacts, factory.stored_objects)
        self.activities = _InMemoryActivityRepository(factory.activities)
        self.configurations = _InMemoryConfigurationRepository(factory.configurations)
        self.runs = _InMemoryRunRepository(factory.runs)
        self.issued_forecasts = _InMemoryIssuedForecastRepository(factory.issued_forecasts)
        self.governance = _InMemoryGovernanceRepository(factory)

    def __enter__(self) -> InMemoryUnitOfWork:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.governance.pending.clear()

    def commit(self) -> None:
        pending, self.governance.pending = self.governance.pending, []
        with self._factory._guard:
            committed = list(self._factory.governance_events)
            for event in pending:
                validate_append(committed, event)
                committed.append(event)
            self._factory.governance_events[:] = committed

    def rollback(self) -> None:
        self.governance.pending.clear()


class InMemoryUnitOfWorkFactory:
    """Shared, process-lifetime backing dicts (simulates a persistent
    database across ``with unit_of_work_factory() as uow`` blocks)."""

    def __init__(self) -> None:
        self.grids: dict[str, tuple[str, dict]] = {}
        self.stored_objects: dict[str, object] = {}
        self.artifacts: dict[str, ArtifactManifest] = {}
        self.activities: dict[str, ActivityManifest] = {}
        self.configurations: dict[str, _InMemoryConfigurationSnapshot] = {}
        self.runs: dict[str, object] = {}
        self.issued_forecasts: dict[UUID, IssuedForecastRecord] = {}
        self.governance_events: list[GovernanceEvent] = []
        self.governance_unavailable = False
        # Injectable database clock; stamps strictly increase like clock_timestamp().
        self._real_clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.governance_clock: Callable[[], datetime] = self._real_clock
        self._governance_last: datetime | None = None
        self._guard = threading.Lock()

    def governance_now(self) -> datetime:
        now = self.governance_clock()
        if self._governance_last is not None and now < self._governance_last:
            return self._governance_last
        return now

    def governance_stamp(self, *, dry_run: bool = False) -> datetime:
        now = self.governance_now()
        if self._governance_last is not None and now <= self._governance_last:
            if self.governance_clock is self._real_clock and not dry_run:
                # A coarse wall clock: wait for it to advance instead of inventing a
                # stamp in the caller's future (PostgreSQL's clock is monotonic).
                deadline = time.monotonic() + 1
                while now <= self._governance_last and time.monotonic() < deadline:
                    time.sleep(0.0005)
                    now = self.governance_clock()
            if now <= self._governance_last:
                now = self._governance_last + timedelta(microseconds=1)
        if not dry_run:
            self._governance_last = now
        return now

    def __call__(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self)
