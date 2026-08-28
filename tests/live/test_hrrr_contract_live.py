"""Opt-in live HRRR provider contract smoke test (plan Task 14).

Guarded by ``MESOFORGE_LIVE_TESTS=1``; skipped before any network
construction otherwise. Never asserts meteorological values, station
availability, byte sizes, or exact record counts -- only that the
provider's URL/selector/key contract still matches what Phase 1 pins.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live

if os.environ.get("MESOFORGE_LIVE_TESTS") != "1":
    pytest.skip(
        "live tests require MESOFORGE_LIVE_TESTS=1 (opt-in only; never run by default CI)",
        allow_module_level=True,
    )


def test_hrrr_live_contract_placeholder() -> None:
    """Placeholder until Task 14 implements the real HRRR live smoke test."""
    pytest.skip("Task 14 implements the real live HRRR contract smoke test")
