"""Focused shadow orchestration checks; generated messages are not real weather data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import numpy as np
import pytest
import xarray as xr

from mesoforge.application import prepared_rap
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition, SelectedMessage
from mesoforge.guidance.index_parsing import IndexRow
from tests.unit.application.test_prepared_temperature import FixtureClock, FixtureSleeper

TARGET = datetime(2026, 8, 30, 12, tzinfo=UTC)
CYCLE = TARGET - timedelta(hours=3)
LOCATIONS = [{"lat": 36.7378, "lon": -119.7871}, {"lat": 35.7796, "lon": -78.6382}]


@dataclass
class _Selection:
    cycle: datetime | None
    source_leads: tuple[int, ...]
    available_leads: tuple[int, ...]
    missing_hours: dict[int, str]


def _acquired(*, forecast_hour: int, **kwargs: Any) -> Phase2LeadAcquisition:
    payload = f"generated RAP lead {forecast_hour}".encode()
    index = f"1:0:d=2026083009:TMP:2 m above ground:{forecast_hour} hour fcst:"
    return Phase2LeadAcquisition(
        model="rap",
        cycle_date=CYCLE.date(),
        cycle_hour=CYCLE.hour,
        forecast_hour=forecast_hour,
        endpoint="fixture",
        resolved_grib_url=f"https://fixture.invalid/RAP-f{forecast_hour}.grib2",
        resolved_index_url=f"https://fixture.invalid/RAP-f{forecast_hour}.grib2.idx",
        index_payload=index.encode(),
        index_attempts=(),
        index_completed_at=TARGET,
        selected_messages=(
            SelectedMessage("air_temperature_2m", IndexRow(1, 0, index), 0, len(payload), payload),
        ),
        grib_attempts=(),
        grib_completed_at=TARGET,
        full_object_etag=None,
        full_object_last_modified=None,
        full_object_content_length=len(payload),
        index_available_at=TARGET,
        grib_available_at=TARGET,
    )


@pytest.fixture
def preparation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Mock, Mock]:
    control = tmp_path / "control"
    control.mkdir()
    (control / "manifest.json").write_text(
        json.dumps(
            {
                "data_kind": "real_prepared_guidance",
                "target_reference_time": "2026-08-30T12:00:00Z",
                "target_horizon_hours": list(range(1, 37)),
            }
        )
    )
    discover = Mock(return_value=_Selection(CYCLE, tuple(range(4, 40)), tuple(range(4, 40)), {}))
    acquire = Mock(side_effect=_acquired)
    monkeypatch.setattr(prepared_rap, "discover_rap_cycle", discover)
    monkeypatch.setattr(prepared_rap, "acquire_rap_lead", acquire)
    monkeypatch.setattr(prepared_rap, "decode_temperature_message", lambda payload, **kw: payload)

    def normalize(decoded: dict[int, bytes], **kw: Any) -> xr.Dataset:
        leads = sorted(decoded)
        return xr.Dataset({"temperature": ("lead", np.array([270.0 + lead for lead in leads]))})

    monkeypatch.setattr(prepared_rap, "normalize_shadow_temperature", normalize)
    monkeypatch.setattr(prepared_rap, "_identity", lambda: {"synthetic_test": True})
    return control, discover, acquire


def test_one_acquisition_shared_across_regions_repeat_and_offline_rebuild(
    tmp_path: Path, preparation: tuple[Path, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    control, discover, acquire = preparation
    original_control = (control / "manifest.json").read_bytes()
    output = tmp_path / "rap"
    report = prepared_rap.prepare_rap(
        LOCATIONS,
        control,
        output,
        transport=Mock(spec=[]),
        clock=FixtureClock(),
        sleeper=FixtureSleeper(),
    )
    assert acquire.call_count == 36  # No per-coordinate acquisitions.
    assert discover.call_count == 1
    assert report["supported_hours"] == list(range(1, 37))
    assert report["missing_hours"] == {}
    assert len(report["regions"]) == 2
    assert report["regions"][0]["area"]["east"] < -70
    assert report["regions"][1]["area"]["west"] > -125
    monkeypatch.setattr(
        prepared_rap, "BoundedHttpTransport", Mock(side_effect=AssertionError("network"))
    )
    discover.side_effect = AssertionError("rediscovery")
    acquire.side_effect = AssertionError("redownload")
    repeated = prepared_rap.prepare_rap(LOCATIONS, control, output)
    assert repeated["downloaded_bytes"] == 0
    rebuilt = prepared_rap.prepare_rap(LOCATIONS, control, tmp_path / "rebuilt", from_raw=output)
    assert rebuilt["downloaded_bytes"] == 0
    for first, second in zip(report["regions"], rebuilt["regions"], strict=True):
        with xr.open_dataset(Path(first["directory"]) / "RAP.nc", engine="h5netcdf") as left:
            with xr.open_dataset(Path(second["directory"]) / "RAP.nc", engine="h5netcdf") as right:
                np.testing.assert_array_equal(left.temperature, right.temperature)
    assert (control / "manifest.json").read_bytes() == original_control


def test_unavailable_hour_is_explicit_and_not_acquired(
    tmp_path: Path, preparation: tuple[Path, Mock, Mock]
) -> None:
    control, discover, acquire = preparation
    discover.return_value = _Selection(
        CYCLE, tuple(range(4, 40)), (4, 5), {hour: "RAP lead unsupported" for hour in range(3, 37)}
    )
    report = prepared_rap.prepare_rap(
        LOCATIONS, control, tmp_path / "partial", transport=Mock(spec=[])
    )
    assert report["supported_hours"] == [1, 2]
    assert report["missing_hours"][36] == "RAP lead unsupported"
    assert acquire.call_count == 2


def test_offline_rebuild_rejects_corrupted_raw(
    tmp_path: Path, preparation: tuple[Path, Mock, Mock]
) -> None:
    control, _, _ = preparation
    output = tmp_path / "rap"
    prepared_rap.prepare_rap(LOCATIONS, control, output, transport=Mock(spec=[]))
    (output / "source/raw/RAP-f004.grib2").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        prepared_rap.prepare_rap(LOCATIONS, control, tmp_path / "rebuilt", from_raw=output)


def test_no_available_cycle_preserves_explicit_empty_shadow(
    tmp_path: Path, preparation: tuple[Path, Mock, Mock]
) -> None:
    control, discover, acquire = preparation
    discover.return_value = _Selection(
        None, (), (), {hour: "no usable cycle" for hour in range(1, 37)}
    )
    report = prepared_rap.prepare_rap(
        LOCATIONS, control, tmp_path / "empty", transport=Mock(spec=[])
    )
    assert report["selected_cycle"] is None
    assert report["supported_hours"] == []
    assert report["prepared_bytes"] == 0
    assert len(report["missing_hours"]) == 36
    acquire.assert_not_called()


def test_offline_success_does_not_keep_a_previous_decode_failure_reason(
    tmp_path: Path, preparation: tuple[Path, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    control, _, _ = preparation
    output = tmp_path / "rap"

    def decode(payload: bytes, **kwargs: Any) -> bytes:
        if kwargs["forecast_hour"] == 4:
            raise ValueError("fixture decoding failure")
        return payload

    monkeypatch.setattr(prepared_rap, "decode_temperature_message", decode)
    first = prepared_rap.prepare_rap(LOCATIONS, control, output, transport=Mock(spec=[]))
    assert 1 not in first["supported_hours"] and 1 in first["missing_hours"]
    monkeypatch.setattr(prepared_rap, "decode_temperature_message", lambda payload, **kw: payload)
    repeated = prepared_rap.prepare_rap(LOCATIONS, control, tmp_path / "rebuilt", from_raw=output)
    assert repeated["supported_hours"] == list(range(1, 37))
    assert repeated["missing_hours"] == {}


def test_bounded_hours_are_retained_and_reused_only_with_the_same_request(
    tmp_path: Path, preparation: tuple[Path, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    control, discover, acquire = preparation
    hours = (3, 6, 9, 12)
    leads = (6, 9, 12, 15)
    discover.return_value = _Selection(CYCLE, leads, leads, {})
    output = tmp_path / "bounded"
    report = prepared_rap.prepare_rap(
        LOCATIONS, control, output, target_horizons=hours, transport=Mock(spec=[])
    )
    assert discover.call_args.kwargs["target_horizons"] == hours
    assert [call.kwargs["forecast_hour"] for call in acquire.call_args_list] == list(leads)
    assert report["requested_target_horizons"] == report["supported_hours"] == list(hours)
    assert len(report["missing_hours"]) == 32
    assert all("not requested" in reason for reason in report["missing_hours"].values())
    manifest = json.loads((output / "source/manifest.json").read_text())
    assert manifest["target_horizon_hours"] == list(range(1, 37))
    assert manifest["requested_target_horizons"] == list(hours)
    assert len(manifest["inputs"]) == 4
    monkeypatch.setattr(
        prepared_rap, "BoundedHttpTransport", Mock(side_effect=AssertionError("network"))
    )
    discover.side_effect = acquire.side_effect = AssertionError("provider access")
    repeated = prepared_rap.prepare_rap(LOCATIONS, control, output, target_horizons=hours)
    rebuilt = prepared_rap.prepare_rap(
        LOCATIONS, control, tmp_path / "rebuilt-bounded", target_horizons=hours, from_raw=output
    )
    assert repeated["downloaded_bytes"] == rebuilt["downloaded_bytes"] == 0
    assert rebuilt["supported_hours"] == list(hours)
    for a, b in zip(report["regions"], rebuilt["regions"], strict=True):
        with (
            xr.open_dataset(Path(a["directory"]) / "RAP.nc", engine="h5netcdf") as left,
            xr.open_dataset(Path(b["directory"]) / "RAP.nc", engine="h5netcdf") as right,
        ):
            xr.testing.assert_identical(left, right)
    with pytest.raises(ValueError, match="snapshot differs"):
        prepared_rap.prepare_rap(LOCATIONS, control, output)
    with pytest.raises(ValueError, match="window differ"):
        prepared_rap.prepare_rap(LOCATIONS, control, tmp_path / "wrong-window", from_raw=output)


def test_legacy_full_window_manifest_remains_reusable(
    tmp_path: Path, preparation: tuple[Path, Mock, Mock]
) -> None:
    control, discover, acquire = preparation
    output = tmp_path / "legacy"
    prepared_rap.prepare_rap(LOCATIONS, control, output, transport=Mock(spec=[]))
    for path in [output / "coverage.json", *output.rglob("manifest.json")]:
        value = json.loads(path.read_text())
        value.pop("requested_target_horizons")
        path.write_text(json.dumps(value))
    discover.side_effect = acquire.side_effect = AssertionError("provider access")
    assert prepared_rap.prepare_rap(LOCATIONS, control, output)["downloaded_bytes"] == 0
    assert prepared_rap.prepare_rap(
        LOCATIONS, control, tmp_path / "legacy-rebuilt", from_raw=output
    )["supported_hours"] == list(range(1, 37))


@pytest.mark.parametrize("hours", [(), (3, 3), (6, 3), (True,), (0,), (37,)])
def test_bounded_preparation_rejects_invalid_hours_before_io(tmp_path, hours):
    with pytest.raises(ValueError, match="sorted unique nonempty"):
        prepared_rap.prepare_rap([], tmp_path / "missing-control", tmp_path, target_horizons=hours)
