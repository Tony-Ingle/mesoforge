"""Tests for scripts/check_repository_hygiene.py (Task 2 quality-gate scaffolding).

RED: written before scripts/check_repository_hygiene.py exists.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_repository_hygiene.py"


def run_hygiene(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_exists() -> None:
    assert SCRIPT.is_file()


def test_passes_on_clean_tree(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    result = run_hygiene(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_rejects_grib_files(tmp_path: Path) -> None:
    (tmp_path / "guidance.grib2").write_bytes(b"\x00\x01")
    result = run_hygiene(tmp_path)
    assert result.returncode != 0
    assert "grib" in (result.stdout + result.stderr).lower()


def test_rejects_env_files(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    result = run_hygiene(tmp_path)
    assert result.returncode != 0
    assert ".env" in (result.stdout + result.stderr).lower()


def test_rejects_netcdf_files_outside_fixtures(tmp_path: Path) -> None:
    (tmp_path / "output.nc").write_bytes(b"\x00\x01")
    result = run_hygiene(tmp_path)
    assert result.returncode != 0
    assert (
        "netcdf" in (result.stdout + result.stderr).lower()
        or ".nc" in (result.stdout + result.stderr).lower()
    )


def test_allows_netcdf_fixture_under_tests_fixtures(tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "tests" / "fixtures"
    fixtures_dir.mkdir(parents=True)
    (fixtures_dir / "sample.nc").write_bytes(b"\x00" * 10)
    result = run_hygiene(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_rejects_large_fixture_files(tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "tests" / "fixtures"
    fixtures_dir.mkdir(parents=True)
    (fixtures_dir / "huge.nc").write_bytes(b"\x00" * (2 * 1024 * 1024))
    result = run_hygiene(tmp_path)
    assert result.returncode != 0
    assert "size" in (result.stdout + result.stderr).lower()


def test_rejects_likely_credential_strings(tmp_path: Path) -> None:
    # Assembled at runtime (never a static "key = value" literal in this
    # source file) so this test file is not flagged by the very check it
    # exercises.
    field_name = "aws_secret" + "_access_key"
    fake_key_id = "AKIA" + "ABCDEFGHIJKLMNOP"
    line = field_name + ' = "' + fake_key_id + '"\n'
    (tmp_path / "config.py").write_text(line, encoding="utf-8")
    result = run_hygiene(tmp_path)
    assert result.returncode != 0
    assert "credential" in (result.stdout + result.stderr).lower()
