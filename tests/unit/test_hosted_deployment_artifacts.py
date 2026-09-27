"""Static invariants of the hosted deployment artifacts (no Docker required).

These guard security and operability properties a reviewer would otherwise have to
re-derive: non-root image, locked production dependencies, no published database or
object-store ports, secrets only from uncommitted files, process health only on the
long-running worker, explicit migration, and a DST-safe schedule.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
HOSTED = ROOT / "deploy" / "hosted"


def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((HOSTED / "compose.yaml").read_text("utf-8"))
    return loaded


def runtime_stage(dockerfile: str) -> str:
    return dockerfile.split("AS runtime", 1)[1]


def test_image_is_non_root_locked_and_carries_no_healthcheck_or_secret() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    runtime = runtime_stage(dockerfile)
    assert "USER 10001:10001" in runtime
    assert "uv sync --locked --no-dev" in dockerfile
    assert "--all-groups" not in dockerfile and "--dev" not in dockerfile.replace("--no-dev", "")
    assert "ghcr.io/astral-sh/uv:0.12.6" in dockerfile
    assert "HEALTHCHECK" not in dockerfile  # compose defines it for the guidance worker only
    assert "ARG MESOFORGE_CODE_REVISION" in runtime
    assert "MESOFORGE_CODE_REVISION=${MESOFORGE_CODE_REVISION}" in runtime
    for variable in (
        "MESOFORGE_PROSPECTIVE_ROOT",
        "MESOFORGE_OBSERVATIONS_DIR",
        "MESOFORGE_MRMS_DIR",
    ):
        match = re.search(rf"{variable}=(\S+)", runtime)
        assert match and match.group(1).startswith("/var/lib/mesoforge/runtime")
    assert "chown -R 10001:10001 /var/lib/mesoforge" in runtime
    assert not re.search(r"(?i)(password|secret|api_key|token)\s*=", dockerfile)


def test_docker_context_is_an_allowlist() -> None:
    lines = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text("utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*"
    allowed = {line[1:].rstrip("/") for line in lines if line.startswith("!")}
    assert allowed == {
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "alembic.ini",
        "migrations",
        "configs",
        "src",
    }


def test_no_service_publishes_ports_and_storage_is_internal() -> None:
    stack = compose()
    services = stack["services"]
    assert all("ports" not in service for service in services.values())
    assert stack["networks"]["backend"]["internal"] is True
    assert services["postgres"]["networks"] == ["backend"]
    assert services["minio"]["networks"] == ["backend"]
    assert services["admin"]["networks"] == ["backend"]
    for role in ("guidance-worker", "forecast-worker"):
        assert set(services[role]["networks"]) == {"backend", "egress"}


def test_health_restart_and_shutdown_belong_to_the_long_running_worker() -> None:
    services = compose()["services"]
    guidance = services["guidance-worker"]
    assert guidance["restart"] == "unless-stopped"
    assert guidance["init"] is True
    assert "worker_status" in " ".join(guidance["healthcheck"]["test"])
    minutes = int(guidance["stop_grace_period"].rstrip("m"))
    assert minutes >= 15
    assert "healthcheck" not in services["forecast-worker"]
    assert "healthcheck" not in services["admin"]
    assert services["forecast-worker"]["restart"] == "no"
    assert "--scheduled" in services["forecast-worker"]["command"]


def test_secrets_are_required_interpolations_scoped_per_service() -> None:
    text = (HOSTED / "compose.yaml").read_text("utf-8")
    for name in (
        "MESOFORGE_PG_PASSWORD",
        "MESOFORGE_PG_WORKER_PASSWORD",
        "MESOFORGE_MINIO_ROOT_PASSWORD",
        "MESOFORGE_S3_SECRET_KEY",
    ):
        assert f"${{{name}:?" in text, name
    assert "OPENAI_API_KEY" not in text  # only ai.env, only the forecast worker
    assert "MESOFORGE_CODE_REVISION" not in text  # baked into the image, never overridden
    services = compose()["services"]
    assert services["forecast-worker"]["env_file"] == [{"path": "./ai.env", "required": False}]
    assert all("env_file" not in services[name] for name in services if name != "forecast-worker")
    for role in ("guidance-worker", "forecast-worker"):
        environment = services[role]["environment"]
        assert "MESOFORGE_ALEMBIC_DSN" not in environment
        assert "MESOFORGE_PG_WORKER_USER" in environment["MESOFORGE_DATABASE_DSN"]
    assert "MESOFORGE_ALEMBIC_DSN" in services["admin"]["environment"]
    # A present-but-blank desk setting is invalid; compose never sets one to empty.
    for key, value in services["forecast-worker"]["environment"].items():
        if key.startswith("MESOFORGE_AI_"):
            assert ":-" in value and not value.endswith(":-}")


def test_examples_hold_placeholders_only() -> None:
    for name in (".env.example", "ai.env.example"):
        text = (HOSTED / name).read_text("utf-8")
        for line in text.splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if any(word in key for word in ("PASSWORD", "SECRET", "ACCESS_KEY", "API_KEY")):
                assert value == "REPLACE_ME", line
    assert "\nOPENAI_API_KEY=" not in (HOSTED / "ai.env.example").read_text("utf-8")


def test_database_worker_role_cannot_change_schema_governance_or_delete() -> None:
    from mesoforge.storage.postgres import schema

    sql = (HOSTED / "postgres-init" / "worker-role.psql").read_text("utf-8").upper()
    assert "NOSUPERUSER" in sql and "REVOKE CREATE ON SCHEMA PUBLIC FROM PUBLIC" in sql
    assert "IN ROLE MESOFORGE_RUNTIME" in sql
    table_grants = [line for line in sql.splitlines() if " ON ALL TABLES" in line]
    assert table_grants == []  # applied after migration, never as blanket defaults
    grants = " ".join(schema._RUNTIME_GRANTS).upper()
    assert "DELETE" not in grants and "TRUNCATE" not in grants and "TRIGGER" not in grants
    assert "GRANT SELECT, INSERT ON ALL TABLES" in grants
    assert "UPDATE (STATUS, COMPLETED_AT, ERROR) ON ACTIVITIES" in grants
    assert "governance_events" in schema._READ_ONLY_TABLES


def test_schedule_is_named_timezone_and_scripts_keep_safe_ordering() -> None:
    timer = (HOSTED / "systemd" / "mesoforge-forecast.timer").read_text("utf-8")
    assert "OnCalendar=*-*-* 08,20:00:00 America/Chicago" in timer
    service = (HOSTED / "systemd" / "mesoforge-forecast.service").read_text("utf-8")
    assert "TimeoutStartSec=" in service and "run --rm forecast-worker" in service
    backup = (HOSTED / "backup.sh").read_text("utf-8")
    assert (
        backup.index("pg_dump -Fc")
        < backup.index("export-objects --destination")
        < backup.index("--entrypoint tar")
    )
    assert "inside the repository" in backup and "exec -T" in backup
    restore = (HOSTED / "restore.sh").read_text("utf-8")
    assert "already has tables" in restore
    assert "migrate" not in restore.replace("migration-status", "")
    build = (HOSTED / "build-image.sh").read_text("utf-8")
    assert "git status --porcelain" in build and "git -c core.autocrlf=false archive" in build


def test_workers_never_migrate_implicitly() -> None:
    for name in ("guidance_worker.py", "forecast_worker.py"):
        source = (ROOT / "src" / "mesoforge" / "application" / name).read_text("utf-8")
        assert "upgrade_to_head" not in source and "command.upgrade" not in source


def test_every_runtime_variable_is_documented_once_in_the_readme() -> None:
    readme = (ROOT / "README.md").read_text("utf-8")
    names: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        names.update(
            re.findall(r"\"(MESOFORGE_[A-Z0-9_]+|OPENAI_API_KEY)\"", path.read_text("utf-8"))
        )
    names -= {"MESOFORGE_TEST_DATABASE_DSN", "MESOFORGE_AI_BUDGETS"}
    assert names, "expected runtime variables in src"
    table = readme.split("### Environment variables", 1)[1].split("\n## ", 1)[0]
    missing = sorted(name for name in names if f"`{name}`" not in table)
    assert not missing, missing
    rows = re.findall(r"^\| `([A-Z0-9_<>]+)`", table, re.MULTILINE)
    assert len(rows) == len(set(rows)), "a variable is documented twice"
