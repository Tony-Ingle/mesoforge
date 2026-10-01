"""Hosted worker roles against real PostgreSQL and S3-compatible storage.

The retained fixture guidance stands in for a refreshed snapshot, so no provider is
contacted: the guidance worker runs in build-only mode, the forecast worker issues from
the baseline it published, and a repeated trigger reuses the issued versions.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from botocore.exceptions import ClientError

from mesoforge.application import operations
from mesoforge.application.baseline_snapshot import current_manifest, read_pointer
from mesoforge.application.forecast_worker import (
    FORECAST_RUN_LOCK,
    ForecastSettings,
    run_forecast,
)
from mesoforge.application.forecast_worker import default_deps as forecast_deps
from mesoforge.application.guidance_worker import GuidanceWorker, WorkerSettings
from mesoforge.application.guidance_worker import default_deps as guidance_deps
from mesoforge.common.identifiers import Digest
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.postgres.schema import (
    RUNTIME_ROLE,
    alembic_config,
    expected_head,
    schema_status,
)
from mesoforge.storage.s3 import S3ArtifactObjectStore, S3StoredObject
from tests.integration.application import test_batch_issuance as storage_tests
from tests.unit.application import test_baseline_snapshot as baseline_tests
from tests.unit.application.test_batch_forecast import FIRST, LAST
from tests.unit.application.test_prepared_temperature import TARGET

pytestmark = pytest.mark.integration
baseline_case = baseline_tests.baseline_case
migrated_dsn = storage_tests.migrated_dsn
object_store = storage_tests.object_store
configured_retrieval_storage = storage_tests.configured_retrieval_storage
REPO = Path(__file__).resolve().parents[3]
LOCATIONS = [
    {**FIRST, "id": "first", "name": "First", "display_timezone": "America/Chicago"},
    {**LAST, "id": "last", "name": "Last", "display_timezone": "America/Chicago"},
]


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("A build-only worker attempted provider discovery or refresh")


def run_guidance_once(root: Path, config: Path) -> tuple[int, dict[str, Any]]:
    settings = WorkerSettings(root=root, config=config, refresh="off")
    deps = replace(guidance_deps(settings), discover=forbidden, refresh=forbidden)
    worker = GuidanceWorker(settings, deps, stream=io.StringIO())
    code = worker.run(once=True, install_signals=False)
    return code, worker.state


@pytest.fixture
def runtime(baseline_case, configured_retrieval_storage: None, tmp_path: Path) -> dict:
    """The fixture's retained guidance root is the worker's guidance directory."""
    root = Path(baseline_case["guidance"]).parent
    config = tmp_path / "locations.json"
    config.write_text(json.dumps({"locations": LOCATIONS}), encoding="utf-8")
    return {"root": root, "config": config}


def test_guidance_worker_publishes_governed_baseline_and_recovers_it_after_restart(
    runtime: dict, migrated_dsn: str
) -> None:
    root, config = runtime["root"], runtime["config"]
    code, state = run_guidance_once(root, config)
    last = state["polls"]["last"]
    assert code == 0, last
    assert last["refresh"]["blocked_by"] == "refresh_disabled"
    assert last["build"]["decision"] in {"build", "current"}
    if last["build"]["decision"] == "build":
        assert last["build"]["outcome"] == "published", last["build"]
    manifest = current_manifest(root / "baseline")
    assert manifest is not None
    assert manifest["blend_governance"]["status"] == "resolved"
    assert manifest["prepared_snapshot"]["snapshot_id"] == (baseline_case_snapshot_id(root))
    pointer = read_pointer(root / "baseline")
    # A new process sees the same durable baseline and does not rebuild it.
    code, restarted = run_guidance_once(root, config)
    assert code == 0
    assert restarted["polls"]["last"]["build"]["decision"] == "current"
    assert read_pointer(root / "baseline") == pointer
    assert restarted["polls"]["count"] == state["polls"]["count"] + 1


def baseline_case_snapshot_id(root: Path) -> str:
    latest = json.loads((root / "guidance" / "latest_complete.json").read_text("utf-8"))
    return str(latest["snapshot_id"])


def test_forecast_worker_issues_pinned_baseline_and_duplicate_triggers_skip(
    runtime: dict,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
) -> None:
    root, config = runtime["root"], runtime["config"]
    assert run_guidance_once(root, config)[0] == 0
    settings = ForecastSettings(root=root, config=config, reference_time=TARGET, verify_prior=False)
    code, first = run_forecast(settings, forecast_deps(), stream=io.StringIO())
    assert (code, first["status"]) == (0, "completed"), first
    assert first["summary"] == {"ok": 2, "issued": 2, "skipped": 0, "failed": 0}
    pinned = read_pointer(root / "baseline")
    assert pinned is not None and first["baseline"] == pinned["baseline_snapshot_id"]
    for row in first["results"]:
        assert row["issued_forecast_id"]
        # The desk is always attempted; without credentials it falls back explicitly.
        assert row["ai_desk"]["issued_stage"] == "deterministic_corrected"
        assert row["ai_desk"]["completion_reason"]
    assert Path(first["record"]).is_file()
    before = storage_tests.storage_inventory(migrated_dsn, object_store)
    code, second = run_forecast(settings, forecast_deps(), stream=io.StringIO())
    assert (code, second["status"]) == (0, "completed")
    assert second["summary"] == {"ok": 0, "issued": 0, "skipped": 2, "failed": 0}
    assert [row["existing_issued_forecast_ids"] for row in second["results"]] == [
        [row["issued_forecast_id"]] for row in first["results"]
    ]
    assert storage_tests.storage_inventory(migrated_dsn, object_store) == before
    with PostgresIdempotencyLock(migrated_dsn).try_acquire(FORECAST_RUN_LOCK):
        code, overlapping = run_forecast(settings, forecast_deps(), stream=io.StringIO())
    assert (code, overlapping["status"]) == (0, "already_running")
    assert storage_tests.storage_inventory(migrated_dsn, object_store) == before


def test_forecast_worker_fails_clearly_for_missing_bucket(
    runtime: dict, migrated_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MESOFORGE_S3_BUCKET", "mesoforge-missing-bucket-never-created")
    settings = ForecastSettings(
        root=runtime["root"], config=runtime["config"], reference_time=TARGET, verify_prior=False
    )
    code, record = run_forecast(settings, forecast_deps(), stream=io.StringIO())
    assert (code, record["category"]) == (2, "storage_unavailable")
    probe = S3ArtifactObjectStore(
        bucket="mesoforge-missing-bucket-never-created",
        endpoint_url=os.environ["MESOFORGE_S3_ENDPOINT"],
        access_key=os.environ["MESOFORGE_S3_ACCESS_KEY"],
        secret_key=os.environ["MESOFORGE_S3_SECRET_KEY"],
        ensure_bucket=False,
    )
    with pytest.raises(ClientError):
        probe.check_bucket()  # the worker did not create it


def test_forecast_run_lock_excludes_an_independent_process_then_releases(
    postgres_dsn: str,
) -> None:
    """The hosted run key is shared by processes, not just connections in one process."""
    script = """
import json
import os
from mesoforge.application.forecast_worker import FORECAST_RUN_LOCK
from mesoforge.storage.postgres.idempotency_lock import AdvisoryLockBusy, PostgresIdempotencyLock

lock = PostgresIdempotencyLock(os.environ["MESOFORGE_TEST_LOCK_DSN"])
try:
    with lock.try_acquire(FORECAST_RUN_LOCK):
        status = "acquired"
except AdvisoryLockBusy:
    status = "busy"
print(json.dumps({"status": status, "pid": os.getpid()}))
"""

    def probe() -> str:
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO,
            env={
                **os.environ,
                "MESOFORGE_TEST_LOCK_DSN": postgres_dsn,
                "PYTHONPATH": str(REPO / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        outcome = json.loads(result.stdout)
        assert outcome["pid"] != os.getpid()
        return str(outcome["status"])

    with PostgresIdempotencyLock(postgres_dsn).try_acquire(FORECAST_RUN_LOCK):
        assert probe() == "busy"
    assert probe() == "acquired"


def test_migration_is_an_explicit_operator_step(
    clean_postgres_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MESOFORGE_DATABASE_DSN", clean_postgres_dsn)
    monkeypatch.delenv("MESOFORGE_ALEMBIC_DSN", raising=False)
    assert schema_status(clean_postgres_dsn) == {
        "current": None,
        "head": expected_head(),
        "at_head": False,
    }
    config = tmp_path / "locations.json"
    config.write_text(json.dumps({"locations": LOCATIONS}), encoding="utf-8")
    settings = ForecastSettings(root=tmp_path / "runtime", config=config)
    code, record = run_forecast(settings, forecast_deps(), stream=io.StringIO())
    assert (code, record["category"]) == (2, "schema_not_at_head")
    assert schema_status(clean_postgres_dsn)["current"] is None  # the worker never migrated
    command.upgrade(alembic_config(), "0004_issued_forecasts")
    database = operations.database_target(clean_postgres_dsn)["database"]
    with pytest.raises(RuntimeError):
        operations.migrate("some_other_database")
    result = operations.migrate(database)
    assert result["before"]["current"] == "0004_issued_forecasts"
    assert result["after"] == {"current": expected_head(), "head": expected_head(), "at_head": True}
    assert "password" not in json.dumps(result).lower()


def test_object_export_import_round_trip(
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    tmp_path: Path,
) -> None:
    payloads = [b'{"object": 1}', b'{"object": 2}']
    stored: list[S3StoredObject] = []
    with PostgresUnitOfWork(migrated_dsn) as uow:
        for payload in payloads:
            saved = object_store.put_if_absent(
                Digest.of_bytes(payload), payload, "application/json"
            )
            uow.stored_objects.add_if_absent(saved)
            stored.append(saved)
        uow.commit()
    exported = operations.export_objects(tmp_path / "backup")
    assert exported["objects"] == 2 and exported["copied"] == 2
    client = object_store._client
    for saved in stored:
        key = saved.storage_uri.split("/", 3)[3]
        client.delete_object(Bucket=object_store._bucket, Key=key)
        assert not object_store.exists_verified(saved.storage_uri, saved.content_digest)
    assert operations.import_objects(tmp_path / "backup")["objects"] == 2
    for saved, payload in zip(stored, payloads, strict=True):
        assert object_store.get_verified(saved.storage_uri, saved.content_digest) == payload


def _psql() -> Path | None:
    """psql from the pgserver development dependency (present locally and in CI)."""
    try:
        import pgserver
    except ImportError:
        return None
    binary = Path(pgserver.__file__).parent / "pginstall" / "bin"
    for name in ("psql.exe", "psql"):
        if (binary / name).is_file():
            return binary / name
    return None


def test_least_privilege_worker_role_cannot_change_schema_governance_or_delete(
    clean_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committed psql script (run by real psql) plus `operations migrate` grants."""
    psql = _psql()
    if psql is None:
        pytest.skip("psql is not available")
    url = sa.engine.make_url(clean_postgres_dsn)
    database = url.database
    assert database
    worker = "mesoforge_worker_test"
    password = uuid4().hex
    admin = psycopg.connect(clean_postgres_dsn.replace("+psycopg", ""), autocommit=True)

    def drop_roles() -> None:
        for role in (worker, RUNTIME_ROLE):
            if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                admin.execute(f'DROP OWNED BY "{role}"')
                admin.execute(f'DROP ROLE "{role}"')

    try:
        drop_roles()
        conninfo = url.set(drivername="postgresql").render_as_string(hide_password=False)
        created = subprocess.run(
            [
                str(psql),
                "-d",
                conninfo,
                "-v",
                f"worker={worker}",
                "-v",
                f"worker_password={password}",
                "-v",
                f"database={database}",
                "-f",
                str(REPO / "deploy/hosted/postgres-init/worker-role.psql"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert created.returncode == 0, created.stderr
        monkeypatch.setenv("MESOFORGE_DATABASE_DSN", clean_postgres_dsn)
        monkeypatch.delenv("MESOFORGE_ALEMBIC_DSN", raising=False)
        result = operations.migrate(database)
        assert result["runtime_grants"] == "applied"
        worker_dsn = url.set(username=worker, password=password).render_as_string(
            hide_password=False
        )
        assert schema_status(worker_dsn)["at_head"] is True
        with psycopg.connect(worker_dsn.replace("+psycopg", ""), autocommit=True) as session:
            session.execute("SELECT count(*) FROM governance_events").fetchone()
            session.execute(
                "INSERT INTO stored_objects (content_digest, storage_uri, media_type, byte_size)"
                " VALUES ('sha256:x', 's3://b/x', 'application/json', 1)"
            )
            allowed = session.execute(
                "SELECT has_column_privilege('activities', 'status', 'UPDATE'),"
                " has_table_privilege('artifacts', 'UPDATE'),"
                " has_table_privilege('governance_events', 'INSERT')"
            ).fetchone()
            assert allowed == (True, False, False)
            for statement in (
                "INSERT INTO governance_events (id) VALUES (gen_random_uuid())",
                "UPDATE governance_events SET reason = 'worker rewrite'",
                "DELETE FROM governance_events",
                "TRUNCATE governance_events",
                "INSERT INTO alembic_version (version_num) VALUES ('forged')",
                "UPDATE alembic_version SET version_num = 'forged'",
                "DELETE FROM alembic_version",
                "UPDATE artifacts SET attributes = '{}'::jsonb",
                "UPDATE stored_objects SET byte_size = 2",
                "DELETE FROM stored_objects",
                "TRUNCATE stored_objects",
                "CREATE TABLE forbidden (id int)",
                "DROP TABLE issued_forecasts",
                "ALTER TABLE governance_events DISABLE TRIGGER ALL",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    session.execute(statement)
    finally:
        drop_roles()
        admin.execute(f'GRANT CONNECT, TEMPORARY ON DATABASE "{database}" TO PUBLIC')
        admin.close()
