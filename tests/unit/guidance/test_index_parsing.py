"""Unit tests for mesoforge.guidance.index_parsing (plan Section 2.1/2.2,
Task 2): the shared HRRR/NBM/GFS ``.idx`` inventory parser generalized
out of ``guidance.sources.hrrr``.
"""

from __future__ import annotations

import pytest

from mesoforge.guidance.index_parsing import (
    GribIndexError,
    compute_message_byte_range,
    parse_index_rows,
    select_field_row,
    select_field_rows,
)

_SAMPLE_INDEX = (
    "1:0:d=2026083012:TMP:2 m above ground:6 hour fcst:\n"
    "2:150:d=2026083012:UGRD:10 m above ground:6 hour fcst:\n"
    "3:300:d=2026083012:VGRD:10 m above ground:6 hour fcst:\n"
)

_DUPLICATE_APCP_INDEX = (
    "1:0:d=2026083012:APCP:surface:0-6 hour acc fcst:\n"
    "2:150:d=2026083012:APCP:surface:5-6 hour acc fcst:\n"
)


class TestParseIndexRows:
    def test_parses_valid_rows(self) -> None:
        rows = parse_index_rows(_SAMPLE_INDEX)
        assert len(rows) == 3
        assert rows[0].message_number == 1
        assert rows[0].byte_offset == 0

    def test_rejects_empty_index(self) -> None:
        with pytest.raises(GribIndexError, match="empty"):
            parse_index_rows("")

    def test_rejects_non_sequential_message_numbers(self) -> None:
        bad = "1:0:d=x:TMP:2 m above ground:anl:\n3:100:d=x:UGRD:10 m above ground:anl:\n"
        with pytest.raises(GribIndexError, match="non-sequential"):
            parse_index_rows(bad)

    def test_rejects_non_monotonic_byte_offsets(self) -> None:
        bad = "1:100:d=x:TMP:2 m above ground:anl:\n2:50:d=x:UGRD:10 m above ground:anl:\n"
        with pytest.raises(GribIndexError, match="non-monotonic"):
            parse_index_rows(bad)

    def test_rejects_malformed_line(self) -> None:
        with pytest.raises(GribIndexError, match="malformed"):
            parse_index_rows("not-a-valid-line\n")


class TestSelectFieldRow:
    def test_selects_unique_match(self) -> None:
        rows = parse_index_rows(_SAMPLE_INDEX)
        row = select_field_row(rows, ":TMP:2 m above ground:(anl|[0-9]+ hour fcst):")
        assert row.message_number == 1

    def test_rejects_zero_matches(self) -> None:
        rows = parse_index_rows(_SAMPLE_INDEX)
        with pytest.raises(GribIndexError, match="matched zero rows"):
            select_field_row(rows, ":DPT:2 m above ground:")

    def test_rejects_ambiguous_matches(self) -> None:
        rows = parse_index_rows(_DUPLICATE_APCP_INDEX)
        with pytest.raises(GribIndexError, match="ambiguous"):
            select_field_row(rows, ":APCP:surface:")


class TestSelectFieldRows:
    def test_returns_all_matches(self) -> None:
        rows = parse_index_rows(_DUPLICATE_APCP_INDEX)
        matches = select_field_rows(rows, ":APCP:surface:")
        assert len(matches) == 2

    def test_rejects_zero_matches(self) -> None:
        rows = parse_index_rows(_SAMPLE_INDEX)
        with pytest.raises(GribIndexError, match="matched zero rows"):
            select_field_rows(rows, ":DPT:")


class TestComputeMessageByteRange:
    def test_middle_row_ends_at_next_row(self) -> None:
        rows = parse_index_rows(_SAMPLE_INDEX)
        start, end = compute_message_byte_range(rows, selected=rows[0], full_object_length=None)
        assert (start, end) == (0, 150)

    def test_last_row_requires_full_object_length(self) -> None:
        rows = parse_index_rows(_SAMPLE_INDEX)
        with pytest.raises(GribIndexError, match="full_object_length"):
            compute_message_byte_range(rows, selected=rows[-1], full_object_length=None)

    def test_last_row_ends_at_full_object_length(self) -> None:
        rows = parse_index_rows(_SAMPLE_INDEX)
        start, end = compute_message_byte_range(rows, selected=rows[-1], full_object_length=500)
        assert (start, end) == (300, 500)
