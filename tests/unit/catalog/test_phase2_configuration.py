"""Unit tests for the Phase 2 addition to mesoforge.catalog.configuration
(plan Section 1, Task 1).

RED: written before configs/phase2-grasston.yaml / the Phase2Configuration
model existed.
"""

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
PHASE2_CONFIG = REPO_ROOT / "configs" / "phase2-grasston.yaml"


def _load_phase2():
    return load_configuration_source(
        base_path=BASE_CONFIG,
        environment_path=PHASE1_CONFIG,
        additional_overlay_paths=(PHASE2_CONFIG,),
    )


class TestPhase2ConfigurationLoads:
    def test_phase2_grasston_config_validates(self) -> None:
        config, _ = _load_phase2()
        assert config.phase2 is not None
        assert config.phase2.domain.domain_id == "grasston-minnesota.v1"
        assert config.phase2.domain.station_ids == (
            "station.kcbg",
            "station.kjmr",
            "station.kros",
        )

    def test_phase2_retains_exact_phase1_domain(self) -> None:
        config, _ = _load_phase2()
        assert config.phase2.domain == config.phase1.domain
        assert config.phase2.stations == config.phase1.stations

    def test_phase2_requires_phase1_present(self) -> None:
        with pytest.raises(ValidationError, match="requires phase1"):
            load_configuration_source(base_path=BASE_CONFIG, environment_path=PHASE2_CONFIG)

    def test_hrrr_allowed_cycle_hours(self) -> None:
        config, _ = _load_phase2()
        assert config.phase2.hrrr.allowed_cycle_hours == (0, 6, 12, 18)
        assert config.phase2.hrrr.max_age_hours == 6.0
        assert config.phase2.hrrr.max_source_lead_hours == 48

    def test_nbm_hourly_cadence_and_probability_threshold(self) -> None:
        config, _ = _load_phase2()
        assert config.phase2.nbm.max_age_hours == 3.0
        assert config.phase2.nbm.probability_threshold_kg_m2 == pytest.approx(0.254)

    def test_gfs_allowed_cycle_hours(self) -> None:
        config, _ = _load_phase2()
        assert config.phase2.gfs.allowed_cycle_hours == (0, 6, 12, 18)
        assert config.phase2.gfs.max_age_hours == 12.0

    def test_rrfs_and_disabled_paths(self) -> None:
        config, _ = _load_phase2()
        assert config.phase2.rrfs_enabled is False
        assert config.phase2.precipitation_type_enabled is False
        assert config.phase2.ai_adjustment_enabled is False
        assert config.phase2.learned_weights_enabled is False
        assert config.phase2.publication_enabled is False

    def test_cycle_selection_policy_horizons(self) -> None:
        config, _ = _load_phase2()
        assert config.phase2.cycle_selection_policy.target_horizons == tuple(range(1, 37))

    def test_digest_is_deterministic(self) -> None:
        config_a, _ = _load_phase2()
        config_b, _ = _load_phase2()
        assert compute_configuration_digest(config_a) == compute_configuration_digest(config_b)


class TestFallbackWeightTables:
    def test_scalar_vector_table_every_row_sums_to_one(self) -> None:
        config, _ = _load_phase2()
        table = config.phase2.blend_configuration.scalar_vector_table
        for row in table.rows:
            assert sum(row.weights) == pytest.approx(1.0, abs=1e-12)

    def test_qpf_table_every_row_sums_to_one(self) -> None:
        config, _ = _load_phase2()
        table = config.phase2.blend_configuration.qpf_table
        for row in table.rows:
            assert sum(row.weights) == pytest.approx(1.0, abs=1e-12)

    def test_scalar_vector_table_has_14_rows(self) -> None:
        config, _ = _load_phase2()
        assert len(config.phase2.blend_configuration.scalar_vector_table.rows) == 14

    def test_row_for_h_and_n_early_horizon(self) -> None:
        config, _ = _load_phase2()
        row = config.phase2.blend_configuration.scalar_vector_table.row_for(
            available_models=("HRRR", "NBM"), horizon=1
        )
        assert row.weights == (0.60, 0.40, 0.0)

    def test_row_for_h_and_n_late_horizon(self) -> None:
        config, _ = _load_phase2()
        row = config.phase2.blend_configuration.scalar_vector_table.row_for(
            available_models=("HRRR", "NBM"), horizon=19
        )
        assert row.weights == (0.45, 0.55, 0.0)

    def test_row_for_all_three_early_horizon(self) -> None:
        config, _ = _load_phase2()
        row = config.phase2.blend_configuration.scalar_vector_table.row_for(
            available_models=("HRRR", "NBM", "GFS"), horizon=18
        )
        assert row.weights == (0.50, 0.30, 0.20)

    def test_qpf_row_for_n_and_g(self) -> None:
        config, _ = _load_phase2()
        row = config.phase2.blend_configuration.qpf_table.row_for(
            available_models=("NBM", "GFS"), horizon=1
        )
        assert row.weights == (0.0, 0.75, 0.25)

    def test_missing_row_raises_key_error(self) -> None:
        config, _ = _load_phase2()
        table = config.phase2.blend_configuration.scalar_vector_table
        with pytest.raises(KeyError):
            table.row_for(available_models=(), horizon=1)

    def test_row_rejects_zero_weight_for_available_model(self) -> None:
        from mesoforge.catalog.configuration import FallbackWeightRow

        with pytest.raises(ValidationError, match="strictly positive"):
            FallbackWeightRow(
                row_id="bad.row",
                available_models=("HRRR", "NBM"),
                horizon_band="h01-h18",
                weights=(1.0, 0.0, 0.0),
            )

    def test_row_rejects_nonzero_weight_for_unavailable_model(self) -> None:
        from mesoforge.catalog.configuration import FallbackWeightRow

        with pytest.raises(ValidationError, match="weight exactly 0"):
            FallbackWeightRow(
                row_id="bad.row",
                available_models=("HRRR",),
                horizon_band="h01-h18",
                weights=(0.9, 0.1, 0.0),
            )

    def test_row_rejects_weights_not_summing_to_one(self) -> None:
        from mesoforge.catalog.configuration import FallbackWeightRow

        with pytest.raises(ValidationError, match="sum to exactly 1"):
            FallbackWeightRow(
                row_id="bad.row",
                available_models=("HRRR", "NBM", "GFS"),
                horizon_band="h01-h18",
                weights=(0.5, 0.3, 0.3),
            )

    def test_table_rejects_missing_row(self) -> None:
        from mesoforge.catalog.configuration import FallbackWeightRow, FallbackWeightTable

        rows = [
            FallbackWeightRow(
                row_id="only.row",
                available_models=("HRRR",),
                horizon_band="h01-h18",
                weights=(1.0, 0.0, 0.0),
            )
        ]
        with pytest.raises(ValidationError, match="missing"):
            FallbackWeightTable(table_id="incomplete.v1", rows=tuple(rows))


class TestGustAndPopPolicies:
    def test_pop_policy_is_nbm_only(self) -> None:
        config, _ = _load_phase2()
        pop = config.phase2.blend_configuration.pop_policy
        assert pop.sole_contributor == "NBM"
        assert pop.weight == 1.0

    def test_pop_policy_rejects_non_unity_weight(self) -> None:
        from mesoforge.catalog.configuration import PopBlendPolicy

        with pytest.raises(ValidationError, match="exactly 1.0"):
            PopBlendPolicy(weight=0.5)

    def test_gust_policy_defaults(self) -> None:
        config, _ = _load_phase2()
        gust = config.phase2.blend_configuration.gust_policy
        assert gust.shortfall_floor_tolerance_m_s == pytest.approx(0.1)
        assert gust.final_epsilon_floor_m_s == pytest.approx(1e-6)
        assert gust.valid_max_m_s == pytest.approx(100.0)
