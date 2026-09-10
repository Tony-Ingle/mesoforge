"""Focused current-cycle completeness, mirror fallback, and valid-time checks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from mesoforge.application import cycle_selection, prepared_temperature
from mesoforge.application.cycle_selection import (
    CurrentGuidanceUnavailableError,
    select_current_guidance,
)
from mesoforge.application.spatial_coverage import UnsupportedCoordinateError, footprint
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition, SelectedMessage
from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import IndexRow
from tests.unit.application.test_prepared_temperature import (
    EXTENDED_HORIZONS,
    TARGET,
    FixtureSleeper,
    FixtureTransport,
    _Response,
    phase2_configuration,
)


class Clock:
    def __init__(self) -> None:
        self.value = TARGET + timedelta(minutes=40)

    def now(self) -> datetime:
        return self.value


def _acquired(model: str, endpoint: str, **kwargs: Any) -> Phase2LeadAcquisition:
    lead = kwargs["forecast_hour"]
    cycle = datetime.combine(kwargs["cycle_date"], datetime.min.time(), tzinfo=UTC).replace(
        hour=kwargs["cycle_hour"]
    )
    payload = f"{model}-{cycle}-{lead}".encode()
    return Phase2LeadAcquisition(
        model=model.lower(),
        cycle_date=cycle.date(),
        cycle_hour=cycle.hour,
        forecast_hour=lead,
        endpoint=endpoint,
        resolved_grib_url=f"https://fixture/{model}/{cycle.hour}/{lead}",
        resolved_index_url=f"https://fixture/{model}/{cycle.hour}/{lead}.idx",
        index_payload=b"retained inventory",
        index_attempts=(),
        index_completed_at=cycle,
        selected_messages=(
            SelectedMessage(
                "air_temperature_2m", IndexRow(1, 0, "fixture"), 0, len(payload), payload
            ),
        ),
        grib_attempts=(),
        grib_completed_at=cycle,
        full_object_etag="fixture-etag",
        full_object_last_modified=None,
        full_object_content_length=len(payload),
        index_available_at=cycle,
        grib_available_at=cycle,
    )


@pytest.fixture
def fake_acquisition(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"calls": [], "fail": lambda model, endpoint, kwargs: False}

    def acquire(settings: Any, **kwargs: Any) -> Phase2LeadAcquisition:
        model = settings.model.upper()
        endpoint = settings.endpoint_order[0]
        state["calls"].append((model, kwargs["cycle_hour"], kwargs["forecast_hour"], endpoint))
        assert kwargs["canonical_variables"] == ("air_temperature_2m",)
        if state["fail"](model, endpoint, kwargs):
            raise FetchError("Provider object unavailable")
        return _acquired(model, endpoint, **kwargs)

    monkeypatch.setattr(cycle_selection, "acquire_hrrr_phase2_lead", acquire)
    monkeypatch.setattr(cycle_selection, "acquire_gfs_lead", acquire)
    monkeypatch.setattr(cycle_selection, "_validate_message", Mock())
    state["normalize"] = Mock(return_value=Mock())
    monkeypatch.setattr(prepared_temperature, "normalize_temperature_messages", state["normalize"])
    return state


def _select(
    tmp_path: Path, **kwargs: Any
) -> tuple[dict[str, list[Phase2LeadAcquisition]], dict[str, Any]]:
    return select_current_guidance(
        tmp_path / "discovery",
        configuration=phase2_configuration(),
        transport=kwargs.pop("transport", Mock(downloaded_bytes=0)),
        clock=kwargs.pop("clock", Clock()),
        sleeper=FixtureSleeper(),
        areas=(footprint(45.8, -93.1, 150),),
        **kwargs,
    )


@pytest.mark.parametrize("missing_lead", [36, 17])
def test_falls_back_only_after_checking_every_required_lead_and_retains_evidence(
    tmp_path: Path,
    fake_acquisition: dict[str, Any],
    missing_lead: int,
) -> None:
    fake_acquisition["fail"] = lambda model, endpoint, kwargs: (
        model == "HRRR" and kwargs["cycle_hour"] == 12 and kwargs["forecast_hour"] == missing_lead
    )
    selected, report = _select(tmp_path)
    assert report["target_reference_time"] == "2026-08-30T12:00:00Z"
    assert report["first_valid_time"] == "2026-08-30T13:00:00Z"
    assert report["last_valid_time"] == "2026-09-01T00:00:00Z"
    assert report["selected_cycles"] == {
        "HRRR": "2026-08-30T06:00:00Z",
        "GFS": "2026-08-30T12:00:00Z",
    }
    assert [a.forecast_hour for a in selected["HRRR"]] == list(range(7, 43))
    assert [a.forecast_hour for a in selected["GFS"]] == list(range(1, 37))
    assert fake_acquisition["calls"][0] == ("HRRR", 12, 36, "aws")
    rejected, accepted = report["candidates"]["HRRR"]
    assert rejected["status"] == "rejected" and str(missing_lead) in rejected["reason"]
    assert accepted["status"] == "selected"
    assert accepted["inputs"][0]["source_lead_hours"] == 42
    for horizon, (hrrr, gfs) in enumerate(zip(selected["HRRR"], selected["GFS"], strict=True), 1):
        assert (
            hrrr.cycle_hour + hrrr.forecast_hour
            == gfs.cycle_hour + gfs.forecast_hour
            == 12 + horizon
        )
    if missing_lead == 17:
        retained = tmp_path / "discovery/HRRR/20260830T12Z/raw/HRRR-f036.grib2"
        assert (
            retained.read_bytes()
            == _acquired("HRRR", "aws", cycle_date=TARGET.date(), cycle_hour=12, forecast_hour=36)
            .selected_messages[0]
            .payload
        )
    assert json.loads((tmp_path / "discovery/selection.json").read_text()) == report


def test_first_mirror_failure_does_not_falsely_age_the_selected_cycle(
    tmp_path: Path,
    fake_acquisition: dict[str, Any],
) -> None:
    fake_acquisition["fail"] = lambda model, endpoint, kwargs: (
        model == "GFS" and endpoint == "gcs_archive"
    )
    selected, report = _select(tmp_path)
    assert report["selected_cycles"]["GFS"] == "2026-08-30T12:00:00Z"
    assert {a.endpoint for a in selected["GFS"]} == {"aws_archive"}
    attempts = report["candidates"]["GFS"][0]["attempts"]
    assert attempts[0]["status"] == "unavailable"
    assert attempts[1]["status"] == "acquired"
    assert attempts[0]["lead"] == attempts[1]["lead"] == 36


def test_no_complete_cycle_fails_closed_without_rewinding_reference_or_exceeding_lead48(
    tmp_path: Path,
    fake_acquisition: dict[str, Any],
) -> None:
    fake_acquisition["fail"] = lambda model, endpoint, kwargs: True
    with pytest.raises(CurrentGuidanceUnavailableError, match="No complete usable HRRR"):
        _select(tmp_path)
    report = json.loads((tmp_path / "discovery/selection.json").read_text())
    assert report["target_reference_time"] == "2026-08-30T12:00:00Z"
    assert [c["cycle"] for c in report["candidates"]["HRRR"]] == [
        "2026-08-30T12:00:00Z",
        "2026-08-30T06:00:00Z",
        "2026-08-30T00:00:00Z",
    ]
    assert max(call[2] for call in fake_acquisition["calls"]) == 48
    assert not report["selected_cycles"] and not report["candidates"]["GFS"]


def test_invalid_decoded_source_falls_back_and_preserves_rejected_message(
    tmp_path: Path,
    fake_acquisition: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def validate(acquired: Phase2LeadAcquisition, settings: Any) -> None:
        if acquired.model == "hrrr" and acquired.cycle_hour == 12:
            raise ValueError("Source cycle in GRIB does not match requested cycle")

    monkeypatch.setattr(cycle_selection, "_validate_message", validate)
    _, report = _select(tmp_path)
    assert report["selected_cycles"]["HRRR"] == "2026-08-30T06:00:00Z"
    rejected = report["candidates"]["HRRR"][0]
    assert "does not match" in rejected["reason"]
    assert rejected["inputs"][0]["raw_sha256"]
    assert (tmp_path / "discovery/HRRR/20260830T12Z/raw/HRRR-f036.grib2").is_file()


def test_unsupported_footprints_do_not_trigger_cycle_fallback(
    tmp_path: Path,
    fake_acquisition: dict[str, Any],
) -> None:
    fake_acquisition["normalize"].side_effect = UnsupportedCoordinateError("Outside native domain")
    with pytest.raises(UnsupportedCoordinateError, match="Outside native domain"):
        _select(tmp_path)
    assert {call[1] for call in fake_acquisition["calls"]} == {12}
    report = json.loads((tmp_path / "discovery/selection.json").read_text())
    assert report["status"] == "unsupported_coordinate"


def test_expired_reference_aborts_and_unexpected_errors_are_not_silently_rejected(
    tmp_path: Path,
    fake_acquisition: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()

    def validate(*args: Any) -> None:
        clock.value = TARGET + timedelta(hours=1)

    monkeypatch.setattr(cycle_selection, "_validate_message", validate)
    with pytest.raises(CurrentGuidanceUnavailableError, match="Reference hour expired"):
        _select(tmp_path, clock=clock)
    assert len(fake_acquisition["calls"]) == 1
    monkeypatch.setattr(
        cycle_selection,
        "acquire_hrrr_phase2_lead",
        Mock(side_effect=RuntimeError("programming defect")),
    )
    with pytest.raises(RuntimeError, match="programming defect"):
        _select(tmp_path / "second")


def test_real_generated_grib_acquisition_and_decode_select_different_cycles_with_aligned_times(
    tmp_path: Path,
) -> None:
    class AvailableFixtureTransport(FixtureTransport):
        def get(
            self,
            url: str,
            *,
            headers: Mapping[str, str] | None = None,
            timeout: tuple[float, float] | None = None,
        ) -> _Response:
            if "gfs.t12z" in url:
                self.calls.append(("get", url, (headers or {}).get("Range")))
                return _Response(404, {}, b"")
            return super().get(url, headers=headers, timeout=timeout)

        def head(
            self,
            url: str,
            *,
            headers: Mapping[str, str] | None = None,
            timeout: tuple[float, float] | None = None,
        ) -> _Response:
            # An advertised index alone is insufficient: a mirror can lack its GRIB.
            if "storage.googleapis.com" in url:
                self.calls.append(("head", url, None))
                return _Response(404, {}, b"")
            return super().head(url, headers=headers, timeout=timeout)

    transport = AvailableFixtureTransport(EXTENDED_HORIZONS)
    selected, report = _select(tmp_path, transport=transport)
    assert len(selected["HRRR"]) == len(selected["GFS"]) == 36
    assert report["selected_cycles"] == {
        "HRRR": "2026-08-30T12:00:00Z",
        "GFS": "2026-08-30T06:00:00Z",
    }
    assert report["candidates"]["GFS"][0]["status"] == "rejected"
    assert {a.endpoint for a in selected["GFS"]} == {"aws_archive"}
    assert len(list((tmp_path / "discovery").rglob("*.grib2"))) == 72
    assert [a.forecast_hour for a in selected["HRRR"]] == list(range(1, 37))
    assert [a.forecast_hour for a in selected["GFS"]] == list(range(7, 43))
