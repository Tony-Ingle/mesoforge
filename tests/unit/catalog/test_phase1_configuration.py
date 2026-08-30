"""Unit tests for the Phase 1 addition to mesoforge.catalog.configuration
(plan Section 3.2, Task 2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mesoforge.catalog.configuration import (
    compute_configuration_digest,
    load_configuration_source,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_CONFIG = REPO_ROOT / "configs" / "base.yaml"
PHASE1_CONFIG = REPO_ROOT / "configs" / "phase1-grasston.yaml"


class TestPhase1ConfigurationLoads:
    def test_base_config_still_validates_without_phase1(self) -> None:
        config, _ = load_configuration_source(base_path=BASE_CONFIG)
        assert config.phase1 is None

    def test_phase1_grasston_config_validates(self) -> None:
        config, _ = load_configuration_source(base_path=BASE_CONFIG, environment_path=PHASE1_CONFIG)
        assert config.phase1 is not None
        assert config.phase1.domain.domain_id == "grasston-minnesota.v1"
        assert config.phase1.domain.station_ids == (
            "station.kcbg",
            "station.kjmr",
            "station.kros",
        )
        assert config.phase1.hrrr.forecast_hours == tuple(range(7))
        assert config.phase1.matching_policy.calm_threshold_m_s == pytest.approx(1.5)

    def test_phase1_digest_is_deterministic(self) -> None:
        config_a, _ = load_configuration_source(
            base_path=BASE_CONFIG, environment_path=PHASE1_CONFIG
        )
        config_b, _ = load_configuration_source(
            base_path=BASE_CONFIG, environment_path=PHASE1_CONFIG
        )
        assert compute_configuration_digest(config_a) == compute_configuration_digest(config_b)

    def test_digest_changes_when_calm_threshold_changes(self, tmp_path: Path) -> None:
        config_a, _ = load_configuration_source(
            base_path=BASE_CONFIG, environment_path=PHASE1_CONFIG
        )
        overlay_text = PHASE1_CONFIG.read_text(encoding="utf-8").replace(
            "calm_threshold_m_s: 1.5", "calm_threshold_m_s: 2.0"
        )
        assert "calm_threshold_m_s: 2.0" in overlay_text
        overlay_path = tmp_path / "phase1-modified.yaml"
        overlay_path.write_text(overlay_text, encoding="utf-8")
        config_b, _ = load_configuration_source(
            base_path=BASE_CONFIG, environment_path=overlay_path
        )
        assert compute_configuration_digest(config_a) != compute_configuration_digest(config_b)


class TestPhase1ConfigurationRejectsDrift:
    def _write(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "bad-phase1.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_rejects_station_metadata_drift(self, tmp_path: Path) -> None:
        overlay_text = PHASE1_CONFIG.read_text(encoding="utf-8").replace(
            "expected_latitude: 45.55700", "expected_latitude: 10.0"
        )
        overlay_path = self._write(tmp_path, overlay_text)
        with pytest.raises(ValidationError):
            load_configuration_source(base_path=BASE_CONFIG, environment_path=overlay_path)

    def test_rejects_station_outside_domain(self, tmp_path: Path) -> None:
        overlay_text = PHASE1_CONFIG.read_text(encoding="utf-8").replace(
            "expected_longitude: -93.26400", "expected_longitude: -80.0"
        )
        overlay_path = self._write(tmp_path, overlay_text)
        with pytest.raises(ValidationError):
            load_configuration_source(base_path=BASE_CONFIG, environment_path=overlay_path)
