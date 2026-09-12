"""Native newly accumulated snow, native SLR and profile acquisition contracts."""

from datetime import UTC, datetime

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError
from mesoforge.guidance.sources import snowfall_amount as source
from tests.unit.guidance.test_acquisition_v2 import (
    _FakeClock,
    _FakeResponse,
    _FakeSleeper,
    _FakeTransport,
    _grib2_message,
)

CYCLE = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.mark.parametrize("model", ["HRRR", "RAP"])
def test_native_cycle_accumulation_is_not_snowpack_swe_or_an_hourly_amount(model):
    rows = (
        b"1:0:d=2026091112:TMP:2 m above ground:7 hour fcst:\n"
        b"2:100:d=2026091112:SNOD:surface:7 hour fcst:\n"
        b"3:200:d=2026091112:WEASD:surface:6-7 hour acc fcst:\n"
        b"4:300:d=2026091112:ASNOW:surface:6-7 hour acc fcst:\n"
        b"5:400:d=2026091112:ASNOW:surface:0-7 hour acc fcst:\n"
    )
    selected = source.selected_amount_rows(model, CYCLE, 7, rows, 500)
    assert [(name, row.byte_offset, end) for name, row, end in selected] == [("amount", 400, 500)]
    with pytest.raises(GribIndexError, match="unavailable"):
        source.selected_amount_rows(model, CYCLE, 7, rows[: rows.rfind(b"5:")], 500)
    with pytest.raises((GribIndexError, ValueError), match="cycle"):
        source.selected_amount_rows(
            model, CYCLE, 7, rows.replace(b"2026091112", b"2026091106"), 500
        )


@pytest.mark.parametrize("model", ["HRRR", "RAP"])
@pytest.mark.parametrize("lead", [24, 48])
def test_native_day_inventory_preserves_exact_hour_bounds(model, lead):
    cycle = CYCLE.replace(hour=15) if model == "RAP" else CYCLE
    date = cycle.strftime("%Y%m%d%H")
    days = lead // 24
    rows = (
        f"1:0:d={date}:ASNOW:surface:{days - 1}-{days} day acc fcst:\n"
        f"2:100:d={date}:ASNOW:surface:0-{days + 1} day acc fcst:\n"
        f"3:200:d={date}:ASNOW:surface:0-{days} day acc fcst:50% level:\n"
        f"4:300:d={date}:ASNOW:surface:0-{days} day acc fcst:\n"
        f"5:400:d={date}:SNOD:surface:{lead} hour fcst:\n"
    )
    # At f24, the first row is the exact same 0-1 day interval. Replace its
    # identity so this test inventory has one unambiguous native accumulation.
    if lead == 24:
        rows = rows.replace(f"1:0:d={date}:ASNOW:", f"1:0:d={date}:WEASD:")
    selected = source.selected_amount_rows(model, cycle, lead, rows.encode(), 500)
    assert [(name, row.byte_offset, end) for name, row, end in selected] == [("amount", 300, 400)]
    assert selected[0][1].line == f"4:300:d={date}:ASNOW:surface:0-{days} day acc fcst:"
    # No compatible interval may be fabricated from another duration or window.
    absent = rows.replace(f"4:300:d={date}:ASNOW:", f"4:300:d={date}:SNOD:")
    with pytest.raises(GribIndexError, match="unavailable"):
        source.selected_amount_rows(model, cycle, lead, absent.encode(), 500)
    duplicate = rows + f"6:500:d={date}:ASNOW:surface:0-{lead} hour acc fcst:\n"
    with pytest.raises(ValueError, match="Ambiguous"):
        source.selected_amount_rows(model, cycle, lead, duplicate.encode(), 600)


def test_nbm_daily_amount_is_not_substituted_for_the_requested_hour():
    rows = b"1:0:d=2026091112:ASNOW:surface:0-1 day acc fcst:\n"
    with pytest.raises(GribIndexError, match="unavailable"):
        source.selected_amount_rows("NBM", CYCLE, 24, rows, 100)


def test_nbm_uses_hourly_deterministic_amount_and_plain_native_ratio():
    rows = (
        b"1:0:d=2026091112:ASNOW:surface:0-6 hour acc fcst:\n"
        b"2:100:d=2026091112:ASNOW:surface:5-6 hour acc fcst:50% level:\n"
        b"3:200:d=2026091112:ASNOW:surface:5-6 hour acc fcst:prob >0.0254:\n"
        b"4:300:d=2026091112:ASNOW:surface:5-6 hour acc fcst:\n"
        b"5:400:d=2026091112:SNOWLR:surface:6 hour fcst:50% level:\n"
        b"6:500:d=2026091112:SNOWLR:surface:6 hour fcst:\n"
    )
    selected = source.selected_amount_rows("NBM", CYCLE, 6, rows, 600)
    assert [(name, row.byte_offset, end) for name, row, end in selected] == [
        ("amount", 300, 400),
        ("native_slr", 500, 600),
    ]
    ratio_only = b"1:0:d=2026091112:SNOWLR:surface:6 hour fcst:\n"
    assert source.selected_amount_rows("NBM", CYCLE, 6, ratio_only, 100)[0][0] == "native_slr"
    duplicate = rows + b"7:600:d=2026091112:ASNOW:surface:5-6 hour acc fcst:\n"
    with pytest.raises(ValueError, match="Ambiguous"):
        source.selected_amount_rows("NBM", CYCLE, 6, duplicate, 700)


def test_rap_profile_uses_distinct_native_levels_and_physical_wind_boundaries():
    rows = [
        "1:0:d=2026091112:TMP:2 m above ground:7 hour fcst:",
        "2.1:100:d=2026091112:UGRD:10 m above ground:7 hour fcst:",
        "2.2:100:d=2026091112:VGRD:10 m above ground:7 hour fcst:",
        "3:200:d=2026091112:ASNOW:surface:0-7 hour acc fcst:",
        "4:300:d=2026091112:PRES:surface:7 hour fcst:",
    ]
    rows.extend(
        f"{i + 5}:{(i + 4) * 100}:d=2026091112:TMP:{level} mb:7 hour fcst:"
        for i, level in enumerate(range(500, 1001, 25))
    )
    inventory = "\n".join(rows).encode()
    selected = source.selected_amount_rows("RAP", CYCLE, 7, inventory, 2500, include_profile=True)
    assert len(selected) == 24
    amount = next((row.byte_offset, end) for name, row, end in selected if name == "amount")
    assert amount == (200, 300)
    assert set(name for name, _, _ in selected) == {
        "amount",
        "surface_pressure",
        "temperature_2m",
        *(f"temperature_{level}hpa" for level in range(500, 1001, 25)),
    }
    missing = inventory.replace(b":TMP:975 mb:", b":RH:975 mb:")
    result = source.selected_amount_rows("RAP", CYCLE, 7, missing, 2500, include_profile=True)
    assert len(result) == 23
    assert "temperature_975hpa" not in [name for name, _, _ in result]


@pytest.mark.parametrize(
    "model,lead", [("IFS", 7), ("GFS", 7), ("NBM", 0), ("NBM", 37), ("HRRR", 49)]
)
def test_unsupported_products_and_leads_fail_before_network(model, lead):
    with pytest.raises(ValueError):
        source.amount_url(model, CYCLE, lead)
    assert source.SOURCES["IFS"]["supported"] is False
    assert "snowpack" in source.SOURCES["IFS"]["missing_reason"]


@pytest.mark.parametrize("changed", [False, True])
def test_acquisition_preserves_native_fields_and_object_identity(changed):
    url = source.amount_url("NBM", CYCLE, 6)
    payload = _grib2_message(b"s")
    size = len(payload)
    identity = {"ETag": '"fixed"', "Last-Modified": "Fri, 11 Sep 2026 18:00:00 GMT"}
    transport = _FakeTransport()
    transport.head_queue[url] = [_FakeResponse(200, {**identity, "Content-Length": str(2 * size)})]
    index = (
        f"1:0:d=2026091112:ASNOW:surface:5-6 hour acc fcst:\n"
        f"2:{size}:d=2026091112:SNOWLR:surface:6 hour fcst:\n"
    ).encode()
    transport.get_queue[url + ".idx"] = [_FakeResponse(200, identity, index)]
    transport.get_queue[url] = [
        _FakeResponse(
            206,
            {**identity, "Content-Range": f"bytes {i * size}-{(i + 1) * size - 1}/{2 * size}"},
            payload,
        )
        for i in range(2)
    ]
    if changed:
        transport.get_queue[url][-1].headers["ETag"] = '"changed"'

    def acquire():
        return source.acquire_amount_lead(
            "NBM", CYCLE, 6, transport=transport, clock=_FakeClock(CYCLE), sleeper=_FakeSleeper()
        )

    if changed:
        with pytest.raises(FetchError, match="changed ETag"):
            acquire()
    else:
        result = acquire()
        assert [m.canonical_variable_id for m in result.selected_messages] == [
            "amount",
            "native_slr",
        ]
        assert all(m.payload == payload for m in result.selected_messages)
        assert result.index_payload == index and result.full_object_etag == '"fixed"'


def _field(model="HRRR", name="amount", *, changes=None, values=None):
    is_amount = name == "amount"
    attrs = {
        "centre": "kwbc",
        "discipline": 0,
        "parameterCategory": 1,
        "parameterNumber": 29 if is_amount else 233,
        "typeOfLevel": "surface",
        "level": 0,
        "stepType": "accum" if is_amount else "instant",
        "startStep": (6 if model == "NBM" else 0) if is_amount else 7,
        "endStep": 7,
        "stepUnits": 1,
        "dataDate": 20260911,
        "dataTime": 1200,
        "productDefinitionTemplateNumber": 8 if is_amount else 0,
        "typeOfStatisticalProcessing": 1,
        "lengthOfTimeRange": 1 if model == "NBM" else 7,
        "indicatorOfUnitForTimeRange": 1,
        "numberOfMissingInStatisticalProcess": 0,
        "generatingProcessIdentifier": {"HRRR": 83, "RAP": 105, "NBM": 104}[model],
        "gridType": "lambert",
        "Nx": 1799,
        "Ny": 1059,
        "DxInMetres": 3000.0,
        "DyInMetres": 3000.0,
        "LoVInDegrees": 262.5,
        "LaDInDegrees": 38.5,
        "Latin1InDegrees": 38.5,
        "Latin2InDegrees": 38.5,
        "iScansNegatively": 0,
        "jScansPositively": 1,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 0,
        "units": "m" if is_amount else "unknown",
        "modelVersion": "native-test-v1",
        "edition": 2,
        "packingType": "grid_simple",
        "bitsPerValue": 16,
    }
    if model == "RAP":
        attrs.update(
            Nx=451,
            Ny=337,
            DxInMetres=13545.0,
            DyInMetres=13545.0,
            LoVInDegrees=265.0,
            LaDInDegrees=25.0,
            Latin1InDegrees=25.0,
            Latin2InDegrees=25.0,
        )
        if name == "surface_pressure":
            attrs.update(parameterCategory=3, parameterNumber=0, units="Pa")
        elif name.startswith("temperature_"):
            attrs.update(
                parameterCategory=0,
                parameterNumber=0,
                units="K",
                typeOfLevel="heightAboveGround" if name == "temperature_2m" else "isobaricInhPa",
                level=2
                if name == "temperature_2m"
                else int(name.removeprefix("temperature_").removesuffix("hpa")),
            )
    if model == "NBM":
        attrs.update(
            subCentre=14,
            typeOfGeneratingProcess=2,
            radius=6371200.0,
            Nx=2345,
            Ny=1597,
            DxInMetres=2539.703,
            DyInMetres=2539.703,
            LoVInDegrees=265.0,
            LaDInDegrees=25.0,
            Latin1InDegrees=25.0,
            Latin2InDegrees=25.0,
            alternativeRowScanning=1,
        )
    attrs.update(changes or {})
    return xr.DataArray(
        [[0.0, 0.02], [-0.001, np.nan]] if values is None else values,
        dims=("y", "x"),
        name=name,
        coords={
            "time": np.datetime64("2026-09-11T12:00:00"),
            "valid_time": np.datetime64("2026-09-11T19:00:00"),
        },
        attrs={"GRIB_" + k: v for k, v in attrs.items()},
    )


def _decode(monkeypatch, fields, model="HRRR"):
    payloads = {name: _grib2_message(bytes([i + 1])) for i, name in enumerate(fields)}

    def decode(payload, **_):
        name = next(name for name, retained in payloads.items() if retained == payload)
        return [fields[name].to_dataset()]

    monkeypatch.setattr(source, "_decode_all", decode)
    # Reuse the already-tested native projection normalization boundary.
    monkeypatch.setattr(
        source,
        "normalized_native_grid",
        lambda f: (f, np.array([0, 1]), np.array([0, 1]), pyproj.CRS.from_epsg(4326)),
    )
    return source.decode_amount_lead(payloads, model, CYCLE, 7)


@pytest.mark.parametrize(
    "model,param,unit", [("HRRR", 57, "m"), ("RAP", 29, "unknown"), ("NBM", 29, "unknown")]
)
def test_native_amount_semantics_units_missingness_and_offline_replay(
    monkeypatch, model, param, unit
):
    fields = {"amount": _field(model, changes={"parameterNumber": param, "units": unit})}
    data, _, event = _decode(monkeypatch, fields, model)
    assert data.amount.attrs["units"] == "m"
    assert data.amount.values[0, 0] == 0 and data.amount.values[0, 1] == 0.02
    assert np.isnan(data.amount.values[1]).all()
    assert data.native_amount.values[1, 0] == -0.001
    assert event["native_quantity"] == "new_snowfall_amount"
    assert event["native_unit"] == event["declared_native_unit"] == "m"
    assert event["raw_grib_unit"] == unit and event["unit_definition_source"].startswith(
        "https://www.nco.ncep.noaa.gov/"
    )
    assert event["interval_start"] == (
        "2026-09-11T18:00:00Z" if model == "NBM" else "2026-09-11T12:00:00Z"
    )
    assert event["interval_end"] == "2026-09-11T19:00:00Z"
    assert event["invalid_cell_count"] == 1 and event["missing_cell_count"] == 2
    assert event["grib_keys"]["packingType"] == "grid_simple"
    assert event["version"]["model_version"] == "native-test-v1"
    again, _, metadata = _decode(monkeypatch, fields, model)
    xr.testing.assert_identical(data, again)
    assert metadata == event


def test_nbm_ratio_retains_instantaneous_identity_when_amount_unavailable(monkeypatch):
    data, _, event = _decode(
        monkeypatch,
        {"native_slr": _field("NBM", "native_slr", values=[[12, 8], [0, np.nan]])},
        "NBM",
    )
    assert np.isnan(data.amount.values).all()
    assert data.native_slr.attrs["units"] == "1"
    assert event["amount_available"] is False and event["missing_reasons"]
    ratio = event["native_slr"]
    assert ratio["native_unit"] == "unknown" and ratio["declared_native_unit"] == "kg kg-1"
    assert ratio["interval_start"] is ratio["interval_end"] is None
    assert ratio["valid_time"] == "2026-09-11T19:00:00Z" and ratio["source_lead_hours"] == 7


@pytest.mark.parametrize(
    "missing", [None, "temperature_975hpa", "surface_pressure", "temperature_2m"]
)
def test_complete_rap_profile_preserves_all_native_levels_and_missing_reasons(monkeypatch, missing):
    fields = {"amount": _field("RAP")}
    names = [
        "temperature_2m",
        "surface_pressure",
        *(f"temperature_{p}hpa" for p in range(500, 1001, 25)),
    ]
    for name in names:
        if name != missing:
            value = 80000 if name == "surface_pressure" else 270
            fields[name] = _field("RAP", name, values=np.full((2, 2), value))
    data, _, event = _decode(monkeypatch, fields, "RAP")
    profile = event["profile"]
    assert profile["complete"] is (missing is None)
    assert profile["level_hpa"] == list(range(500, 1001, 25))
    assert profile["valid_time"] == event["interval_end"]
    assert profile["source_cycle"] == event["source_cycle"] and profile["source_lead_hours"] == 7
    if missing:
        assert "temperature_profile" not in data
        assert any(missing in reason for reason in profile["missing_reasons"])
    else:
        assert data.temperature_profile.shape == (21, 2, 2)
        assert data.level.values.tolist() == list(range(500, 1001, 25))
        assert data.temperature_profile.attrs["units"] == data.temperature_2m.attrs["units"] == "K"
        assert data.surface_pressure.attrs["units"] == "Pa"
        # Below-ground levels remain native evidence; the scientific calculator owns masking.
        assert data.temperature_profile.sel(level=1000).values[0, 0] == 270


@pytest.mark.parametrize(
    "changes",
    [
        {"parameterNumber": 11},
        {"parameterNumber": 13},
        {"parameterNumber": 233},
        {"units": "kg m**-2"},
        {"units": "m s**-1"},
        {"stepType": "instant"},
        {"startStep": 6},
        {"endStep": 8},
        {"lengthOfTimeRange": 1},
        {"productDefinitionTemplateNumber": 9},
        {"typeOfStatisticalProcessing": 0},
        {"numberOfMissingInStatisticalProcess": 1},
        {"generatingProcessIdentifier": 105},
        {"dataTime": 600},
        {"DxInMetres": 13545.0},
    ],
)
def test_rejects_wrong_native_amount_product_interval_or_identity(monkeypatch, changes):
    with pytest.raises(ValueError):
        _decode(monkeypatch, {"amount": _field(changes=changes)})


@pytest.mark.parametrize(
    "name,changes",
    [
        ("native_slr", {"productDefinitionTemplateNumber": 6}),
        ("native_slr", {"parameterNumber": 29}),
        ("temperature_975hpa", {"level": 1000}),
        ("temperature_2m", {"units": "C"}),
        ("surface_pressure", {"units": "hPa"}),
    ],
)
def test_rejects_ratio_percentiles_or_wrong_profile_semantics(monkeypatch, name, changes):
    model = "NBM" if name == "native_slr" else "RAP"
    with pytest.raises(ValueError):
        _decode(monkeypatch, {name: _field(model, name, changes=changes)}, model)


def test_decoded_time_must_match_native_end_time(monkeypatch):
    field = _field().assign_coords(valid_time=np.datetime64("2026-09-11T18:00:00"))
    with pytest.raises(ValueError, match="decoded time mismatch"):
        _decode(monkeypatch, {"amount": field})


def test_native_ratio_absence_does_not_remove_nbm_amount(monkeypatch):
    data, _, event = _decode(monkeypatch, {"amount": _field("NBM")}, "NBM")
    assert data.amount.values[0, 1] == 0.02
    assert "native_slr" not in data
    assert event["native_slr"]["status"] == "unavailable"
    assert event["native_slr"]["missing_reasons"] == ["Native SNOWLR unavailable"]


def test_rap_amount_survives_inventory_without_optional_t2m():
    rows = (
        b"1.1:0:d=2026091112:UGRD:10 m above ground:7 hour fcst:\n"
        b"1.2:0:d=2026091112:VGRD:10 m above ground:7 hour fcst:\n"
        b"2:100:d=2026091112:ASNOW:surface:0-7 hour acc fcst:\n"
        b"3:200:d=2026091112:TMP:975 mb:7 hour fcst:\n"
    )
    selected = source.selected_amount_rows("RAP", CYCLE, 7, rows, 300, include_profile=True)
    assert [(name, row.byte_offset, end) for name, row, end in selected] == [
        ("amount", 100, 200),
        ("temperature_975hpa", 200, 300),
    ]
    # Existing temperature acquisition must still reject a missing required temperature field.
    with pytest.raises(source.rap.RapTemperatureUnavailableError):
        source.rap.selected_surface_field(
            rows, canonical_variable_id="air_temperature_2m", cycle=CYCLE, forecast_hour=7
        )
