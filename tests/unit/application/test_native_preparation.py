"""Extended native acquisition contracts without provider calls or fabricated hours."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application.native_discovery import (
    load_native_selection,
    native_window_usable,
    required_native_leads,
    select_native_model_set,
)
from mesoforge.application.native_preparation import difference_native_accumulations
from mesoforge.common.horizon import FIVE_DAY_HORIZON
from mesoforge.guidance.coverage import window_hours
from mesoforge.guidance.sources.native_fields import CLOUD, POP, QPF, selected_native_fields
from tests.support.phase1_fixture_transports import FixedClock, RecordingSleeper
from tests.unit.application.test_current_model_set import MetadataProbe
from tests.unit.application.test_prepared_temperature import phase2_configuration

TARGET = datetime(2026, 9, 11, 10, tzinfo=UTC)


def test_native_plan_keeps_age_brackets_short_expiration_and_gfs_cadence_change():
    cycle = TARGET.replace(hour=6)
    gfs = required_native_leads("GFS", cycle, TARGET, 126)
    assert gfs[0] == 4  # previous bucket, retained rather than invented zero
    assert gfs[-5:] == (120, 123, 126, 129, 132)
    assert 121 not in gfs
    assert max(required_native_leads("HRRR", cycle, TARGET, 126)) == 48
    assert max(required_native_leads("RAP", TARGET.replace(hour=9), TARGET, 126)) == 51
    ifs = required_native_leads("IFS", cycle, TARGET, 126)
    assert ifs[0] == 3 and ifs[-1] == 132
    assert all(lead % 3 == 0 for lead in ifs)
    assert required_native_leads("IFS", cycle, cycle, 126)[:2] == (0, 3)


def test_extended_window_requires_declaration_and_preserves_historical_limits():
    row = {"forecast_horizon": FIVE_DAY_HORIZON.payload(), "horizon_hours": list(range(1, 127))}
    assert window_hours(row) == tuple(range(1, 127))
    with pytest.raises(ValueError):
        window_hours({"horizon_hours": row["horizon_hours"]})
    with pytest.raises(ValueError):
        window_hours({**row, "horizon_hours": list(range(1, 128))})


def test_native_selection_does_not_require_hrrr_rap_to_reach_five_days(tmp_path):
    clock = FixedClock(TARGET + timedelta(minutes=20))
    probe = MetadataProbe(expected_decision=clock.now())
    report = select_native_model_set(
        tmp_path / "selection",
        configuration=phase2_configuration(),
        transport=Mock(spec=[], downloaded_bytes=0),
        clock=clock,
        sleeper=RecordingSleeper(clock),
        probe=probe,
    )
    assert report["status"] == "selected"
    assert report["forecast_horizon"] == FIVE_DAY_HORIZON.payload()
    assert report["horizon_hours"][-1] == 126
    assert set(report["selected_cycles"]) == {"HRRR", "RAP", "GFS", "IFS", "NBM"}
    assert report["models"]["HRRR"]["source_leads"][-1] == 18
    assert report["models"]["RAP"]["source_leads"][-1] == 21
    assert max(report["models"]["GFS"]["source_leads"]) >= 126
    assert report["model_data_acquired"] is False
    assert all(kwargs for kwargs in probe.calls)


@pytest.mark.parametrize(
    "source,success", [("RAP", True), ("IFS", True), ("GFS", True), ("NBM", True)]
)
def test_native_missing_source_is_explicit_without_substitution(tmp_path, source, success):
    clock = FixedClock(TARGET + timedelta(minutes=20))
    probe = MetadataProbe(expected_decision=clock.now())
    probe.missing = lambda model, cycle, lead: model == source
    report = select_native_model_set(
        tmp_path / source,
        configuration=phase2_configuration(),
        transport=Mock(spec=[], downloaded_bytes=0),
        clock=clock,
        sleeper=RecordingSleeper(clock),
        probe=probe,
    )
    assert (report["status"] == "selected") is success
    assert source not in report["selected_cycles"]
    assert report["source_shortfalls"][source]["reason"]


def _index(lead, *descriptors):
    return (
        "\n".join(
            f"{index + 1}:{index * 100}:d=2026091110:{text}"
            for index, text in enumerate(descriptors)
        )
        + "\n"
    ).encode()


def test_nbm_native_interval_selection_does_not_spread_sparse_hourly_amounts():
    payload = _index(
        50,
        "TMP:2 m above ground:50 hour fcst:",
        "APCP:surface:49-50 hour acc fcst:",
        "APCP:surface:44-50 hour acc fcst:",
        "TCDC:surface:50 hour fcst:",
    )
    fields, _ = selected_native_fields("NBM", payload, TARGET, 50, 400)
    amounts = [row for row in fields if row["canonical_variable_id"] == QPF]
    assert len(amounts) == 1 and ":44-50 hour acc fcst:" in amounts[0]["index_row"]
    sparse = _index(51, "TMP:2 m above ground:51 hour fcst:", "APCP:surface:50-51 hour acc fcst:")
    fields, missing = selected_native_fields("NBM", sparse, TARGET, 51, 200)
    assert not any(row["canonical_variable_id"] == QPF for row in fields)
    assert QPF in missing


def test_nbm_hourly_pop_is_not_deterministic_qpf_or_longer_probability():
    payload = _index(
        12,
        "TMP:2 m above ground:12 hour fcst:",
        "APCP:surface:11-12 hour acc fcst:",
        "APCP:surface:11-12 hour acc fcst:prob >0.254:prob fcst 255/255",
        "APCP:surface:6-12 hour acc fcst:prob >0.254:prob fcst 255/255",
    )
    fields, _ = selected_native_fields("NBM", payload, TARGET, 12, 400)
    assert [row["byte_start"] for row in fields if row["canonical_variable_id"] == QPF] == [100]
    assert [row["byte_start"] for row in fields if row["canonical_variable_id"] == POP] == [200]


def test_rap_vector_submessages_keep_both_fields_and_one_physical_range():
    inventory = (
        b"1:0:d=2026091110:TMP:2 m above ground:1 hour fcst:\n"
        b"2.1:100:d=2026091110:UGRD:10 m above ground:1 hour fcst:\n"
        b"2.2:100:d=2026091110:VGRD:10 m above ground:1 hour fcst:\n"
        b"3:200:d=2026091110:APCP:surface:0-1 hour acc fcst:\n"
    )
    fields, missing = selected_native_fields("RAP", inventory, TARGET, 1, 300)
    wind = {
        row["canonical_variable_id"]: row
        for row in fields
        if "wind" in row["canonical_variable_id"]
    }
    assert set(wind) == {"eastward_wind_10m", "northward_wind_10m"}
    assert {row["byte_start"] for row in wind.values()} == {100}
    assert {row["byte_end_exclusive"] for row in wind.values()} == {200}
    assert wind["northward_wind_10m"]["index_row"].startswith("2.2:")
    assert "northward_wind_10m" not in missing


def test_ifs_native_identity_cloud_and_cumulative_tp_are_pinned_separately():
    rows = [
        {
            "domain": "g",
            "class": "od",
            "stream": "oper",
            "type": "fc",
            "levtype": "sfc",
            "step": "132",
            "date": "20260911",
            "time": "0600",
            "expver": "0001",
            "param": param,
            "_offset": index * 100,
            "_length": 100,
        }
        for index, param in enumerate(("2t", "2d", "10u", "10v", "tcc", "tp"))
    ]
    payload = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    fields, missing = selected_native_fields("IFS", payload, TARGET.replace(hour=6), 132, 600)
    assert {row["canonical_variable_id"] for row in fields} >= {
        CLOUD,
        QPF,
        "dew_point_temperature_2m",
    }
    assert "wind_gust_10m" in missing
    with pytest.raises(ValueError, match="mismatch"):
        selected_native_fields(
            "IFS", payload.replace(b'"fc"', b'"pf"'), TARGET.replace(hour=6), 132, 600
        )


def test_native_cumulative_difference_preserves_three_hour_event_and_missingness():
    times = np.array(["2026-09-11T03", "2026-09-11T06", "2026-09-11T09"], dtype="datetime64[ns]")
    native = np.array([[[2.0, 1.0]], [[5.0, 0.5]], [[9.0, np.nan]]])
    bounds = np.stack([np.full(3, np.datetime64("2026-09-11T00", "ns")), times], axis=1)
    dataset = xr.Dataset(
        {
            QPF: (("source_lead_time", "y", "x"), native),
            QPF + "_interval_bounds": (("source_lead_time", "bounds"), bounds),
        },
        coords={"source_lead_time": np.array([3, 6, 9], dtype="timedelta64[h]")},
    )
    info = {
        str(lead): {
            "status": "available",
            "start": "2026-09-11T00:00:00Z",
            "end": f"2026-09-11T{lead:02d}:00:00Z",
            "parents": [{"source_lead_hours": lead}],
        }
        for lead in (3, 6, 9)
    }
    before = dataset.copy(deep=True)
    result = difference_native_accumulations(dataset, info)
    np.testing.assert_array_equal(result[QPF].values[:, 0, 0], [2, 3, 4])
    assert np.isnan(result[QPF].values[1, 0, 1]) and np.isnan(result[QPF].values[2, 0, 1])
    assert info["6"]["start"] == "2026-09-11T03:00:00Z"
    assert [row["source_lead_hours"] for row in info["6"]["parents"]] == [6, 3]
    assert result[QPF + "_interval_bounds"].values[1, 0] == times[0]
    xr.testing.assert_identical(dataset, before)


def test_background_crossing_hour_keeps_full_prospective_view_without_relabeling():
    assert native_window_usable(TARGET, 126, TARGET + timedelta(hours=1, minutes=30))
    assert native_window_usable(TARGET, 126, TARGET + timedelta(hours=6))
    assert not native_window_usable(TARGET, 126, TARGET + timedelta(hours=6, seconds=1))
    with pytest.raises(ValueError, match="timezone"):
        native_window_usable(TARGET, 126, TARGET.replace(tzinfo=None))


def test_discovery_retention_and_conditional_acquisition_work_offline(tmp_path, monkeypatch):
    """Only HTTP and the separately tested GRIB decoder are fixtures here."""
    import re
    from email.utils import format_datetime

    from mesoforge.application import native_preparation
    from mesoforge.guidance.sources.current_availability import probe_temperature
    from tests.support.phase1_fixture_transports import FakeHttpResponse
    from tests.unit.guidance.test_acquisition_v2 import _grib2_message

    clock = FixedClock(TARGET + timedelta(minutes=20))
    cycle = TARGET.replace(hour=6)
    modified = format_datetime(TARGET, usegmt=True)
    body = _grib2_message(b"N")

    class Transport:
        def __init__(self):
            self.range_calls = 0

        def get(self, url, *, headers=None, timeout=None):
            if "gfs." not in url or "20260911/06/" not in url:
                return FakeHttpResponse(404, {})
            lead = int(re.search(r"\.f(\d+)", url)[1])
            metadata = {
                "Last-Modified": modified,
                "ETag": '"native-object"',
                "Content-Length": str(len(body)),
            }
            if url.endswith(".idx"):
                return FakeHttpResponse(
                    200,
                    metadata,
                    f"1:0:d=2026091106:TMP:2 m above ground:{lead} hour fcst:\n".encode(),
                )
            assert headers["If-Match"] == '"native-object"'
            assert headers["Range"] == f"bytes=0-{len(body) - 1}"
            self.range_calls += 1
            return FakeHttpResponse(
                206, {**metadata, "Content-Range": f"bytes 0-{len(body) - 1}/{len(body)}"}, body
            )

        def head(self, url, *, headers=None, timeout=None):
            return FakeHttpResponse(
                200,
                {
                    "Last-Modified": modified,
                    "ETag": '"native-object"',
                    "Content-Length": str(len(body)),
                },
            )

    transport = Transport()
    selection_path = tmp_path / "selection"
    selection = select_native_model_set(
        selection_path,
        configuration=phase2_configuration(),
        transport=transport,
        clock=clock,
        sleeper=RecordingSleeper(clock),
        probe=probe_temperature,
    )
    assert selection["status"] == "selected" and set(selection["selected_cycles"]) == {"GFS"}
    retained, _, probes = load_native_selection(selection_path / "selection.json", clock=clock)
    assert retained == selection

    def normalized(payloads, **kwargs):
        assert payloads["air_temperature_2m"] == body
        lead = kwargs["lead"]
        time = np.datetime64((cycle + timedelta(hours=lead)).replace(tzinfo=None), "ns")
        start = time - np.timedelta64(1, "h")
        data = np.zeros((1, 2, 2))
        ds = xr.Dataset(
            {
                "air_temperature_2m": (
                    ("source_lead_time", "y", "x"),
                    data + 280,
                    {"unit_id": "K"},
                ),
                QPF: (
                    ("source_lead_time", "y", "x"),
                    data + np.nan,
                    {
                        "unit_id": "kg/m^2",
                        "temporal_semantics": "accumulation",
                        "interval_closure": "left_open_right_closed",
                    },
                ),
                QPF + "_interval_bounds": (
                    ("source_lead_time", "bounds"),
                    np.array([[start, time]]),
                ),
            },
            coords={
                "source_lead_time": np.array([lead], dtype="timedelta64[h]"),
                "forecast_reference_time": np.datetime64(cycle.replace(tzinfo=None), "ns"),
                "source_valid_time": ("source_lead_time", np.array([time])),
                "x": [-94, -92],
                "y": [45, 47],
            },
            attrs={
                "model": "GFS",
                "data_kind": "real_prepared_guidance",
                "target_reference_time": TARGET.isoformat(),
                "crs_wkt2": pyproj.CRS.from_epsg(4326).to_wkt(),
                "field_missing_reasons_json": "{}",
                "wind_rotation_policy_json": "{}",
            },
        )
        return ds, {"status": "unavailable", "start": str(start), "end": str(time), "parents": []}

    monkeypatch.setattr(native_preparation, "normalize_native_frame", normalized)
    report = native_preparation.prepare_native_selected(
        [{"lat": 45.80268, "lon": -93.07952}],
        selection_path / "selection.json",
        tmp_path / "prepared",
        transport=transport,
        clock=clock,
        sleeper=RecordingSleeper(clock),
    )
    assert transport.range_calls == len(probes)
    source = tmp_path / "prepared/control/source"
    manifest = json.loads((source / "manifest.json").read_bytes())
    assert manifest["forecast_horizon"] == FIVE_DAY_HORIZON.payload()
    assert set(manifest["prepared_files"]) == {"GFS"}
    assert len(manifest["inputs"]) == len(probes)
    assert report["retained_raw_bytes"] == len(probes) * len(body)
    for record in manifest["inputs"]:
        assert (source / record["raw_file"]).read_bytes() == body
        assert record["grib_available_at"] == "2026-09-11T10:00:00Z"
    with xr.open_dataset(source / "GFS.nc", engine="h5netcdf") as ds:
        assert ds.source_lead_time.values[-1] == np.timedelta64(132, "h")
    # Exercise the production real-manifest loader, source proof and native point
    # path, not just the writer. Absent optional sources must not require HRRR.
    from mesoforge.application.prepared_snapshot import load_preparation
    from mesoforge.application.refresh_guidance import _validate

    prepared = load_preparation(report)
    column = prepared.reference_view(TARGET).point_column(latitude=45.80268, longitude=-93.07952)
    assert len(column["hours"]) == 120
    assert column["hours"][-1]["valid_time"] == "2026-09-16T10:00:00Z"
    assert (
        _validate(report, [{"lat": 45.80268, "lon": -93.07952}])["point_columns"][0]["hours"] == 120
    )
    from mesoforge.application.prepared_snapshot import (
        build_snapshot_manifest,
        check_information_cutoff,
        coverage_for,
    )
    from mesoforge.common.identifiers import PreparedSnapshotId

    snapshot = build_snapshot_manifest(
        snapshot_id=PreparedSnapshotId("native-fixture"),
        root=tmp_path,
        preparation_path=tmp_path / "prepared/preparation.json",
        selection_path=selection_path / "selection.json",
        steps=[],
        downloaded_bytes=report["downloaded_bytes"],
        clock=clock.now(),
        completed_at=clock.now(),
    )
    assert snapshot["source_information"]["status"] == "complete"
    assert not check_information_cutoff(
        snapshot["source_information"],
        analysis_cutoff=clock.now(),
        published_at=clock.now().isoformat(),
        completed_at=clock.now().isoformat(),
    )
    assert coverage_for(snapshot, TARGET)["usable"]
    assert snapshot["contributors"]["GFS"]["cycle"] == "2026-09-11T06:00:00Z"
    assert snapshot["contributors"]["NBM"]["cycle"] is None
    # Tampered metadata cannot redirect a retained temperature byte range.
    bad = json.loads((selection_path / "selection.json").read_bytes())
    bad["models"]["GFS"]["candidates"][-1]["probes"][0]["selected_message"]["byte_start"] = 1
    (selection_path / "selection.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="temperature differs"):
        load_native_selection(selection_path / "selection.json", clock=clock)


@pytest.mark.parametrize("model", ["RAP", "IFS", "GFS"])
def test_native_acquisition_preserves_provider_request_pacing(tmp_path, monkeypatch, model):
    from mesoforge.application import native_preparation
    from tests.unit.guidance.test_acquisition_v2 import _grib2_message
    from tests.unit.guidance.test_current_availability import DECISION, MetadataTransport, probe
    from tests.unit.guidance.test_selected_objects import AcquisitionTransport

    evidence = probe(MetadataTransport(model), model).evidence
    clock = FixedClock(DECISION + timedelta(minutes=2))
    sleeper = RecordingSleeper(clock)
    transport = AcquisitionTransport(model)
    transport.ranged.content = _grib2_message(b"N", total_length=80)
    observed_times = []
    original_get, original_head = transport.get, transport.head

    def get(*args, **kwargs):
        observed_times.append(clock.now())
        return original_get(*args, **kwargs)

    def head(*args, **kwargs):
        observed_times.append(clock.now())
        return original_head(*args, **kwargs)

    monkeypatch.setattr(transport, "get", get)
    monkeypatch.setattr(transport, "head", head)
    monkeypatch.setattr(
        native_preparation,
        "load_native_selection",
        lambda *args, **kwargs: (
            {"decision_time": DECISION.isoformat(), "target_reference_time": DECISION.isoformat()},
            phase2_configuration(),
            [evidence],
        ),
    )

    def finish_after_acquisition(*args, **kwargs):
        raise RuntimeError("stop after bounded acquisition")

    monkeypatch.setattr(native_preparation, "normalize_native_frame", finish_after_acquisition)
    with pytest.raises(RuntimeError, match="stop after bounded acquisition"):
        native_preparation.prepare_native_selected(
            [{"lat": 45.80268, "lon": -93.07952}],
            tmp_path / "selection.json",
            tmp_path / "prepared",
            transport=transport,
            clock=clock,
            sleeper=sleeper,
        )
    interval = 0.5 if model in {"RAP", "IFS"} else 0
    assert sleeper.sleeps == ([interval] * 3 if interval else [])
    assert observed_times == [
        DECISION + timedelta(minutes=2, seconds=interval * index) for index in range(1, 4)
    ]
    assert [call[0] for call in transport.calls] == ["GET", "HEAD", "GET"]


def test_native_frame_keeps_event_bounds_units_nbm_vector_conversion_and_zero(monkeypatch):
    from mesoforge.application import native_preparation
    from mesoforge.catalog.domains import BoundingBox
    from tests.unit.application.test_prepared_shadow import CYCLE, frame

    lead = 126
    values = {
        "air_temperature_2m": 280.0,
        "dew_point_temperature_2m": 275.0,
        "wind_speed_10m": 4.0,
        "wind_from_direction_10m": 90.0,
        "wind_gust_10m": 7.0,
        POP: 40.0,
    }

    def state(payload, model, variable, cycle, source_lead, configuration):
        assert model == "NBM" and source_lead == lead and cycle == CYCLE
        field = frame(lead)
        field.values[:] = values[variable]
        return field

    amount = frame(lead)
    amount.values[:] = 2.5
    amount.values[0, 0] = 0
    amount.attrs["GRIB_units"] = "kg m**-2"
    monkeypatch.setattr(native_preparation, "decode_state", state)
    monkeypatch.setattr(
        native_preparation, "decode_interval", lambda *args: (amount, 120, 126, 1.0)
    )
    payloads = {field: b"already-validated-fixture" for field in (*values, QPF)}
    dataset, info = native_preparation.normalize_native_frame(
        payloads,
        model="NBM",
        cycle=CYCLE,
        lead=lead,
        target=CYCLE,
        configuration=phase2_configuration(),
        area=BoundingBox(south=45.0, north=45.6, west=-94.0, east=-93.4),
    )
    assert float(dataset.eastward_wind_10m.values[0, 0, 0]) == -4
    assert abs(float(dataset.northward_wind_10m.values[0, 0, 0])) < 1e-15
    assert np.all(dataset[POP].values == 0.4)
    assert dataset[POP].attrs["probability_comparison"] == "gt"
    assert dataset[QPF].attrs["unit_id"] == "kg/m^2"
    assert info["native_end_step"] - info["native_start_step"] == 6
    assert dataset[QPF + "_interval_bounds"].values[0, 1] - dataset[
        QPF + "_interval_bounds"
    ].values[0, 0] == np.timedelta64(6, "h")
    assert np.isnan(dataset[CLOUD].values).all()


def test_native_crop_releases_full_grid_backing_array(monkeypatch):
    from mesoforge.application import native_preparation
    from mesoforge.catalog.domains import BoundingBox

    field = xr.DataArray(np.ones((20, 20), dtype=np.float64), dims=("y", "x"))
    x, y = np.arange(-103, -83, dtype=float), np.arange(35, 55, dtype=float)
    crs = pyproj.CRS.from_epsg(4326)
    monkeypatch.setattr(native_preparation, "normalized_native_grid", lambda _: (field, x, y, crs))
    crop, _, _, _ = native_preparation._crop(
        field, BoundingBox(west=-94, east=-92, south=44, north=46)
    )
    assert crop.size < field.size
    assert not np.shares_memory(crop, field.values)


@pytest.mark.parametrize("earth_shape,radius", [(6, 6371229), (1, 6371000)])
def test_hrrr_native_decode_retains_encoded_earth_figure_for_crop(earth_shape, radius):
    import eccodes

    from mesoforge.application.native_preparation import _crop
    from mesoforge.catalog.domains import BoundingBox
    from mesoforge.guidance.sources.native_fields import TEMPERATURE, decode_state
    from tests.fixtures import hrrr_grib

    configuration = phase2_configuration()
    assert "radius" not in configuration.hrrr.read_keys  # Legacy configuration remains unchanged.
    payload = hrrr_grib.make_temperature_message(
        forecast_hour=2,
        values_k=np.full((hrrr_grib.NY, hrrr_grib.NX), 280.0),
        cycle_date=TARGET.strftime("%Y%m%d"),
        cycle_hour=TARGET.hour,
    )
    message = eccodes.codes_new_from_message(payload)
    try:
        eccodes.codes_set(message, "jScansPositively", 1)
        eccodes.codes_set(message, "shapeOfTheEarth", earth_shape)
        if earth_shape == 1:
            eccodes.codes_set(message, "scaleFactorOfRadiusOfSphericalEarth", 0)
            eccodes.codes_set(message, "scaledValueOfRadiusOfSphericalEarth", radius)
        payload = bytes(eccodes.codes_get_message(message))
    finally:
        eccodes.codes_release(message)
    field = decode_state(payload, "HRRR", TEMPERATURE, TARGET, 2, configuration)
    assert field.attrs["GRIB_shapeOfTheEarth"] == earth_shape
    assert field.attrs["GRIB_radius"] == radius
    values, x, y, crs = _crop(field, BoundingBox(south=45.7, north=45.9, west=-93.2, east=-92.9))
    assert values.shape == (len(y), len(x))
    assert np.all(values == 280.0)
    assert crs.ellipsoid.semi_major_metre == radius


@pytest.mark.parametrize(
    "change",
    [
        {"GRIB_lengthOfTimeRange": 1},
        {"GRIB_units": "in"},
        {"GRIB_probabilityType": 1},
        {"GRIB_dataTime": 600},
        {"GRIB_startStep": 5},
    ],
)
def test_native_interval_decoder_rejects_contradictory_event_metadata(monkeypatch, change):
    from mesoforge.guidance.sources import native_fields
    from tests.unit.guidance.test_acquisition_v2 import _grib2_message

    cycle = TARGET.replace(hour=0)
    attrs = {
        "GRIB_centre": "kwbc",
        "GRIB_stepType": "accum",
        "GRIB_stepUnits": 1,
        "GRIB_startStep": 0,
        "GRIB_endStep": 6,
        "GRIB_typeOfLevel": "surface",
        "GRIB_level": 0,
        "GRIB_dataDate": 20260911,
        "GRIB_dataTime": 0,
        "GRIB_typeOfStatisticalProcessing": 1,
        "GRIB_discipline": 0,
        "GRIB_parameterCategory": 1,
        "GRIB_parameterNumber": 8,
        "GRIB_generatingProcessIdentifier": 96,
        "GRIB_productDefinitionTemplateNumber": 8,
        "GRIB_lengthOfTimeRange": 6,
        "GRIB_indicatorOfUnitForTimeRange": 1,
        "GRIB_units": "kg m**-2",
    }

    def decoded():
        return xr.Dataset(
            {"tp": (("y", "x"), np.ones((2, 2)), attrs)},
            coords={
                "time": np.datetime64("2026-09-11T00", "ns"),
                "valid_time": np.datetime64("2026-09-11T06", "ns"),
            },
        )

    monkeypatch.setattr(native_fields, "_decode_all", lambda *args, **kwargs: [decoded()])
    field, start, end, factor = native_fields.decode_interval(_grib2_message(b"Q"), "GFS", cycle, 6)
    assert (start, end, factor) == (0, 6, 1.0) and float(field.values[0, 0]) == 1
    attrs.update(change)
    with pytest.raises(ValueError):
        native_fields.decode_interval(_grib2_message(b"Q"), "GFS", cycle, 6)
