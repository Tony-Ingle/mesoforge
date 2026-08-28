"""Opt-in live AviationWeather.gov provider contract smoke test (plan Task 14).

Guarded by ``MESOFORGE_LIVE_TESTS=1``; skipped before any network
construction otherwise.
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


def test_aviationweather_live_contract_placeholder() -> None:
    """Placeholder until Task 14 implements the real METAR live smoke test."""
    pytest.skip("Task 14 implements the real live AviationWeather contract smoke test")
