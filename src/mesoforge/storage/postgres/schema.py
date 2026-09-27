"""Read-only schema revision checks and the one explicit migration entry point.

Workers never migrate. They compare the database revision with the repository's
Alembic head and refuse to act when they differ; an operator runs ``upgrade_to_head``
deliberately (``python -m mesoforge.application.operations migrate``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

_ROOT = Path(__file__).resolve().parents[4]
# NOLOGIN group role of the hosted worker logins (deploy/hosted/postgres-init).
RUNTIME_ROLE = "mesoforge_runtime"
# Workers read and append; only activity status changes; governance is read-only.
_RUNTIME_GRANTS = (
    f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}",
    f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {RUNTIME_ROLE}",
    f"GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA public TO {RUNTIME_ROLE}",
    f"GRANT UPDATE (status, completed_at, error) ON activities TO {RUNTIME_ROLE}",
    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE}",
)
_READ_ONLY_TABLES = ("governance_events", "alembic_version")


def alembic_config() -> Config:
    """Repository migrations, independent of the working directory."""
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    return config


def expected_head() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("Repository migrations define no head revision")
    return head


# The workers' first database contact: an unreachable server fails fast, not after
# the driver's multi-minute default.
CONNECT_TIMEOUT_SECONDS = 10


def database_revision(dsn: str) -> str | None:
    """The applied revision, or None for a database that was never migrated."""
    engine = sa.create_engine(
        dsn, future=True, connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS}
    )
    try:
        with engine.connect() as connection:
            if not sa.inspect(connection).has_table("alembic_version"):
                return None
            revision = connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
            return str(revision) if revision is not None else None
    finally:
        engine.dispose()


def schema_status(dsn: str) -> dict[str, Any]:
    current, head = database_revision(dsn), expected_head()
    return {"current": current, "head": head, "at_head": current == head}


def upgrade_to_head() -> None:
    """Explicit operator action; the DSN comes from MESOFORGE_ALEMBIC_DSN/DATABASE_DSN."""
    command.upgrade(alembic_config(), "head")


def apply_runtime_grants(dsn: str) -> str:
    """Idempotent least-privilege table grants for the worker group role, as the owner.

    Returns ``role_absent`` for databases without the hosted runtime role (for example
    local development), where nothing changes.
    """
    engine = sa.create_engine(dsn, future=True)
    try:
        with engine.begin() as connection:
            exists = connection.execute(
                sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": RUNTIME_ROLE}
            ).first()
            if exists is None:
                return "role_absent"
            for statement in _RUNTIME_GRANTS:
                connection.execute(sa.text(statement))
            for table in _READ_ONLY_TABLES:
                if connection.execute(
                    sa.text("SELECT to_regclass(:name)"), {"name": f"public.{table}"}
                ).scalar():
                    connection.execute(sa.text(f"REVOKE INSERT ON {table} FROM {RUNTIME_ROLE}"))
        return "applied"
    finally:
        engine.dispose()
