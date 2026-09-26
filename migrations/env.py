"""Alembic migration environment for the Phase 0 PostgreSQL schema.

The DSN is resolved from the environment at run time -- never hardcoded
in this file, matching the plan's rule that configuration snapshots
carry only environment-variable *names*, not literal DSNs.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from mesoforge.storage.postgres.database import Base
from mesoforge.storage.postgres.models import (  # noqa: F401 - registers tables on Base.metadata
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

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_url() -> str:
    url = os.environ.get("MESOFORGE_ALEMBIC_DSN") or os.environ.get("MESOFORGE_DATABASE_DSN")
    if not url:
        raise RuntimeError(
            "MESOFORGE_ALEMBIC_DSN or MESOFORGE_DATABASE_DSN must be set to run migrations"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _get_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
