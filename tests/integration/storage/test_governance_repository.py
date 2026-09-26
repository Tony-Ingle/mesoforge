"""PostgreSQL proof of governance invariants, CAS races and the visibility handshake.

The in-memory double mirrors these rules; only this module proves the migration 0005
trigger, the transaction-scoped advisory locks, database clock stamping and
concurrent compare-and-set behavior against a real server.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from mesoforge.common.identifiers import ArtifactId, Digest, GovernanceEventId
from mesoforge.contracts.artifacts import ArtifactManifest, Availability
from mesoforge.contracts.policy_governance import (
    AI_DESK_POLICY,
    DESK_SCOPE,
    GOVERNANCE_POLICY_VERSION,
    TEMPERATURE_CORRECTION,
    GovernanceConflict,
    GovernanceEvent,
    correction_scope,
    governance_policy_digest,
    head_at,
    request_digest,
    request_key,
)
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from tests.support.governance import FIXTURE_REVISION, append_event

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
SCOPE = correction_scope(44.98861, -93.25553)
FAMILY = TEMPERATURE_CORRECTION


@pytest.fixture()
def migrated_dsn(clean_postgres_dsn: str) -> str:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    os.environ["MESOFORGE_ALEMBIC_DSN"] = clean_postgres_dsn
    command.upgrade(config, "head")
    return clean_postgres_dsn


class _Snapshot:
    def __init__(self) -> None:
        self.configuration_snapshot_id = "cfg_sha256_" + "a" * 64
        self.configuration_digest = "sha256:" + "a" * 64
        self.canonical_json: dict[str, object] = {}
        self.source_references: tuple[dict[str, object], ...] = ()


class _Stored:
    def __init__(self, digest: str) -> None:
        self.content_digest = digest
        self.storage_uri = f"s3://bucket/objects/{digest[7:]}"
        self.media_type = "application/json"
        self.byte_size = 10


def _artifacts(dsn: str, count: int, *, available_at: datetime | None = None) -> list[dict]:
    rows = []
    with PostgresUnitOfWork(dsn) as uow:
        uow.configurations.add_if_absent(_Snapshot())
        for index in range(count):
            digest = "sha256:" + f"{index:x}".rjust(64, "c")
            uow.stored_objects.add_if_absent(_Stored(digest))
            manifest = ArtifactManifest(
                artifact_id=f"art_{uuid.uuid4()}",
                artifact_type="learning-policy",
                artifact_schema_version="mesoforge.temperature-correction-policy.v1",
                media_type="application/json",
                byte_size=10,
                content_digest=digest,
                storage_uri=f"s3://bucket/objects/{digest[7:]}",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                registered_at=datetime(2026, 1, 1, tzinfo=UTC),
                availability=Availability(
                    available_at=available_at or datetime(2026, 1, 1, tzinfo=UTC),
                    authority="mesoforge.test",
                    method="fixture.v1",
                ),
                configuration_snapshot_id="cfg_sha256_" + "a" * 64,
                configuration_digest="sha256:" + "a" * 64,
                code_revision="a" * 40,
                environment_digest="sha256:" + "b" * 64,
                quality_state="valid",
            )
            uow.artifacts.add(manifest)
            rows.append({"artifact_id": manifest.artifact_id, "content_digest": digest})
        uow.commit()
    return rows


def _factory(dsn: str):
    return lambda: PostgresUnitOfWork(dsn)


def _register(dsn: str, policy: dict) -> None:
    append_event(
        _factory(dsn),
        "REGISTERED",
        family=FAMILY,
        scope_key=SCOPE,
        policy=policy["artifact_id"],
        content_digest=policy["content_digest"],
    )


def _activate(dsn: str, policy: dict, evaluation: dict):
    return append_event(
        _factory(dsn),
        "ACTIVATED",
        family=FAMILY,
        scope_key=SCOPE,
        policy=policy["artifact_id"],
        content_digest=policy["content_digest"],
        evaluation=evaluation["artifact_id"],
    )


def _events(dsn: str):
    with PostgresUnitOfWork(dsn) as uow:
        return uow.governance.events(FAMILY, (SCOPE,))


def test_trigger_stamps_database_time_and_enforces_lifecycle_invariants(migrated_dsn: str) -> None:
    a, b, evaluation, c = _artifacts(migrated_dsn, 4)
    before = datetime.now(UTC) - timedelta(minutes=5)
    _register(migrated_dsn, a)
    registered = _events(migrated_dsn)[0]
    assert registered.recorded_at is not None and registered.recorded_at > before
    with pytest.raises(GovernanceConflict, match="candidate_not_registered"):
        _activate(migrated_dsn, b, evaluation)
    with pytest.raises(GovernanceConflict, match="policy_digest_mismatch"):
        append_event(
            _factory(migrated_dsn),
            "REGISTERED",
            family=FAMILY,
            scope_key=SCOPE,
            policy=b["artifact_id"],
            content_digest=a["content_digest"],
        )
    first = _activate(migrated_dsn, a, evaluation)
    with pytest.raises(GovernanceConflict, match="retire_active_policy"):
        append_event(
            _factory(migrated_dsn),
            "RETIRED",
            family=FAMILY,
            scope_key=SCOPE,
            policy=a["artifact_id"],
            content_digest=a["content_digest"],
        )
    append_event(
        _factory(migrated_dsn),
        "REGISTERED",
        family=AI_DESK_POLICY,
        scope_key=DESK_SCOPE,
        policy=c["artifact_id"],
        content_digest=c["content_digest"],
    )
    engine = sa.create_engine(migrated_dsn)
    try:
        # The database refuses AI desk chain events even from a writer that skips the
        # contract model: the AI can never be activated.
        with pytest.raises(sa.exc.IntegrityError, match="ck_governance_events_ai_desk"):
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO governance_events SELECT (md5(random()::text))::uuid, "
                        "schema_version, family, scope_key, scope_seq + 1, 'ACTIVATED', "
                        "policy_artifact_id, policy_content_digest, NULL, NULL, :evaluation, "
                        "NULL, NULL, governance_policy_version, governance_policy_digest, actor, "
                        "reason, recorded_at, code_revision, environment_digest, 'sha256:x', "
                        "request_digest, NULL FROM governance_events WHERE family = :family"
                    ),
                    {
                        "evaluation": uuid.UUID(evaluation["artifact_id"][4:]),
                        "family": AI_DESK_POLICY,
                    },
                )
        for statement in (
            "UPDATE governance_events SET reason = 'edited'",
            "DELETE FROM governance_events",
            "TRUNCATE governance_events",
        ):
            with pytest.raises(sa.exc.DBAPIError, match="immutable"), engine.begin() as conn:
                conn.execute(sa.text(statement))
    finally:
        engine.dispose()
    events = _events(migrated_dsn)
    assert head_at(events).event_id == first.event_id
    assert [row.scope_seq for row in events] == list(range(1, len(events) + 1))


def test_stale_predecessor_gap_and_future_artifact_are_refused(migrated_dsn: str) -> None:
    a, evaluation = _artifacts(migrated_dsn, 2)
    (future,) = _artifacts(migrated_dsn, 1, available_at=datetime.now(UTC) + timedelta(days=1))
    _register(migrated_dsn, a)
    with pytest.raises(GovernanceConflict, match="artifact_not_available"):
        _register(migrated_dsn, future)
    first = _activate(migrated_dsn, a, evaluation)
    with PostgresUnitOfWork(migrated_dsn) as uow:
        stale = first.model_copy(
            update={
                "event_id": GovernanceEventId.generate(),
                "scope_seq": first.scope_seq + 1,
                "previous_head_event_id": None,
                "request_key": Digest.of_bytes(b"stale"),
            }
        )
        with pytest.raises(GovernanceConflict, match="stale_head"):
            uow.governance.append(stale)
    with PostgresUnitOfWork(migrated_dsn) as uow:
        gap = first.model_copy(
            update={
                "event_id": GovernanceEventId.generate(),
                "scope_seq": first.scope_seq + 5,
                "request_key": Digest.of_bytes(b"gap"),
            }
        )
        with pytest.raises(GovernanceConflict, match="scope_seq_conflict"):
            uow.governance.append(gap)
    with PostgresUnitOfWork(migrated_dsn) as uow:
        replay = first.model_copy(
            update={"event_id": GovernanceEventId.generate(), "scope_seq": first.scope_seq + 1}
        )
        with pytest.raises(GovernanceConflict, match="request_key_conflict|stale_head"):
            uow.governance.append(replay)
    assert len(_events(migrated_dsn)) == 2


def _race(dsn: str, builders) -> list[object]:
    barrier = threading.Barrier(len(builders))
    outcomes: list[object] = [None] * len(builders)

    def run(index: int) -> None:
        event = builders[index]
        with PostgresUnitOfWork(dsn) as uow:
            barrier.wait()
            try:
                stored = uow.governance.append(event)
                uow.commit()
                outcomes[index] = stored
            except GovernanceConflict as exc:
                outcomes[index] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(builders))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return outcomes


def _prepared(dsn: str, event_type: str, policy: dict, evaluation: dict | None, tag: str):
    """An event built against the current head, appended later by a racing writer."""
    events = _events(dsn)
    head = head_at(events)
    key = request_key(race=tag)
    return GovernanceEvent(
        event_id=GovernanceEventId.generate(),
        family=FAMILY,
        scope_key=SCOPE,
        scope_seq=len(events) + 1,
        event_type=event_type,
        policy_artifact_id=ArtifactId(policy["artifact_id"]),
        policy_content_digest=Digest(policy["content_digest"]),
        previous_head_event_id=head.event_id if event_type == "ACTIVATED" and head else None,
        evaluation_artifact_id=ArtifactId(evaluation["artifact_id"]) if evaluation else None,
        governance_policy_version=GOVERNANCE_POLICY_VERSION,
        governance_policy_digest=governance_policy_digest(),
        actor="race",
        reason="race",
        code_revision=FIXTURE_REVISION,
        environment_digest=Digest.of_bytes(b"env"),
        request_key=key,
        request_digest=request_digest(key, actor="race", reason="race"),
    )


def test_concurrent_first_activation_and_retire_versus_activate(migrated_dsn: str) -> None:
    a, b, evaluation = _artifacts(migrated_dsn, 3)
    _register(migrated_dsn, a)
    _register(migrated_dsn, b)
    outcomes = _race(
        migrated_dsn,
        [
            _prepared(migrated_dsn, "ACTIVATED", a, evaluation, "first"),
            _prepared(migrated_dsn, "ACTIVATED", b, evaluation, "second"),
        ],
    )
    winners = [row for row in outcomes if not isinstance(row, Exception)]
    losers = [row for row in outcomes if isinstance(row, GovernanceConflict)]
    assert len(winners) == 1 and len(losers) == 1
    assert losers[0].code in {"scope_seq_conflict", "stale_head"}
    events = _events(migrated_dsn)
    assert [row.event_type for row in events].count("ACTIVATED") == 1
    active = head_at(events).policy_artifact_id
    other = b if str(active) == a["artifact_id"] else a
    outcomes = _race(
        migrated_dsn,
        [
            _prepared(migrated_dsn, "RETIRED", other, None, "retire"),
            _prepared(migrated_dsn, "ACTIVATED", other, evaluation, "activate"),
        ],
    )
    assert sum(isinstance(row, GovernanceConflict) for row in outcomes) == 1
    events = _events(migrated_dsn)
    retired = {str(row.policy_artifact_id) for row in events if row.event_type == "RETIRED"}
    assert str(head_at(events).policy_artifact_id) not in retired


def test_readers_wait_for_committing_writers_and_never_miss_recorded_events(
    migrated_dsn: str,
) -> None:
    a, evaluation = _artifacts(migrated_dsn, 2)
    _register(migrated_dsn, a)
    appended = threading.Event()
    stamped: dict[str, object] = {}

    def writer() -> None:
        with PostgresUnitOfWork(migrated_dsn) as uow:
            uow.governance.lock(FAMILY, shared=False, timeout_seconds=10)
            event = _prepared(migrated_dsn, "ACTIVATED", a, evaluation, "visibility")
            stamped["event"] = uow.governance.append(event)
            appended.set()
            time.sleep(2)
            uow.commit()

    thread = threading.Thread(target=writer)
    thread.start()
    assert appended.wait(30)
    recorded = stamped["event"].recorded_at
    started = time.perf_counter()
    with PostgresUnitOfWork(migrated_dsn) as uow:
        uow.governance.lock(FAMILY, shared=True, timeout_seconds=10)
        waited = time.perf_counter() - started
        now = uow.governance.db_now()
        events = uow.governance.events(FAMILY, (SCOPE,))
    thread.join(30)
    # The reader blocked until commit, and every event recorded at or before its
    # decision time is visible: a decision at T >= recorded_at resolves the new head.
    assert waited > 1.0 and now >= recorded
    assert head_at(events, recorded).event_id == stamped["event"].event_id
    assert head_at(events, recorded - timedelta(microseconds=1)) is None
    with PostgresUnitOfWork(migrated_dsn) as holder:
        holder.governance.lock(FAMILY, shared=False, timeout_seconds=10)
        with PostgresUnitOfWork(migrated_dsn) as reader, pytest.raises(GovernanceConflict) as busy:
            reader.governance.lock(FAMILY, shared=True, timeout_seconds=0.5)
        assert busy.value.code == "governance_busy"


def test_clock_regression_is_refused_without_partial_rows(migrated_dsn: str) -> None:
    a, b = _artifacts(migrated_dsn, 2)
    _register(migrated_dsn, a)
    engine = sa.create_engine(migrated_dsn)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "ALTER TABLE governance_events DISABLE TRIGGER governance_events_before_insert"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO governance_events SELECT (md5(random()::text))::uuid, "
                    "schema_version, family, scope_key, scope_seq + 1, 'RETIRED', "
                    "policy_artifact_id, policy_content_digest, NULL, NULL, NULL, NULL, NULL, "
                    "governance_policy_version, governance_policy_digest, actor, reason, "
                    "recorded_at + interval '1 day', code_revision, environment_digest, "
                    "'sha256:' || md5(random()::text) || md5(random()::text), request_digest, "
                    "NULL FROM governance_events"
                )
            )
            conn.execute(
                sa.text(
                    "ALTER TABLE governance_events ENABLE TRIGGER governance_events_before_insert"
                )
            )
    finally:
        engine.dispose()
    with pytest.raises(GovernanceConflict, match="clock_regression"):
        _register(migrated_dsn, b)
    assert len(_events(migrated_dsn)) == 2


def test_downgrade_drops_governance_functions(clean_postgres_dsn: str) -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    os.environ["MESOFORGE_ALEMBIC_DSN"] = clean_postgres_dsn
    command.upgrade(config, "head")
    command.downgrade(config, "0004_issued_forecasts")
    engine = sa.create_engine(clean_postgres_dsn)
    try:
        with engine.connect() as conn:
            names = set(
                conn.execute(
                    sa.text(
                        "SELECT proname FROM pg_proc WHERE proname IN ('governance_lock_key', "
                        "'governance_event_before_insert', 'reject_governance_event_mutation')"
                    )
                ).scalars()
            )
        assert names == set()
        assert "governance_events" not in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
    command.upgrade(config, "head")
