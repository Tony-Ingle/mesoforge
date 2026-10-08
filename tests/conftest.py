"""Shared pytest fixtures and markers for MesoForge tests.

Includes real PostgreSQL fixtures used by both ``tests/integration/``
and ``tests/acceptance/``. Two backends are supported:

- An operator can supply a dedicated real PostgreSQL service through
  ``MESOFORGE_TEST_DATABASE_DSN``. When that variable is set,
  ``postgres_dsn`` connects directly rather than spawning another server.
- Locally (or in any environment without ``MESOFORGE_TEST_DATABASE_DSN``
  set, e.g. this development sandbox with no docker group membership),
  ``postgres_dsn`` falls back to the ``pgserver`` pip package -- a
  self-contained, non-root, no-Docker PostgreSQL binary distribution.
  This is a genuine PostgreSQL 16+ instance, not SQLite and not a mock.

Either path exercises the identical Alembic migrations and SQLAlchemy
models against a real PostgreSQL server.
"""

from __future__ import annotations

import os
import shutil
import smtplib
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_real_email_in_tests(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Offline tests use injected SMTP clients, never inherited operator credentials."""
    if request.node.get_closest_marker("live") is not None:
        yield
        return
    for name in list(os.environ):
        if name.startswith(("MESOFORGE_SMTP_", "MESOFORGE_EMAIL_")):
            monkeypatch.delenv(name)
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Real SMTP is forbidden in ordinary tests")

    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden)
    yield
    assert not attempts, "An offline test attempted real SMTP"


@pytest.fixture(autouse=True)
def no_paid_forecast_desk_in_tests(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Ordinary suites never spend runtime-provider credentials inherited from an operator.

    Operator desk settings (provider, model, pacing, budgets) and the provider
    credential are removed so offline results never depend on the invoking shell.
    The controller converts transport exceptions into a safe fallback, so an attempt
    is also recorded and fails the test at teardown instead of passing silently.
    """
    if request.node.get_closest_marker("live") is not None:
        yield
        return
    from mesoforge.application import forecast_desk_provider

    for name in list(os.environ):
        if name.startswith("MESOFORGE_AI_") or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name)
    attempts: list[None] = []

    def forbidden(*args: object, **kwargs: object) -> bytes:
        attempts.append(None)
        raise AssertionError("Live forecast-desk provider transport is disabled in offline tests")

    monkeypatch.setattr(forecast_desk_provider, "_post", forbidden)
    yield
    assert not attempts, "An offline test reached the live forecast-desk provider transport"


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """Yield a psycopg3 DSN for a real PostgreSQL instance.

    Uses the CI-declared ``postgres:16`` service (via
    ``MESOFORGE_TEST_DATABASE_DSN``) when set; otherwise starts a
    session-scoped ephemeral ``pgserver`` instance and tears it down at
    the end of the test session.
    """
    env_dsn = os.environ.get("MESOFORGE_TEST_DATABASE_DSN")
    if env_dsn:
        yield env_dsn
        return

    import pgserver

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
        GovernanceEventRow,
        GridRow,
        IssuedForecastRow,
        RunRow,
        RunSelectedInputRow,
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
