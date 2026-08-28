"""Unit tests for mesoforge.alignment.reports.PointExtractionReport
(plan Section 3.6, Task 6)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mesoforge.alignment.reports import (
    ExtractionWeights,
    PointExtractionRecord,
    PointExtractionReport,
)

_STATIONS = ("station.kcbg", "station.kjmr", "station.kros")
_LEADS = tuple(range(2))
_VARIABLES = ("air_temperature_2m", "eastward_wind_10m", "northward_wind_10m")


def _record(station: str, lead: int, variable: str) -> PointExtractionRecord:
    return PointExtractionRecord(
        station_id=station,
        lead_hours=lead,
        canonical_variable_id=variable,
        source_y0=0,
        source_y1=1,
        source_x0=0,
        source_x1=1,
        station_projected_x=100.0,
        station_projected_y=200.0,
        weights=ExtractionWeights(w00=0.25, w01=0.25, w10=0.25, w11=0.25),
        value=280.0,
    )


def _complete_records() -> tuple[PointExtractionRecord, ...]:
    return tuple(_record(s, lead, v) for s in _STATIONS for lead in _LEADS for v in _VARIABLES)


class TestPointExtractionReport:
    def test_accepts_complete_report(self) -> None:
        report = PointExtractionReport(
            source_grid_id="hrrr-conus-grasston-subset.v1",
            pyproj_version="3.7.2",
            records=_complete_records(),
            expected_station_ids=_STATIONS,
            expected_lead_hours=_LEADS,
            expected_variable_ids=_VARIABLES,
        )
        assert len(report.records) == 18  # 3 stations x 2 leads x 3 variables

    def test_rejects_missing_record(self) -> None:
        records = _complete_records()[:-1]
        with pytest.raises(ValidationError, match="missing"):
            PointExtractionReport(
                source_grid_id="hrrr-conus-grasston-subset.v1",
                pyproj_version="3.7.2",
                records=records,
                expected_station_ids=_STATIONS,
                expected_lead_hours=_LEADS,
                expected_variable_ids=_VARIABLES,
            )

    def test_rejects_duplicate_record(self) -> None:
        records = _complete_records() + (_record("station.kcbg", 0, "air_temperature_2m"),)
        with pytest.raises(ValidationError, match="duplicate"):
            PointExtractionReport(
                source_grid_id="hrrr-conus-grasston-subset.v1",
                pyproj_version="3.7.2",
                records=records,
                expected_station_ids=_STATIONS,
                expected_lead_hours=_LEADS,
                expected_variable_ids=_VARIABLES,
            )

    def test_elevation_policy_is_pinned(self) -> None:
        report = PointExtractionReport(
            source_grid_id="hrrr-conus-grasston-subset.v1",
            pyproj_version="3.7.2",
            records=_complete_records(),
            expected_station_ids=_STATIONS,
            expected_lead_hours=_LEADS,
            expected_variable_ids=_VARIABLES,
        )
        assert report.station_elevation_policy == "ignored-in-phase1"
