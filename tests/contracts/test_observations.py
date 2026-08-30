"""Unit tests for mesoforge.contracts.observations.RawMetarRecord (plan
Section 2.4, Task 8)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mesoforge.contracts.observations import RawMetarRecord


def _record(**overrides: object) -> RawMetarRecord:
    values: dict[str, object] = dict(
        icao_id="KCBG",
        obs_time=datetime(2026, 8, 28, 18, 0, tzinfo=UTC),
        report_time=datetime(2026, 8, 28, 18, 0, tzinfo=UTC),
        receipt_time=datetime(2026, 8, 28, 18, 1, tzinfo=UTC),
        temp=15.0,
        wdir=270.0,
        wspd=10.0,
        qc_field=0.0,
        metar_type="METAR",
        raw_ob="KCBG 281800Z 27010KT 10SM CLR 15/10 A3000",
        lat=45.557,
        lon=-93.264,
        elev=285.0,
    )
    values.update(overrides)
    return RawMetarRecord(**values)  # type: ignore[arg-type]


class TestRawMetarRecord:
    def test_accepts_valid_record(self) -> None:
        record = _record()
        assert record.icao_id == "KCBG"

    def test_accepts_vrb_wind_direction(self) -> None:
        record = _record(wdir="VRB")
        assert record.wdir == "VRB"

    def test_rejects_non_vrb_string_direction(self) -> None:
        with pytest.raises(ValidationError):
            _record(wdir="XYZ")

    def test_accepts_null_optional_fields(self) -> None:
        record = _record(temp=None, wdir=None, wspd=None, qc_field=None)
        assert record.temp is None

    def test_rejects_nonfinite_temperature(self) -> None:
        with pytest.raises(ValidationError):
            _record(temp=float("nan"))

    def test_rejects_nonfinite_lat(self) -> None:
        with pytest.raises(ValidationError):
            _record(lat=float("inf"))

    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValidationError):
            _record(obs_time=datetime(2026, 8, 28, 18, 0))  # no tzinfo

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            _record(unknown_field=True)
