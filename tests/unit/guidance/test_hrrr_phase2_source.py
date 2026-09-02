"""Unit tests for mesoforge.guidance.sources.hrrr_phase2 (plan Section
2.2, Task 5).
"""

from __future__ import annotations

import pytest

from mesoforge.guidance.sources.hrrr_phase2 import build_field_selector


class TestBuildFieldSelector:
    def test_analysis_lead_uses_anl(self) -> None:
        assert build_field_selector("air_temperature_2m", forecast_hour=0) == (
            ":TMP:2 m above ground:anl:$"
        )

    def test_forecast_lead_uses_hour_fcst(self) -> None:
        assert build_field_selector("dew_point_temperature_2m", forecast_hour=24) == (
            ":DPT:2 m above ground:24 hour fcst:$"
        )

    def test_gust_selector(self) -> None:
        assert build_field_selector("wind_gust_10m", forecast_hour=36) == (
            ":GUST:surface:36 hour fcst:$"
        )

    def test_qpf_rolling_one_hour(self) -> None:
        assert (
            build_field_selector("liquid_equivalent_precipitation_amount_1h", forecast_hour=6)
            == ":APCP:surface:5-6 hour acc fcst:$"
        )

    def test_qpf_rejects_lead_zero(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            build_field_selector("liquid_equivalent_precipitation_amount_1h", forecast_hour=0)

    def test_rejects_unknown_variable(self) -> None:
        with pytest.raises(ValueError, match="unknown"):
            build_field_selector("not_a_variable", forecast_hour=1)

    def test_rejects_negative_lead(self) -> None:
        with pytest.raises(ValueError, match="nonnegative"):
            build_field_selector("air_temperature_2m", forecast_hour=-1)
