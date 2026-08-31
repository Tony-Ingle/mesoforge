"""Tests for scripts/validate_docs.py (Task 2 quality-gate scaffolding).

RED: written before scripts/validate_docs.py exists / is implemented.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate_docs.py"


def run_validator() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_validator_script_exists() -> None:
    assert SCRIPT.is_file(), "scripts/validate_docs.py must exist"


def test_validator_passes_on_current_docs() -> None:
    result = run_validator()
    assert result.returncode == 0, result.stdout + result.stderr


def test_validator_requires_unique_adr_numbers(tmp_path: Path) -> None:
    decisions_dir = tmp_path / "docs" / "decisions"
    decisions_dir.mkdir(parents=True)
    (decisions_dir / "0001-a.md").write_text(
        "# 0001: A\n\nStatus: Accepted\n\n## Context\n\nc\n\n## Decision\n\nd\n\n"
        "## Consequences\n\ne\n",
        encoding="utf-8",
    )
    (decisions_dir / "0001-b.md").write_text(
        "# 0001: B\n\nStatus: Accepted\n\n## Context\n\nc\n\n## Decision\n\nd\n\n"
        "## Consequences\n\ne\n",
        encoding="utf-8",
    )
    data_contracts_dir = tmp_path / "docs" / "data-contracts"
    data_contracts_dir.mkdir(parents=True)
    (data_contracts_dir / "vocabulary.md").write_text("# Vocabulary\n", encoding="utf-8")
    (data_contracts_dir / "phase-0.md").write_text("# Phase 0\n", encoding="utf-8")
    (data_contracts_dir / "phase-1.md").write_text("# Phase 1\n", encoding="utf-8")
    (data_contracts_dir / "phase-2.md").write_text("# Phase 2\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "duplicate" in (result.stdout + result.stderr).lower()


def test_validator_requires_accepted_status(tmp_path: Path) -> None:
    decisions_dir = tmp_path / "docs" / "decisions"
    decisions_dir.mkdir(parents=True)
    (decisions_dir / "0001-a.md").write_text(
        "# 0001: A\n\nStatus: Proposed\n\n## Context\n\nc\n\n## Decision\n\nd\n\n"
        "## Consequences\n\ne\n",
        encoding="utf-8",
    )
    data_contracts_dir = tmp_path / "docs" / "data-contracts"
    data_contracts_dir.mkdir(parents=True)
    (data_contracts_dir / "vocabulary.md").write_text("# Vocabulary\n", encoding="utf-8")
    (data_contracts_dir / "phase-0.md").write_text("# Phase 0\n", encoding="utf-8")
    (data_contracts_dir / "phase-1.md").write_text("# Phase 1\n", encoding="utf-8")
    (data_contracts_dir / "phase-2.md").write_text("# Phase 2\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "status" in (result.stdout + result.stderr).lower()


def test_validator_requires_required_headings(tmp_path: Path) -> None:
    decisions_dir = tmp_path / "docs" / "decisions"
    decisions_dir.mkdir(parents=True)
    (decisions_dir / "0001-a.md").write_text(
        "# 0001: A\n\nStatus: Accepted\n\n## Context\n\nc\n",
        encoding="utf-8",
    )
    data_contracts_dir = tmp_path / "docs" / "data-contracts"
    data_contracts_dir.mkdir(parents=True)
    (data_contracts_dir / "vocabulary.md").write_text("# Vocabulary\n", encoding="utf-8")
    (data_contracts_dir / "phase-0.md").write_text("# Phase 0\n", encoding="utf-8")
    (data_contracts_dir / "phase-1.md").write_text("# Phase 1\n", encoding="utf-8")
    (data_contracts_dir / "phase-2.md").write_text("# Phase 2\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "decision" in (result.stdout + result.stderr).lower()


def test_validator_detects_broken_relative_links(tmp_path: Path) -> None:
    decisions_dir = tmp_path / "docs" / "decisions"
    decisions_dir.mkdir(parents=True)
    (decisions_dir / "0001-a.md").write_text(
        "# 0001: A\n\nStatus: Accepted\n\n## Context\n\nSee [missing](missing.md).\n\n"
        "## Decision\n\nd\n\n## Consequences\n\ne\n",
        encoding="utf-8",
    )
    data_contracts_dir = tmp_path / "docs" / "data-contracts"
    data_contracts_dir.mkdir(parents=True)
    (data_contracts_dir / "vocabulary.md").write_text("# Vocabulary\n", encoding="utf-8")
    (data_contracts_dir / "phase-0.md").write_text("# Phase 0\n", encoding="utf-8")
    (data_contracts_dir / "phase-1.md").write_text("# Phase 1\n", encoding="utf-8")
    (data_contracts_dir / "phase-2.md").write_text("# Phase 2\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "link" in (result.stdout + result.stderr).lower()
