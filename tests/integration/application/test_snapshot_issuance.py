"""Separate-process snapshot issuance against the existing PostgreSQL/MinIO path."""

from __future__ import annotations

import multiprocessing
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest

from mesoforge.application import forecast_from_snapshot as fast
from mesoforge.application import forward_run
from mesoforge.application.issuance import acquire_issuance_run_lock
from mesoforge.storage.postgres.idempotency_lock import AdvisoryLockBusy
from tests.integration.application import test_batch_issuance as storage_tests
from tests.unit.application.test_batch_forecast import write_config
from tests.unit.application.test_snapshot_issuance import (
    LOCATIONS,
    REQUEST,
    install_snapshot,
)

pytestmark = pytest.mark.integration
migrated_dsn = storage_tests.migrated_dsn
object_store = storage_tests.object_store
configured_retrieval_storage = storage_tests.configured_retrieval_storage


def _issue_worker(root: str, first: bool, attempted: Any, looked_up: Any, release: Any, queue: Any):
    """Hold the first empty lookup while the other process attempts the same lock."""
    from mesoforge.application.issuance import _configured_reader

    try:
        with pytest.MonkeyPatch.context() as patch:
            install_snapshot(patch, Path(root))
            service = _configured_reader()
            lookup = service.find_versions

            def find_versions(**coords):
                existing = lookup(**coords)
                looked_up.set()
                if first and not release.wait(30):
                    raise TimeoutError("Test did not release the first metadata lookup")
                return existing

            @contextmanager
            def lock():
                attempted.set()
                with acquire_issuance_run_lock(wait=True):
                    yield

            patch.setattr(service, "find_versions", find_versions)
            result = fast.forecast_from_snapshot(
                Path(root),
                LOCATIONS[:1],
                request_time=REQUEST,
                issue=True,
                issuer=service,
                run_lock=lock,
            )
            queue.put({"result": result})
    except BaseException as exc:
        queue.put({"error": f"{type(exc).__name__}: {exc}"})


def test_concurrent_snapshot_issuance_has_one_primary_and_explicit_reissue(
    tmp_path: Path,
    migrated_dsn: str,
    object_store: Any,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mesoforge.application.issuance import _configured_reader

    context = multiprocessing.get_context("spawn")
    first_attempted, second_attempted = context.Event(), context.Event()
    first_lookup, second_lookup = context.Event(), context.Event()
    release = context.Event()
    queue = context.Queue()
    children = [
        context.Process(
            target=_issue_worker,
            args=(str(tmp_path / "first"), True, first_attempted, first_lookup, release, queue),
        ),
        context.Process(
            target=_issue_worker,
            args=(str(tmp_path / "second"), False, second_attempted, second_lookup, release, queue),
        ),
    ]
    try:
        children[0].start()
        assert first_lookup.wait(30), "First process did not reach the protected lookup"
        # The old forward command uses this same lock and cannot issue concurrently.
        with pytest.raises(AdvisoryLockBusy):
            forward_run.run_forward(
                write_config(tmp_path, LOCATIONS[:1]), tmp_path / "forward-overlap"
            )
        assert not (tmp_path / "forward-overlap").exists()
        children[1].start()
        assert second_attempted.wait(30)
        # Observe a real waiting PostgreSQL advisory lock, not a timing-based sleep.
        deadline = time.monotonic() + 15
        with psycopg.connect(migrated_dsn.replace("+psycopg", ""), autocommit=True) as connection:
            while time.monotonic() < deadline:
                waiting = connection.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                ).fetchone()[0]
                if waiting:
                    break
                time.sleep(0.02)
            else:
                pytest.fail("Second process did not wait on the existing PostgreSQL lock")
        assert not second_lookup.is_set(), "Second lookup escaped compare-and-issue serialization"
        release.set()
        outcomes = [queue.get(timeout=30), queue.get(timeout=30)]
        assert all("result" in outcome for outcome in outcomes), outcomes
        results = [outcome["result"] for outcome in outcomes]
        assert sorted(result["results"][0]["status"] for result in results) == [
            "ok",
            "skipped_already_issued",
        ]
        successful = next(result["results"][0] for result in results if result["summary"]["issued"])
        skipped = next(result["results"][0] for result in results if result["summary"]["skipped"])
        primary_id = UUID(successful["issued"]["issued_forecast_id"])
        assert skipped["skipped"]["existing_issued_forecast_ids"] == [str(primary_id)]
        service = _configured_reader()
        primary = service.read(primary_id)
        assert primary["forecast"] == successful["forecast"]
        assert primary["forecast"]["prepared_snapshot"]["issuance_mode"] == "primary"
        inventory = storage_tests.storage_inventory(migrated_dsn, object_store)
        assert [len(rows) for rows in inventory] == [1, 1, 1]

        install_snapshot(monkeypatch, tmp_path / "explicit-reissue")
        repeated = fast.forecast_from_snapshot(
            tmp_path,
            LOCATIONS[:1],
            request_time=REQUEST,
            issue=True,
            issuer=service,
            reissue=True,
        )
        assert repeated["summary"]["issued"] == 1
        reissue = repeated["results"][0]
        assert reissue["issued"]["issued_forecast_id"] != str(primary_id)
        assert reissue["forecast"]["prepared_snapshot"]["issuance_mode"] == "explicit_reissue"
        assert service.read(primary_id) == primary
        assert [
            len(rows) for rows in storage_tests.storage_inventory(migrated_dsn, object_store)
        ] == [2, 2, 2]
    finally:
        release.set()
        for child in children:
            if child.pid is None:
                continue
            child.join(timeout=15)
            if child.is_alive():
                child.terminate()
                child.join(timeout=10)
            assert child.exitcode == 0
