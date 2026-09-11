"""IFS adapter wiring reuses the existing shadow preparation lifecycle and fixtures."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
import xarray as xr

from mesoforge.application import prepared_ifs
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.application.prepared_shadow import normalize_shadow_temperature
from mesoforge.application.prepared_temperature import _write_prepared_file
from mesoforge.guidance.sources.ifs import NO_NATIVE_GUIDANCE, IfsCycleSelection
from tests.unit.application.test_prepared_rap import _acquired
from tests.unit.application.test_prepared_shadow import geographic_frame
from tests.unit.application.test_prepared_temperature import (
    EXTENDED_HORIZONS,
    prepare_fixture_guidance,
)

CYCLE = datetime(2026, 8, 30, 6, tzinfo=UTC)
TARGET = CYCLE + timedelta(hours=7)
NATIVE = tuple(range(9, 43, 3))
LOCATIONS = [{"lat": 36.7378, "lon": -119.7871}, {"lat": 35.7796, "lon": -78.6382}]


def test_ifs_prepares_only_native_leads_once_and_reuses_rebuilds_offline(tmp_path, monkeypatch):
    control = tmp_path / "control"
    control.mkdir()
    (control / "manifest.json").write_text(
        json.dumps(
            {
                "data_kind": "real_prepared_guidance",
                "target_reference_time": TARGET.isoformat(),
                "target_horizon_hours": list(range(1, 37)),
            }
        )
    )
    missing = {h: NO_NATIVE_GUIDANCE for h in range(1, 37) if h + 7 not in NATIVE}
    discover = Mock(
        return_value=IfsCycleSelection(
            CYCLE, tuple(range(8, 44)), NATIVE, NATIVE, missing, (), "fixture"
        )
    )

    def acquire(**kwargs):
        return replace(
            _acquired(**kwargs), model="ifs", cycle_date=CYCLE.date(), cycle_hour=CYCLE.hour
        )

    acquire_mock = Mock(side_effect=acquire)
    monkeypatch.setattr(prepared_ifs, "discover_ifs_cycle", discover)
    monkeypatch.setattr(prepared_ifs, "acquire_ifs_lead", acquire_mock)
    monkeypatch.setattr(prepared_ifs, "decode_temperature_message", lambda payload, **kw: payload)
    monkeypatch.setattr(
        prepared_ifs,
        "normalize_shadow_temperature",
        lambda decoded, **kw: xr.Dataset(
            {"fixture_temperature": ("lead", np.array(sorted(decoded)) + 270.0)}
        ),
    )
    monkeypatch.setattr(prepared_ifs, "_identity", lambda: {"synthetic_test": True})
    output = tmp_path / "ifs"
    report = prepared_ifs.prepare_ifs(LOCATIONS, control, output, transport=Mock(spec=[]))
    assert report["supported_hours"] == list(range(2, 36, 3))
    assert report["missing_hours"] == missing
    assert acquire_mock.call_count == 12 and discover.call_count == 1
    assert len(report["regions"]) == 2
    manifest = json.loads((Path(report["source_directory"]) / "manifest.json").read_text())
    assert len(manifest["inputs"]) == 12
    assert all(
        row["model"] == "IFS" and row["source_lead_hours"] % 3 == 0 for row in manifest["inputs"]
    )
    assert "ECMWF" in manifest["source_metadata"]["capabilities"]["attribution"]
    assert manifest["source_metadata"]["capabilities"]["licence"] == "CC-BY-4.0"
    configuration = json.loads((output / "contributors.json").read_text())
    assert {m["model_id"]: m["status"] for m in configuration["models"]} == {
        "HRRR": "active",
        "GFS": "active",
        "RAP": "shadow",
        "IFS": "shadow",
    }
    monkeypatch.setattr(
        prepared_ifs, "BoundedHttpTransport", Mock(side_effect=AssertionError("network"))
    )
    discover.side_effect = acquire_mock.side_effect = AssertionError("provider access")
    repeated = prepared_ifs.prepare_ifs(LOCATIONS, control, output)
    rebuilt = prepared_ifs.prepare_ifs(LOCATIONS, control, tmp_path / "rebuilt", from_raw=output)
    assert repeated["downloaded_bytes"] == rebuilt["downloaded_bytes"] == 0
    assert rebuilt["supported_hours"] == report["supported_hours"]
    for a, b in zip(report["regions"], rebuilt["regions"], strict=True):
        with (
            xr.open_dataset(Path(a["directory"]) / "IFS.nc", engine="h5netcdf") as left,
            xr.open_dataset(Path(b["directory"]) / "IFS.nc", engine="h5netcdf") as right,
        ):
            xr.testing.assert_identical(left, right)
    # A different adapter's empty manifest must not masquerade as reusable IFS.
    path = Path(report["source_directory"]) / "manifest.json"
    manifest["source_metadata"]["model_definition"]["model_id"] = "RAP"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Existing IFS snapshot differs"):
        prepared_ifs.prepare_ifs(LOCATIONS, control, output)


def test_sparse_ifs_point_values_preserve_all_control_hours_and_do_no_request_io(
    tmp_path, monkeypatch
):
    control = tmp_path / "control"
    prepare_fixture_guidance(control, EXTENDED_HORIZONS)
    before = PreparedPointForecast.from_directory(control).forecast(latitude=45.8, longitude=-93.1)
    cycle = datetime(2026, 8, 30, 12, tzinfo=UTC)
    decoded = {}
    for lead in range(3, 37, 3):
        frame = geographic_frame(lead)
        time = np.datetime64(cycle.replace(tzinfo=None), "ns")
        decoded[lead] = frame.assign_coords(
            time=time, step=np.timedelta64(lead, "h"), valid_time=time + np.timedelta64(lead, "h")
        )
    dataset = normalize_shadow_temperature(decoded, model="IFS", cycle=cycle, target=cycle)
    # Synthetic geometry/value fixture; real raw-manifest readback is demonstrated separately.
    dataset.attrs["data_kind"] = "synthetic_demonstration"
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    _write_prepared_file(shadow, "IFS", dataset)
    reader = PreparedPointForecast.from_directory(
        control, configuration=prepared_ifs.IFS_CONFIGURATION, shadow_directories={"IFS": shadow}
    )
    forbidden = Mock(side_effect=AssertionError("Request attempted guidance I/O"))
    monkeypatch.setattr(xr, "open_dataset", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    result = reader.forecast(latitude=45.8, longitude=-93.1)
    assert reader.forecast(latitude=45.8, longitude=-93.1) == result
    for original, hour in zip(before["hours"], result["hours"], strict=True):
        assert {k: v for k, v in hour.items() if k != "shadow_sources"} == original
        source = next(s for s in hour["shadow_sources"] if s["model"] == "IFS")
        h = hour["horizon_hours"]
        assert source["weight"] == 0
        if h % 3:
            assert source["temperature"]["value"] is None
            assert source["missing_reasons"] == ["IFS: no guidance for this valid time"]
        else:
            # Independent linear-field interpolation:270+lead+.5*(45.8-45)+.2*(-93.1+93).
            assert source["temperature"]["value"] == pytest.approx(270.38 + h, abs=1e-10)
            assert source["source_lead_hours"] == h
    forbidden.assert_not_called()
