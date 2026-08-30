"""Opt-in live HRRR provider contract smoke test (plan Task 14).

Guarded by ``MESOFORGE_LIVE_TESTS=1``; skipped before any network
construction otherwise. Never asserts meteorological values, station
availability, byte sizes, or exact record counts -- only that the
provider's URL/selector/key contract still matches what Phase 1 pins.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("MESOFORGE_LIVE_TESTS") != "1",
        reason="live tests require MESOFORGE_LIVE_TESTS=1 (opt-in only)",
    ),
]


def test_hrrr_live_contract(tmp_path: Path) -> None:
    from mesoforge.catalog.configuration import load_configuration_source
    from mesoforge.guidance.interfaces import HttpResponse
    from mesoforge.guidance.sources.hrrr import (
        build_index_url,
        check_lead_step_type,
        parse_index_rows,
        select_field_row,
    )
    from mesoforge.guidance.sources.hrrr_transport import RequestsHrrrHttpTransport

    raw_cycle = os.environ.get("MESOFORGE_LIVE_HRRR_CYCLE")
    if raw_cycle is None:
        pytest.fail("MESOFORGE_LIVE_HRRR_CYCLE=YYYYMMDDTHH is required")
    cycle = datetime.strptime(raw_cycle, "%Y%m%dT%H")
    config, _ = load_configuration_source(
        base_path=Path("configs/base.yaml"),
        environment_path=Path("configs/phase1-grasston.yaml"),
    )
    assert config.phase1 is not None
    settings = config.phase1.hrrr
    url = build_index_url(
        settings,
        endpoint="aws",
        cycle_date=cycle.date(),
        cycle_hour=cycle.hour,
        forecast_hour=0,
    )
    response = cast(HttpResponse, RequestsHrrrHttpTransport().get(url, timeout=(10.0, 60.0)))
    assert response.status_code == 200
    rows = parse_index_rows(response.content.decode("utf-8"))
    selected = tuple(
        select_field_row(rows, assertion.inventory_selector)
        for assertion in settings.field_assertions
    )
    for row in selected:
        check_lead_step_type(row, forecast_hour=0)
    assert len(selected) == 3
    assert list(tmp_path.iterdir()) == []
