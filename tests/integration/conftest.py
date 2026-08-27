"""Real, ephemeral PostgreSQL fixture for integration tests, backed by
the ``pgserver`` pip package (a self-contained, non-root, no-Docker
PostgreSQL binary distribution). This is a genuine PostgreSQL 16+
instance -- not SQLite and not a mock -- satisfying the plan's
requirement that integration/acceptance tests run against real
PostgreSQL, while remaining runnable in environments without Docker
access (this development sandbox has no docker group membership).

CI (``.github/workflows/ci.yml``) instead uses a real ``postgres:16``
service container; either path exercises the identical Alembic
migrations and SQLAlchemy models against a real PostgreSQL server.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pgserver
import pytest


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """Start a session-scoped ephemeral PostgreSQL instance and yield a
    psycopg3 DSN. Torn down at the end of the test session."""
    data_dir = Path(tempfile.mkdtemp(prefix="mesoforge-pgserver-"))
    server = pgserver.get_server(data_dir, cleanup_mode="stop")
    try:
        raw_uri = server.get_uri()
        # pgserver returns a postgresql:// URI usable with psycopg2/psql;
        # SQLAlchemy needs the psycopg3 dialect prefix.
        dsn = raw_uri.replace("postgresql://", "postgresql+psycopg://", 1)
        yield dsn
    finally:
        server.cleanup()
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture()
def clean_postgres_dsn(postgres_dsn: str) -> Iterator[str]:
    """Yield the same DSN but ensure the Phase 0 schema is dropped
    before and after each test so tests do not see each other's rows."""
    import sqlalchemy as sa

    from mesoforge.storage.postgres.database import Base
    from mesoforge.storage.postgres.models import (  # noqa: F401
        ActivityInputRow,
        ActivityOutputRow,
        ActivityRow,
        ArtifactRow,
        ConfigurationSnapshotRow,
        GridRow,
        RunRow,
        StoredObjectRow,
    )

    engine = sa.create_engine(postgres_dsn, future=True)
    try:
        Base.metadata.drop_all(engine)
        with engine.begin() as connection:
            connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
        yield postgres_dsn
    finally:
        Base.metadata.drop_all(engine)
        with engine.begin() as connection:
            connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()
