"""NOAA HRRR provider adapter (plan Section 2.1/2.2, Task 4; Task 2
generalization).

Pure URL construction and field-row selection live here as plain
functions with no I/O -- they are used by both
``guidance/acquisition.py`` (with an injected transport) and any live
smoke test. ``.idx`` inventory parsing and byte-range framing were
extracted to ``guidance.index_parsing`` (Task 2) so NBM/GFS share the
exact same strict parser; this module re-exports those names under
their original Phase 1 names (``HrrrIndexError``, ``IndexRow``,
``parse_index_rows``, ``select_field_row``,
``compute_message_byte_range``) for full backward compatibility. The
only concrete network code is ``RequestsHrrrHttpTransport``, a thin
``requests``-backed implementation of ``guidance.interfaces.HttpTransport``
used solely for production wiring / opt-in live tests, never unit
tests.
"""

from __future__ import annotations

from datetime import date

from mesoforge.catalog.sources import HrrrSourceSettings
from mesoforge.guidance.index_parsing import (
    GribIndexError,
    IndexRow,
    compute_message_byte_range,
    parse_index_rows,
    select_field_row,
)

# Backward-compatible alias: Phase 1 code/tests import HrrrIndexError from
# this module. Task 2 generalized the underlying parser to
# guidance.index_parsing.GribIndexError (shared by HRRR/NBM/GFS); both
# names refer to the exact same exception class.
HrrrIndexError = GribIndexError

__all__ = [
    "HrrrIndexError",
    "IndexRow",
    "build_grib_url",
    "build_index_url",
    "check_lead_step_type",
    "compute_message_byte_range",
    "format_grib_filename",
    "parse_index_rows",
    "select_field_row",
]


def format_grib_filename(
    settings: HrrrSourceSettings, *, cycle_hour: int, forecast_hour: int
) -> str:
    """Section 2.1: ``hrrr.t{HH:02d}z.wrfsfcf{FF:02d}.grib2``."""
    if not (0 <= cycle_hour <= 23):
        raise ValueError(f"cycle_hour must be in [0, 23], got {cycle_hour!r}")
    if forecast_hour not in settings.forecast_hours:
        raise ValueError(
            f"forecast_hour {forecast_hour!r} is not one of the configured "
            f"forecast_hours {settings.forecast_hours!r}"
        )
    return settings.file_template.format(HH=cycle_hour, FF=forecast_hour)


def build_grib_url(
    settings: HrrrSourceSettings,
    *,
    endpoint: str,
    cycle_date: date,
    cycle_hour: int,
    forecast_hour: int,
) -> str:
    if endpoint not in settings.endpoint_url_templates:
        raise ValueError(
            f"unknown endpoint {endpoint!r}; expected one of {settings.endpoint_order!r}"
        )
    filename = format_grib_filename(settings, cycle_hour=cycle_hour, forecast_hour=forecast_hour)
    template = settings.endpoint_url_templates[endpoint]
    return template.format(YYYYMMDD=cycle_date.strftime("%Y%m%d"), FILE=filename)


def build_index_url(
    settings: HrrrSourceSettings,
    *,
    endpoint: str,
    cycle_date: date,
    cycle_hour: int,
    forecast_hour: int,
) -> str:
    return (
        build_grib_url(
            settings,
            endpoint=endpoint,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
            forecast_hour=forecast_hour,
        )
        + settings.index_suffix
    )


def check_lead_step_type(row: IndexRow, *, forecast_hour: int) -> None:
    """Section 2.1: lead 0 must end in inventory step type ``anl``; lead
    N > 0 must end in exactly ``N hour fcst``."""
    expected = "anl" if forecast_hour == 0 else f"{forecast_hour} hour fcst"
    if f":{expected}:" not in row.descriptor:
        raise HrrrIndexError(
            f"row {row.line!r} does not declare the expected step type {expected!r} "
            f"for forecast_hour={forecast_hour}"
        )
