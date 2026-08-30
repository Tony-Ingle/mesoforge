"""Unit tests for mesoforge.guidance.sources.hrrr (plan Section 2.1/2.2,
Task 4)."""

from __future__ import annotations

from datetime import date

import pytest

from mesoforge.catalog.sources import HrrrFieldAssertion, HrrrSourceSettings, RetryPolicy
from mesoforge.guidance.sources.hrrr import (
    HrrrIndexError,
    build_grib_url,
    build_index_url,
    check_lead_step_type,
    compute_message_byte_range,
    format_grib_filename,
    parse_index_rows,
    select_field_row,
)

_RETRY = RetryPolicy(
    connect_timeout_seconds=10.0,
    read_timeout_seconds=60.0,
    attempts_per_endpoint=4,
    backoff_seconds=(1.0, 2.0, 4.0, 8.0),
    retry_after_cap_seconds=60.0,
)

_ASSERTIONS = (
    HrrrFieldAssertion(
        canonical_variable_id="air_temperature_2m",
        inventory_selector=":TMP:2 m above ground:(anl|[0-9]+ hour fcst):",
        discipline=0,
        parameter_category=0,
        parameter_number=0,
        type_of_level="heightAboveGround",
        level=2.0,
        expected_unit_id="K",
    ),
    HrrrFieldAssertion(
        canonical_variable_id="eastward_wind_10m",
        inventory_selector=":UGRD:10 m above ground:(anl|[0-9]+ hour fcst):",
        discipline=0,
        parameter_category=2,
        parameter_number=2,
        type_of_level="heightAboveGround",
        level=10.0,
        expected_unit_id="m/s",
    ),
    HrrrFieldAssertion(
        canonical_variable_id="northward_wind_10m",
        inventory_selector=":VGRD:10 m above ground:(anl|[0-9]+ hour fcst):",
        discipline=0,
        parameter_category=2,
        parameter_number=3,
        type_of_level="heightAboveGround",
        level=10.0,
        expected_unit_id="m/s",
    ),
)

_SETTINGS = HrrrSourceSettings(
    forecast_hours=tuple(range(7)),
    file_template="hrrr.t{HH:02d}z.wrfsfcf{FF:02d}.grib2",
    endpoint_order=("aws", "nomads"),
    endpoint_url_templates={
        "aws": "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{YYYYMMDD}/conus/{FILE}",
        "nomads": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/hrrr.{YYYYMMDD}/conus/{FILE}",
    },
    field_assertions=_ASSERTIONS,
    read_keys=("discipline",),
    retry_policy=_RETRY,
    cycle_availability_deadline_minutes=90.0,
)


class TestFormatGribFilename:
    def test_formats_exact_filename(self) -> None:
        assert (
            format_grib_filename(_SETTINGS, cycle_hour=18, forecast_hour=3)
            == "hrrr.t18z.wrfsfcf03.grib2"
        )

    def test_pads_single_digit_hours(self) -> None:
        assert (
            format_grib_filename(_SETTINGS, cycle_hour=0, forecast_hour=0)
            == "hrrr.t00z.wrfsfcf00.grib2"
        )

    def test_rejects_out_of_range_cycle_hour(self) -> None:
        with pytest.raises(ValueError, match="cycle_hour"):
            format_grib_filename(_SETTINGS, cycle_hour=24, forecast_hour=0)

    def test_rejects_unconfigured_forecast_hour(self) -> None:
        with pytest.raises(ValueError, match="forecast_hour"):
            format_grib_filename(_SETTINGS, cycle_hour=0, forecast_hour=7)


class TestBuildUrls:
    def test_builds_aws_grib_url(self) -> None:
        url = build_grib_url(
            _SETTINGS, endpoint="aws", cycle_date=date(2026, 8, 28), cycle_hour=18, forecast_hour=3
        )
        assert url == (
            "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20260828/conus/"
            "hrrr.t18z.wrfsfcf03.grib2"
        )

    def test_builds_nomads_grib_url(self) -> None:
        url = build_grib_url(
            _SETTINGS,
            endpoint="nomads",
            cycle_date=date(2026, 8, 28),
            cycle_hour=0,
            forecast_hour=6,
        )
        assert url == (
            "https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/hrrr.20260828/conus/"
            "hrrr.t00z.wrfsfcf06.grib2"
        )

    def test_index_url_appends_suffix(self) -> None:
        url = build_index_url(
            _SETTINGS, endpoint="aws", cycle_date=date(2026, 8, 28), cycle_hour=18, forecast_hour=0
        )
        assert url.endswith(".grib2.idx")

    def test_rejects_unknown_endpoint(self) -> None:
        with pytest.raises(ValueError, match="unknown endpoint"):
            build_grib_url(
                _SETTINGS,
                endpoint="ftp",  # type: ignore[arg-type]
                cycle_date=date(2026, 8, 28),
                cycle_hour=0,
                forecast_hour=0,
            )


_F00_INDEX = (
    "1:0:d=2026082818:TMP:2 m above ground:anl:\n"
    "2:520000:d=2026082818:UGRD:10 m above ground:anl:\n"
    "3:1040000:d=2026082818:VGRD:10 m above ground:anl:\n"
    "4:1560000:d=2026082818:DPT:2 m above ground:anl:\n"
)

_F01_INDEX = (
    "1:0:d=2026082818:TMP:2 m above ground:1 hour fcst:\n"
    "2:520000:d=2026082818:UGRD:10 m above ground:1 hour fcst:\n"
    "3:1040000:d=2026082818:VGRD:10 m above ground:1 hour fcst:\n"
)


class TestParseIndexRows:
    def test_parses_all_rows(self) -> None:
        rows = parse_index_rows(_F00_INDEX)
        assert len(rows) == 4
        assert rows[0].message_number == 1
        assert rows[0].byte_offset == 0
        assert rows[1].byte_offset == 520000

    def test_rejects_empty_index(self) -> None:
        with pytest.raises(HrrrIndexError, match="empty"):
            parse_index_rows("")

    def test_rejects_malformed_line_too_few_fields(self) -> None:
        with pytest.raises(HrrrIndexError, match="malformed"):
            parse_index_rows("1:0\n2:520000\n")

    def test_rejects_non_integer_offset(self) -> None:
        with pytest.raises(HrrrIndexError, match="malformed"):
            parse_index_rows("1:not-a-number:d=x:TMP:2 m above ground:anl:\n")

    def test_rejects_non_sequential_message_numbers(self) -> None:
        bad = "1:0:d=x:TMP:2 m above ground:anl:\n3:520000:d=x:UGRD:10 m above ground:anl:\n"
        with pytest.raises(HrrrIndexError, match="non-sequential"):
            parse_index_rows(bad)

    def test_rejects_non_monotonic_offsets(self) -> None:
        bad = "1:520000:d=x:TMP:2 m above ground:anl:\n2:0:d=x:UGRD:10 m above ground:anl:\n"
        with pytest.raises(HrrrIndexError, match="non-monotonic"):
            parse_index_rows(bad)


class TestSelectFieldRow:
    def test_selects_exact_temperature_row(self) -> None:
        rows = parse_index_rows(_F00_INDEX)
        row = select_field_row(rows, ":TMP:2 m above ground:(anl|[0-9]+ hour fcst):")
        assert row.message_number == 1

    def test_selects_exact_ugrd_row(self) -> None:
        rows = parse_index_rows(_F00_INDEX)
        row = select_field_row(rows, ":UGRD:10 m above ground:(anl|[0-9]+ hour fcst):")
        assert row.message_number == 2

    def test_zero_matches_raises(self) -> None:
        rows = parse_index_rows(_F00_INDEX)
        with pytest.raises(HrrrIndexError, match="zero rows"):
            select_field_row(rows, ":PRES:surface:(anl|[0-9]+ hour fcst):")

    def test_duplicate_matches_raises(self) -> None:
        duplicated = _F00_INDEX + "5:2080000:d=2026082818:TMP:2 m above ground:anl:\n"
        rows = parse_index_rows(duplicated)
        with pytest.raises(HrrrIndexError, match="ambiguous"):
            select_field_row(rows, ":TMP:2 m above ground:(anl|[0-9]+ hour fcst):")


class TestCheckLeadStepType:
    def test_f00_requires_anl(self) -> None:
        rows = parse_index_rows(_F00_INDEX)
        row = select_field_row(rows, ":TMP:2 m above ground:(anl|[0-9]+ hour fcst):")
        check_lead_step_type(row, forecast_hour=0)  # does not raise

    def test_f01_requires_exact_hour_fcst(self) -> None:
        rows = parse_index_rows(_F01_INDEX)
        row = select_field_row(rows, ":TMP:2 m above ground:(anl|[0-9]+ hour fcst):")
        check_lead_step_type(row, forecast_hour=1)  # does not raise

    def test_f01_row_rejected_for_f00_lead(self) -> None:
        rows = parse_index_rows(_F01_INDEX)
        row = select_field_row(rows, ":TMP:2 m above ground:(anl|[0-9]+ hour fcst):")
        with pytest.raises(HrrrIndexError, match="expected step type"):
            check_lead_step_type(row, forecast_hour=0)


class TestComputeMessageByteRange:
    def test_middle_message_ends_at_next_row(self) -> None:
        rows = parse_index_rows(_F00_INDEX)
        selected = rows[1]  # UGRD at 520000
        start, end = compute_message_byte_range(rows, selected=selected, full_object_length=None)
        assert (start, end) == (520000, 1040000)

    def test_last_message_requires_full_object_length(self) -> None:
        rows = parse_index_rows(_F00_INDEX)
        last = rows[-1]
        with pytest.raises(HrrrIndexError, match="full_object_length"):
            compute_message_byte_range(rows, selected=last, full_object_length=None)

    def test_last_message_uses_full_object_length(self) -> None:
        rows = parse_index_rows(_F00_INDEX)
        last = rows[-1]
        start, end = compute_message_byte_range(rows, selected=last, full_object_length=2_000_000)
        assert (start, end) == (1560000, 2_000_000)
