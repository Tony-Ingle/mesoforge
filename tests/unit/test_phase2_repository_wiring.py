"""Static proofs for the Phase 2 offline and CI entry points."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_makefile_exposes_offline_and_acceptance_boundaries() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "phase2-offline: quality test phase2-scientific" in makefile
    assert "phase2-acceptance:" in makefile
    assert "tests/acceptance/test_phase2_multimodel_baseline.py" in makefile
    assert 'if [ "$$MESOFORGE_LIVE_TESTS" != "1" ]' in makefile


def test_ci_keeps_live_default_off_and_runs_phase2_on_real_services() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "Proof that live tests are skipped by default" in workflow
    assert "MESOFORGE_LIVE_TESTS:" not in workflow
    assert "image: postgres:16" in workflow
    assert "docker run -d --name mesoforge-minio" in workflow
    assert "Migration upgrade / downgrade / upgrade" in workflow
    assert "run: make phase2-acceptance" in workflow
    assert "--cov-fail-under=90" in workflow
