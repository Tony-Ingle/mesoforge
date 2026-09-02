"""Unit tests for mesoforge.forecasting.pop_blend (plan Section 4.5,
Task 9): NBM-only PoP passthrough, no synthesis from deterministic
precipitation.
"""

from __future__ import annotations

import pytest

from mesoforge.forecasting.pop_blend import PopBlendError, pop_passthrough


class TestPopPassthrough:
    def test_passes_through_unchanged(self) -> None:
        assert pop_passthrough(0.45) == 0.45

    def test_zero_and_one_are_valid(self) -> None:
        assert pop_passthrough(0.0) == 0.0
        assert pop_passthrough(1.0) == 1.0

    def test_rejects_out_of_range(self) -> None:
        with pytest.raises(PopBlendError, match="0, 1"):
            pop_passthrough(1.5)

    def test_rejects_negative(self) -> None:
        with pytest.raises(PopBlendError, match="0, 1"):
            pop_passthrough(-0.1)

    def test_rejects_nan(self) -> None:
        with pytest.raises(PopBlendError, match="finite"):
            pop_passthrough(float("nan"))

    def test_signature_accepts_exactly_one_input(self) -> None:
        """No deterministic-precipitation argument exists on this
        function at all -- PoP can never be synthesized from QPF by
        construction."""
        import inspect

        sig = inspect.signature(pop_passthrough)
        assert list(sig.parameters) == ["nbm_pop_fraction"]
