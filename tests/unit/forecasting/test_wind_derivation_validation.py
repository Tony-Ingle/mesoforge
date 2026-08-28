"""Unit tests for mesoforge.forecasting.validation (plan Section 3.6,
Task 7)."""

from __future__ import annotations

import numpy as np
import pytest

from mesoforge.forecasting.validation import WindDerivationError, validate_wind_derivation


class TestValidateWindDerivation:
    def test_accepts_valid_speed_and_direction(self) -> None:
        validate_wind_derivation(
            speed=np.array([1.0, 2.0, 0.0]), direction=np.array([10.0, 350.0, np.nan])
        )

    def test_rejects_negative_speed(self) -> None:
        with pytest.raises(WindDerivationError, match="nonnegative"):
            validate_wind_derivation(speed=np.array([-1.0]), direction=np.array([10.0]))

    def test_rejects_direction_out_of_range(self) -> None:
        with pytest.raises(WindDerivationError, match="0, 360"):
            validate_wind_derivation(speed=np.array([1.0]), direction=np.array([360.0]))

    def test_rejects_defined_direction_at_calm(self) -> None:
        with pytest.raises(WindDerivationError, match="undefined"):
            validate_wind_derivation(speed=np.array([0.0]), direction=np.array([90.0]))
