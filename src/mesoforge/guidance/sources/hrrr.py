"""NOAA HRRR provider adapter (plan Section 2.1/2.2, Task 4).

Pure URL construction, ``.idx`` inventory parsing, and field-row
selection live here as plain functions with no I/O -- they are used by
both ``guidance/acquisition.py`` (with an injected transport) and any
live smoke test. The only concrete network code is
``RequestsHrrrHttpTransport``, a thin ``requests``-backed
implementation of ``guidance.interfaces.HttpTransport`` used solely for
production wiring / opt-in live tests, never unit tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from mesoforge.catalog.sources import HrrrSourceSettings
from mesoforge.common.errors import MesoForgeError


class HrrrIndexError(MesoForgeError):
    """Raised when a ``.idx`` inventory is malformed, or a field
    selector matches zero or more than one row (plan Section 2.1: a
    terminal scientific validation error, never a silent first-match
    choice)."""


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


@dataclass(frozen=True, slots=True)
class IndexRow:
    """One parsed ``.idx`` inventory row: sequence number, byte start
    offset, and the exact original line text (used both for selector
    matching and for lineage retention)."""

    message_number: int
    byte_offset: int
    line: str

    @property
    def descriptor(self) -> str:
        """The colon-joined tail (parameter/level/step-type fields)
        reconstructed exactly as a leading-colon string, e.g.
        ``':TMP:2 m above ground:anl:'``, for selector regex matching."""
        fields = self.line.split(":")
        return ":" + ":".join(fields[3:])


def parse_index_rows(index_text: str) -> tuple[IndexRow, ...]:
    """Parse a wgrib2-style ``.idx`` inventory. Raises ``HrrrIndexError``
    on any malformed line, non-monotonic byte offsets, or non-sequential
    message numbers (plan Section 2.1/4: unsorted/malformed index
    rejection)."""
    lines = [line for line in index_text.splitlines() if line.strip()]
    if not lines:
        raise HrrrIndexError("empty .idx inventory")

    rows: list[IndexRow] = []
    for raw_line in lines:
        fields = raw_line.split(":")
        if len(fields) < 4:
            raise HrrrIndexError(f"malformed .idx line (too few fields): {raw_line!r}")
        try:
            message_number = int(fields[0])
            byte_offset = int(fields[1])
        except ValueError as exc:
            raise HrrrIndexError(f"malformed .idx line (non-integer fields): {raw_line!r}") from exc
        if byte_offset < 0:
            raise HrrrIndexError(f"negative byte offset in .idx line: {raw_line!r}")
        rows.append(IndexRow(message_number=message_number, byte_offset=byte_offset, line=raw_line))

    for expected, row in enumerate(rows, start=1):
        if row.message_number != expected:
            raise HrrrIndexError(
                f"non-sequential .idx message numbers: expected {expected}, got "
                f"{row.message_number} at line {row.line!r}"
            )
    for previous, current in zip(rows, rows[1:], strict=False):
        if current.byte_offset <= previous.byte_offset:
            raise HrrrIndexError(
                f"non-monotonic .idx byte offsets: {previous.byte_offset} then "
                f"{current.byte_offset}"
            )
    return tuple(rows)


def select_field_row(rows: tuple[IndexRow, ...], selector_pattern: str) -> IndexRow:
    """Match exactly one row's descriptor against ``selector_pattern``.
    Raises ``HrrrIndexError`` on zero or multiple matches (plan Section
    2.1: 'Zero or duplicate matches are terminal scientific validation
    errors')."""
    compiled = re.compile(selector_pattern)
    matches = [row for row in rows if compiled.search(row.descriptor)]
    if len(matches) == 0:
        raise HrrrIndexError(f"selector {selector_pattern!r} matched zero rows")
    if len(matches) > 1:
        raise HrrrIndexError(
            f"selector {selector_pattern!r} matched {len(matches)} rows "
            f"(ambiguous): {[m.line for m in matches]!r}"
        )
    return matches[0]


def check_lead_step_type(row: IndexRow, *, forecast_hour: int) -> None:
    """Section 2.1: lead 0 must end in inventory step type ``anl``; lead
    N > 0 must end in exactly ``N hour fcst``."""
    expected = "anl" if forecast_hour == 0 else f"{forecast_hour} hour fcst"
    if f":{expected}:" not in row.descriptor:
        raise HrrrIndexError(
            f"row {row.line!r} does not declare the expected step type {expected!r} "
            f"for forecast_hour={forecast_hour}"
        )


def compute_message_byte_range(
    rows: tuple[IndexRow, ...],
    *,
    selected: IndexRow,
    full_object_length: int | None,
) -> tuple[int, int]:
    """Section 2.2: the byte range for one selected message is
    ``[byte_offset, next_row.byte_offset)`` using the *full* index's
    next row (not merely the next selected row); the last message in
    the whole index instead ends at ``full_object_length`` (obtained
    via HEAD)."""
    ordered = sorted(rows, key=lambda r: r.byte_offset)
    position = next(i for i, r in enumerate(ordered) if r.message_number == selected.message_number)
    if position + 1 < len(ordered):
        end = ordered[position + 1].byte_offset
    else:
        if full_object_length is None:
            raise HrrrIndexError(
                f"message {selected.message_number} is the last row in the index; "
                "full_object_length (from a HEAD request) is required"
            )
        end = full_object_length
    return (selected.byte_offset, end)
