"""Bounded opt-in GFS pgrb2 0.25-degree contract canary."""

from __future__ import annotations

from pathlib import Path

import pytest

from mesoforge.guidance.index_parsing import select_field_row, select_field_rows
from mesoforge.guidance.precipitation import (
    ApcpCandidateRecord,
    compute_one_hour_qpf,
    select_bucket_record,
    validate_dual_parent_equivalence,
)
from mesoforge.guidance.sources.gfs import build_field_selector, build_grib_url, build_index_url
from tests.live.support import (
    BoundedRequestsTransport,
    decode_contract_message,
    fetch_index,
    fetch_rows,
    load_phase2_configuration,
    require_live_cycle,
)

pytestmark = pytest.mark.live


def test_gfs_live_contract(tmp_path: Path) -> None:
    cycle, transport = require_live_cycle("GFS", BoundedRequestsTransport)
    settings = load_phase2_configuration().gfs
    assert (settings.model, settings.product_profile) == ("gfs", "gfs-pgrb2-0p25.v1")
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
        destination=tmp_path / "gfs-f006.idx",
    )
    contracts = {c.canonical_variable_id: c for c in settings.field_contracts}
    selectors = {name: build_field_selector(name, forecast_hour=lead) for name in contracts}
    qpf_id = "liquid_equivalent_precipitation_amount_1h"
    assert selectors == {
        "air_temperature_2m": ":TMP:2 m above ground:6 hour fcst:$",
        "dew_point_temperature_2m": ":DPT:2 m above ground:6 hour fcst:$",
        "eastward_wind_10m": ":UGRD:10 m above ground:6 hour fcst:$",
        "northward_wind_10m": ":VGRD:10 m above ground:6 hour fcst:$",
        "wind_gust_10m": ":GUST:surface:6 hour fcst:$",
        qpf_id: r":APCP:surface:0-6 hour acc fcst:$",
    }
    instant_rows = tuple(
        select_field_row(rows, selectors[name]) for name in contracts if name != qpf_id
    )
    apcp_rows = select_field_rows(rows, selectors[qpf_id])
    assert len(apcp_rows) in {1, 2}
    paths = fetch_rows(
        transport,
        endpoint=endpoint,
        grib_url=build_grib_url(settings, **kwargs),
        rows=rows,
        selected=instant_rows + apcp_rows,
        retry_policy=settings.retry_policy,
        cycle=cycle,
        deadline_minutes=settings.cycle_completion_deadline_minutes,
        destination=tmp_path,
    )
    instantaneous = [c for c in settings.field_contracts if not c.is_accumulation]
    for contract, path in zip(instantaneous, paths[:5], strict=True):
        attrs = decode_contract_message(path, contract=contract, read_keys=settings.read_keys).attrs
        assert attrs["GRIB_gridType"] == "regular_ll"
        assert attrs["GRIB_Ni"] > 0 and attrs["GRIB_Nj"] > 0
        assert attrs["GRIB_level"] == contract.level
        assert attrs["GRIB_step"] == lead
    candidates = []
    for path in paths[5:]:
        array = decode_contract_message(
            path, contract=contracts[qpf_id], read_keys=settings.read_keys
        )
        attrs = array.attrs
        assert attrs["GRIB_units"] in {"kg m**-2", "kg m-2"}
        assert attrs["GRIB_stepType"] == "accum"
        candidates.append(
            ApcpCandidateRecord(
                start_step=int(attrs["GRIB_startStep"]),
                end_step=int(attrs["GRIB_endStep"]),
                is_accumulation=True,
                values_kg_m2=tuple(float(v) for v in array.values.flat),
            )
        )
    bucket = select_bucket_record(tuple(candidates), forecast_hour=lead)
    assert (bucket.start_step, bucket.end_step) == (0, 6)
    if len(candidates) == 2:
        other = next(candidate for candidate in candidates if candidate is not bucket)
        assert validate_dual_parent_equivalence(bucket, other)

    # f006 is not a reset hour, so its Phase 2 one-hour product must be
    # differenced from f005 in the same six-hour bucket. This is the minimum
    # additional product needed to canary that live contract.
    previous_kwargs = {**kwargs, "forecast_hour": lead - 1}
    previous_rows = fetch_index(
        transport,
        endpoint=endpoint,
        index_url=build_index_url(settings, **previous_kwargs),
        retry_policy=settings.retry_policy,
        cycle=cycle,
        deadline_minutes=settings.cycle_completion_deadline_minutes,
        destination=tmp_path / "gfs-f005.idx",
    )
    previous_selector = build_field_selector(qpf_id, forecast_hour=lead - 1)
    assert previous_selector == r":APCP:surface:0-5 hour acc fcst:$"
    previous_apcp_rows = select_field_rows(previous_rows, previous_selector)
    previous_paths = fetch_rows(
        transport,
        endpoint=endpoint,
        grib_url=build_grib_url(settings, **previous_kwargs),
        rows=previous_rows,
        selected=previous_apcp_rows,
        retry_policy=settings.retry_policy,
        cycle=cycle,
        deadline_minutes=settings.cycle_completion_deadline_minutes,
        destination=tmp_path / "previous",
    )
    previous_candidates = []
    for path in previous_paths:
        array = decode_contract_message(
            path, contract=contracts[qpf_id], read_keys=settings.read_keys
        )
        attrs = array.attrs
        previous_candidates.append(
            ApcpCandidateRecord(
                start_step=int(attrs["GRIB_startStep"]),
                end_step=int(attrs["GRIB_endStep"]),
                is_accumulation=attrs["GRIB_stepType"] == "accum",
                values_kg_m2=tuple(float(value) for value in array.values.flat),
            )
        )
    previous_bucket = select_bucket_record(tuple(previous_candidates), forecast_hour=lead - 1)
    assert len(bucket.values_kg_m2) == len(previous_bucket.values_kg_m2)
    results = tuple(
        compute_one_hour_qpf(
            forecast_hour=lead,
            bucket_value_current_kg_m2=current,
            bucket_value_previous_kg_m2=previous,
        )
        for current, previous in zip(bucket.values_kg_m2, previous_bucket.values_kg_m2, strict=True)
    )
    assert results
    assert all(not result.is_reset_passthrough for result in results)
