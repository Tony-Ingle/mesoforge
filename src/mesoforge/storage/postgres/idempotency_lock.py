"""PostgreSQL session-advisory-lock IdempotencyLock (plan Section 4.9,
Section 4.8's "concurrent loser rechecks after waiting" requirement).

Maps the first signed 64 bits of the digest to ``pg_advisory_lock``,
holds a dedicated connection (not an open SQL transaction, so the lock
survives across the multi-step transformation attempt) for the duration
of the ``with`` block, and always calls ``pg_advisory_unlock`` in
``finally``. Connection loss also releases the lock (PostgreSQL's
session-level advisory locks are tied to the connection's lifetime).

``acquire`` uses the typed ``Digest`` class, not an unrestricted ``str``
(Codex review t_f569c45c finding 3; final re-review HIGH finding
6/t_1ecb8414: the public idempotency-lock digest boundary must be
typed). A caller-supplied digest is re-constructed against ``Digest``
immediately, so a malformed value fails closed with
``InvalidIdentifier`` before any lock key is derived.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from mesoforge.common.errors import Conflict
from mesoforge.common.identifiers import Digest


class AdvisoryLockBusy(Conflict):
    """Another session holds the requested advisory lock; nothing was waited for."""


def _digest_to_signed_bigint(digest: Digest) -> int:
    """First signed 64 bits of a SHA-256 hex digest string, used as the
    PostgreSQL advisory lock key. Deterministic and collision-resistant
    enough for lock partitioning (a false-positive lock collision only
    costs extra serialization, never incorrect behavior, because the
    caller always rechecks succeeded-state after acquiring)."""
    raw_hash = hashlib.sha256(str(digest).encode("utf-8")).digest()
    (signed_value,) = struct.unpack(">q", raw_hash[:8])
    return int(signed_value)


class PostgresIdempotencyLock:
    def __init__(self, dsn: str) -> None:
        # psycopg accepts SQLAlchemy-style "postgresql+psycopg://" DSNs
        # from the rest of this codebase; strip the "+psycopg" driver
        # qualifier that psycopg itself does not understand.
        self._dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    @contextmanager
    def acquire(self, digest: Digest) -> Iterator[None]:
        digest = Digest(digest)
        lock_key = _digest_to_signed_bigint(digest)
        connection = psycopg.connect(self._dsn, autocommit=True)
        try:
            connection.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
            try:
                yield
            finally:
                connection.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
        finally:
            connection.close()

    @contextmanager
    def try_acquire(self, digest: Digest) -> Iterator[None]:
        """Hold the lock for the block, or raise ``AdvisoryLockBusy`` immediately.

        Non-blocking so a repeated caller (an external scheduler) detects an
        overlapping run instead of queueing behind it. Connection loss releases
        the lock exactly as for ``acquire``.
        """
        digest = Digest(digest)
        lock_key = _digest_to_signed_bigint(digest)
        connection = psycopg.connect(self._dsn, autocommit=True)
        try:
            row = connection.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,)).fetchone()
            if row is None or row[0] is not True:
                raise AdvisoryLockBusy(f"advisory lock {digest} is held by another session")
            try:
                yield
            finally:
                connection.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
        finally:
            connection.close()
