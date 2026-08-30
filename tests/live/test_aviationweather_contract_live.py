"""Opt-in live AviationWeather.gov provider contract smoke test (plan Task 14).

Guarded by ``MESOFORGE_LIVE_TESTS=1``; skipped before any network
construction otherwise.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
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


def test_aviationweather_live_contract(tmp_path: Path) -> None:
    from mesoforge.catalog.configuration import load_configuration_source
    from mesoforge.guidance.interfaces import HttpResponse
    from mesoforge.observations.sources.aviationweather import (
        RequestsAviationWeatherHttpTransport,
        build_metar_url,
        request_headers,
    )

    config, _ = load_configuration_source(
        base_path=Path("configs/base.yaml"),
        environment_path=Path("configs/phase1-grasston.yaml"),
    )
    assert config.phase1 is not None
    settings = config.phase1.aviationweather
    ids = ("KCBG", "KJMR", "KROS")
    url = build_metar_url(settings, station_ids=ids, query_date=datetime.now(UTC))
    response = cast(
        HttpResponse,
        RequestsAviationWeatherHttpTransport().get(
            url, headers=request_headers(settings), timeout=(10.0, 60.0)
        ),
    )
    assert response.status_code in {200, 204}
    if response.status_code == 200:
        records = json.loads(response.content)
        assert isinstance(records, list)
        required = {
            "icaoId",
            "obsTime",
            "reportTime",
            "receiptTime",
            "temp",
            "wdir",
            "wspd",
            "qcField",
            "metarType",
            "rawOb",
            "lat",
            "lon",
            "elev",
        }
        for record in records:
            assert required <= record.keys()
            assert record["icaoId"] in ids
    assert list(tmp_path.iterdir()) == []
