"""Integration tests for the PostgreSQL session-advisory-lock
IdempotencyLock (plan Section 4.9/4.10, Task 10).

Real PostgreSQL via the pgserver-backed fixture, same pattern as
Task 8's repository tests.
"""

from __future__ import annotations

import threading
import time

import pytest

from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock

pytestmark = pytest.mark.integration


class TestPostgresIdempotencyLock:
    def test_acquire_and_release(self, postgres_dsn: str) -> None:
        lock = PostgresIdempotencyLock(postgres_dsn)
        with lock.acquire("sha256:" + "a" * 64):
            pass  # lock held and released cleanly

    def test_concurrent_identical_digest_serializes(self, postgres_dsn: str) -> None:
        lock = PostgresIdempotencyLock(postgres_dsn)
        digest = "sha256:" + "b" * 64
        order: list[str] = []
        barrier = threading.Barrier(2)

        def _worker(name: str, hold_seconds: float) -> None:
            barrier.wait()
            with lock.acquire(digest):
                order.append(f"{name}-start")
                time.sleep(hold_seconds)
                order.append(f"{name}-end")

        t1 = threading.Thread(target=_worker, args=("first", 0.3))
        t2 = threading.Thread(target=_worker, args=("second", 0.0))
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # the second thread must not start until the first has ended --
        # otherwise the lock did not actually serialize the critical section.
        assert order.index("first-end") < order.index("second-start")

    def test_different_digests_do_not_block_each_other(self, postgres_dsn: str) -> None:
        lock = PostgresIdempotencyLock(postgres_dsn)
        results: list[str] = []

        def _worker(name: str, digest: str) -> None:
            with lock.acquire(digest):
                time.sleep(0.1)
                results.append(name)

        t1 = threading.Thread(target=_worker, args=("a", "sha256:" + "c" * 64))
        t2 = threading.Thread(target=_worker, args=("b", "sha256:" + "d" * 64))
        start = time.monotonic()
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        elapsed = time.monotonic() - start

        assert set(results) == {"a", "b"}
        # if they were serialized (bug), this would take >= 0.2s;
        # concurrent execution should take close to 0.1s.
        assert elapsed < 0.19

    def test_lock_released_on_exception(self, postgres_dsn: str) -> None:
        lock = PostgresIdempotencyLock(postgres_dsn)
        digest = "sha256:" + "e" * 64

        with pytest.raises(ValueError):
            with lock.acquire(digest):
                raise ValueError("boom")

        # lock must be released despite the exception: a second acquire
        # must succeed promptly.
        acquired = threading.Event()

        def _second() -> None:
            with lock.acquire(digest):
                acquired.set()

        t = threading.Thread(target=_second)
        t.start()
        t.join(timeout=2)
        assert acquired.is_set()
