"""Bounded opt-in NBM deterministic-core and PoP01 contract canary."""

from __future__ import annotations

from pathlib import Path

import pytest

from mesoforge.guidance.index_parsing import select_field_row
from mesoforge.guidance.sources.nbm import build_field_selector, build_grib_url, build_index_url
from tests.live.support import (
    BoundedRequestsTransport,
    decode_contract_message,
    fetch_index,
    fetch_rows,
    load_phase2_configuration,
    require_live_cycle,
)

pytestmark = pytest.mark.live


def test_nbm_live_contract(tmp_path: Path) -> None:
    cycle, transport = require_live_cycle("NBM", BoundedRequestsTransport)
    settings = load_phase2_configuration().nbm
    assert (settings.model, settings.product, settings.sector, settings.contract_profile) == (
        "nbm",
        "core",
        "co",
        "nbm-core-conus-operational.v1",
    )
    lead, endpoint = 6, settings.endpoint_order[0]
    kwargs = dict(
        endpoint=endpoint, cycle_date=cycle.date(), cycle_hour=cycle.hour, forecast_hour=lead
    )
    rows = fetch_index(
        transport,
        endpoint=endpoint,
        index_url=build_index_url(settings, **kwargs),
        retry_policy=settings.retry_policy,
        cycle=cycle,
        deadline_minutes=settings.cycle_completion_deadline_minutes,
        destination=tmp_path / "nbm-f006.idx",
    )
    selectors = tuple(
        build_field_selector(c.canonical_variable_id, forecast_hour=lead)
        for c in settings.field_contracts
    )
    assert selectors == (
        ":TMP:2 m above ground:6 hour fcst:$",
        ":DPT:2 m above ground:6 hour fcst:$",
        ":WIND:10 m above ground:6 hour fcst:$",
        ":WDIR:10 m above ground:6 hour fcst:$",
        ":GUST:10 m above ground:6 hour fcst:$",
        r":APCP:surface:5-6 hour acc fcst:$",
        r":APCP:surface:5-6 hour acc fcst:prob >0\.254:prob fcst \d+/\d+$",
    )
    selected = tuple(select_field_row(rows, selector) for selector in selectors)
    paths = fetch_rows(
        transport,
        endpoint=endpoint,
        grib_url=build_grib_url(settings, **kwargs),
        rows=rows,
        selected=selected,
        retry_policy=settings.retry_policy,
        cycle=cycle,
        deadline_minutes=settings.cycle_completion_deadline_minutes,
        destination=tmp_path,
    )
    assert len(paths) == 7
    for contract, path in zip(settings.field_contracts, paths, strict=True):
        attrs = decode_contract_message(path, contract=contract, read_keys=settings.read_keys).attrs
        assert attrs["GRIB_gridType"] == "lambert"
        assert attrs["GRIB_Nx"] > 0 and attrs["GRIB_Ny"] > 0
        assert attrs["GRIB_level"] == contract.level
        assert attrs["GRIB_step"] == lead
        if contract.is_accumulation:
            assert (attrs["GRIB_stepType"], attrs["GRIB_startStep"], attrs["GRIB_endStep"]) == (
                "accum",
                lead - 1,
                lead,
            )
        if contract.is_probability:
            assert attrs["GRIB_units"] == "%"
            assert attrs["GRIB_probabilityType"] is not None
            assert (
                attrs["GRIB_scaledValueOfUpperLimit"],
                attrs["GRIB_scaleFactorOfUpperLimit"],
            ) == (254, 3)
        else:
            assert attrs.get("GRIB_probabilityType") is None
