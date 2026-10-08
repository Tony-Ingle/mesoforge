"""Static proofs for the Phase 2 operator validation entry points."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_makefile_exposes_offline_and_acceptance_boundaries() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "phase2-offline: quality test phase2-scientific" in makefile
    assert "phase2-acceptance:" in makefile
    assert "tests/acceptance/test_phase2_multimodel_baseline.py" in makefile
    assert 'if [ "$$MESOFORGE_LIVE_TESTS" != "1" ]' in makefile
