"""Execute the POSIX scripts with an isolated Docker stub to inject failures.

This proves shell ordering and exit handling, not container build/runtime behavior.
Real tar and sha256sum retain the archive and checksum boundaries in these tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

DOCKER_STUB = r"""#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$DOCKER_LOG"
case "$*" in
    *" ps "*)
        test "${FAIL_AT:-}" != process_check
        case "$*" in
            *"forecast-worker"*) test "${RUNNING_FORECAST:-0}" = 0 || echo forecast ;;
            *"guidance-worker"*) test "${RUNNING_GUIDANCE:-0}" = 0 || echo guidance ;;
        esac ;;
    *" start guidance-worker"*) test "${FAIL_AT:-}" != restart ;;
    *"local_backup estimate"*)
        printf '{"status":"estimated","backup_bytes":%s}\n' "${ESTIMATE_BYTES:-1}" ;;
    *"pg_dump -Fc"*) test "${FAIL_AT:-}" != dump; printf 'database dump\n' ;;
    *"--entrypoint pg_restore postgres --list"*) cat >/dev/null; printf 'dump list\n' ;;
    *"pg_restore --list"*) cat >/dev/null; printf 'dump list\n' ;;
    *"pg_restore --exit-on-error"*) cat >/dev/null ;;
    *"information_schema.tables"*) echo "${TABLE_COUNT:-0}" ;;
    *"export-objects"*)
        test "${FAIL_AT:-}" != export
        while [ "$1" != '-v' ]; do shift; done
        target=${2%:/backup}
        printf '{"bucket":"test","objects":[]}\n' > "$target/objects-manifest.json" ;;
    *"--entrypoint tar"*)
        test "${FAIL_AT:-}" != tar
        if [ "${SLOW_TAR:-0}" = 1 ]; then
            echo "$PPID" > "$PARENT_PID_FILE"
            trap 'echo stopped > "$CHILD_STOPPED_FILE"; exit 143' TERM
            while :; do sleep 0.05; done
        fi
        case "$*" in
            *" -czf "*) tar -C "$FAKE_RUNTIME" -czf - . ;;
            *" -tzf "*) tar -tzf - ;;
            *" -xzf "*) tar -C "$FAKE_RUNTIME" -xzf - ;;
        esac ;;
    *"--entrypoint sh"*) test "${NONEMPTY_RUNTIME:-0}" = 0 ;;
    *"validate-export"*) test "${FAIL_AT:-}" != validate ;;
    *"check-empty-storage"*) test "${FAIL_AT:-}" != storage ;;
    *"config --images"*) echo 'mesoforge:test' ;;
esac
"""


@pytest.fixture
def shell() -> str:
    # Git's POSIX shell lets Windows run these failure tests without WSL or Docker.
    bash = shutil.which("bash") or "C:/Program Files/Git/bin/bash.exe"
    if not Path(bash).is_file():
        pytest.skip("POSIX shell is unavailable")
    return bash


@pytest.fixture
def harness(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    hosted = tmp_path / "repository" / "deploy" / "hosted"
    hosted.mkdir(parents=True)
    for name in ("backup.sh", "restore.sh"):
        shutil.copyfile(ROOT / "deploy" / "hosted" / name, hosted / name)
    module = tmp_path / "repository/src/mesoforge/application"
    module.mkdir(parents=True)
    shutil.copyfile(
        ROOT / "src/mesoforge/application/disk_admission.py", module / "disk_admission.py"
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(DOCKER_STUB, encoding="utf-8", newline="\n")
    docker.chmod(0o700)
    runtime = tmp_path / "runtime"
    (runtime / "guidance" / "snapshots" / "retained" / "source").mkdir(parents=True)
    (runtime / "guidance" / "snapshots" / "retained" / "source" / "array.bin").write_bytes(
        b"retained guidance"
    )
    (runtime / "latest_baseline.json").write_text('{"baseline_id":"retained"}', encoding="utf-8")
    env = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "DOCKER_LOG": (tmp_path / "docker.log").as_posix(),
        "FAKE_RUNTIME": runtime.as_posix(),
        "RUNNING_GUIDANCE": "1",
        "MESOFORGE_BACKUP_PYTHON": sys.executable,
        "MESOFORGE_GUIDANCE_MIN_FREE_GB": "0",
    }
    return hosted, env


def run_script(
    shell: str, harness: tuple[Path, dict[str, str]], name: str, target: Path, **env: str
) -> subprocess.CompletedProcess[str]:
    hosted, base_env = harness
    return subprocess.run(
        [shell, (hosted / name).as_posix(), target.as_posix()],
        env={**base_env, **env},
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize("failure", ["dump", "export", "tar", "restart"])
def test_backup_failure_never_reports_complete_and_restarts_guidance(
    shell: str, harness: tuple[Path, dict[str, str]], tmp_path: Path, failure: str
) -> None:
    result = run_script(shell, harness, "backup.sh", tmp_path / "backups", FAIL_AT=failure)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Backup complete:" not in result.stdout
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1 and backups[0].name.endswith(".incomplete")
    log = Path(harness[1]["DOCKER_LOG"]).read_text("utf-8")
    assert "start guidance-worker" in log
    assert log.index("stop guidance-worker") < log.index("pg_dump -Fc")


def test_backup_retains_guidance_and_publishes_only_after_restart(
    shell: str, harness: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    import tarfile

    result = run_script(shell, harness, "backup.sh", tmp_path / "backups")
    assert result.returncode == 0, result.stdout + result.stderr
    backup = next((tmp_path / "backups").iterdir())
    assert not backup.name.endswith(".incomplete")
    assert (backup / "SHA256SUMS").is_file()
    with tarfile.open(backup / "runtime.tar.gz") as archive:
        assert "./guidance/snapshots/retained/source/array.bin" in archive.getnames()
    if os.name != "nt":
        assert backup.stat().st_mode & 0o077 == 0


def test_backup_estimate_refuses_copy_that_would_cross_reserve(
    shell: str, harness: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    result = run_script(
        shell, harness, "backup.sh", tmp_path / "backups", ESTIMATE_BYTES=str(1024**5)
    )
    assert result.returncode != 0
    assert "cross disk reserve" in result.stderr
    log = Path(harness[1]["DOCKER_LOG"]).read_text("utf-8")
    assert "pg_dump -Fc" not in log and "export-objects" not in log
    assert "--entrypoint tar" not in log
    assert "start guidance-worker" in log
    assert "Backup complete:" not in result.stdout


def test_backup_term_stops_only_its_owned_copy_containers_before_return(
    shell: str, harness: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    hosted, environment = harness
    parent_file = tmp_path / "parent.pid"
    stopped = tmp_path / "child-stopped"
    process = subprocess.Popen(
        [shell, (hosted / "backup.sh").as_posix(), (tmp_path / "backups").as_posix()],
        env={
            **environment,
            "SLOW_TAR": "1",
            "PARENT_PID_FILE": parent_file.as_posix(),
            "CHILD_STOPPED_FILE": stopped.as_posix(),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 15
    try:
        while not parent_file.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)
        pid = parent_file.read_text().strip()
        assert pid.isdecimal()
        subprocess.run([shell, "-c", f"kill -TERM {pid}"], check=True, timeout=10)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
    assert process.returncode != 0
    assert "Backup complete:" not in stdout
    assert "Backup incomplete:" in stderr
    assert stopped.is_file()
    commands = Path(environment["DOCKER_LOG"]).read_text().splitlines()
    stops = [line for line in commands if line.startswith("stop --time 10 ")]
    assert len(stops) == 5  # estimate, dump, list, objects, runtime
    assert all(line.split()[-1].startswith("mesoforge-") for line in stops)
    assert not any(line in {"stop postgres", "stop minio"} for line in commands)
    assert commands[-1] == "compose start guidance-worker"


@pytest.mark.parametrize(
    "failure", ["checksum", "runtime", "storage", "validate", "worker", "process_check"]
)
def test_restore_preflight_fails_before_restoring_database_or_objects(
    shell: str, harness: tuple[Path, dict[str, str]], tmp_path: Path, failure: str
) -> None:
    backup_result = run_script(shell, harness, "backup.sh", tmp_path / "backups")
    assert backup_result.returncode == 0, backup_result.stdout + backup_result.stderr
    backup = next((tmp_path / "backups").iterdir())
    Path(harness[1]["DOCKER_LOG"]).write_text("", encoding="utf-8")
    if failure == "checksum":
        (backup / "postgres.dump").write_bytes(b"corrupt")
    result = run_script(
        shell,
        harness,
        "restore.sh",
        backup,
        RUNNING_GUIDANCE="0",
        RUNNING_FORECAST="1" if failure == "worker" else "0",
        NONEMPTY_RUNTIME="1" if failure == "runtime" else "0",
        FAIL_AT=failure,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    log = Path(harness[1]["DOCKER_LOG"]).read_text("utf-8")
    assert "pg_restore --exit-on-error" not in log
    assert "import-objects" not in log


def test_restore_valid_backup_preserves_pointer_and_guidance(
    shell: str, harness: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup_result = run_script(shell, harness, "backup.sh", tmp_path / "backups")
    assert backup_result.returncode == 0, backup_result.stdout + backup_result.stderr
    backup = next((tmp_path / "backups").iterdir())
    restored = tmp_path / "restored"
    restored.mkdir()
    result = run_script(
        shell, harness, "restore.sh", backup, RUNNING_GUIDANCE="0", FAKE_RUNTIME=restored.as_posix()
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (restored / "latest_baseline.json").read_text() == '{"baseline_id":"retained"}'
    assert (
        restored / "guidance/snapshots/retained/source/array.bin"
    ).read_bytes() == b"retained guidance"
    log = Path(harness[1]["DOCKER_LOG"]).read_text("utf-8")
    assert log.index("check-empty-storage") < log.index("pg_restore --exit-on-error")
    assert "--single-transaction" in log
