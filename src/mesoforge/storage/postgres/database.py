"""SQLAlchemy engine/session plumbing for the PostgreSQL metadata store
(plan Section 4.10).

This module owns the psycopg/SQLAlchemy dependency; ``storage.interfaces``
and everything above it never import it directly.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for all Phase 0 PostgreSQL-mapped tables."""


def resolve_database_dsn(dsn_environment_variable: str) -> str:
    """Resolve a database DSN from the named environment variable. Never
    hardcodes or accepts a literal DSN/credential in configuration --
    only the *name* of the environment variable is ever configured
    (plan Section 4.7)."""
    dsn = os.environ.get(dsn_environment_variable)
    if not dsn:
        raise RuntimeError(
            f"environment variable {dsn_environment_variable!r} is not set; it must "
            "contain the PostgreSQL connection DSN"
        )
    return dsn


def create_database_engine(dsn: str, *, echo: bool = False) -> Engine:
    return create_engine(dsn, echo=echo, future=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a Session, committing on success and rolling back on any
    exception. Callers that need finer-grained transaction control (the
    application-service algorithm in Section 5) manage commit/rollback
    themselves instead of using this helper."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
