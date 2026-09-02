"""Generic wgrib2-style ``.idx`` inventory parsing and byte-range
framing (plan Section 2.1/2.2, Task 2).

Extracted from ``guidance/sources/hrrr.py`` so NBM and GFS source
adapters share exactly the same strict inventory-row/byte-range logic
instead of re-implementing it. ``guidance/sources/hrrr.py`` re-exports
these names unchanged for backward compatibility with Phase 1 code and
tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mesoforge.common.errors import MesoForgeError


class GribIndexError(MesoForgeError):
    """Raised when a ``.idx`` inventory is malformed, or a field
    selector matches zero or more than one row (plan Section 2.1: a
    terminal scientific validation error, never a silent first-match
    choice)."""


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
    """Parse a wgrib2-style ``.idx`` inventory. Raises ``GribIndexError``
    on any malformed line, non-monotonic byte offsets, or non-sequential
    message numbers (plan Section 2.1/4: unsorted/malformed index
    rejection)."""
    lines = [line for line in index_text.splitlines() if line.strip()]
    if not lines:
        raise GribIndexError("empty .idx inventory")

    rows: list[IndexRow] = []
    for raw_line in lines:
        fields = raw_line.split(":")
        if len(fields) < 4:
            raise GribIndexError(f"malformed .idx line (too few fields): {raw_line!r}")
        try:
            message_number = int(fields[0])
            byte_offset = int(fields[1])
        except ValueError as exc:
            raise GribIndexError(f"malformed .idx line (non-integer fields): {raw_line!r}") from exc
        if byte_offset < 0:
            raise GribIndexError(f"negative byte offset in .idx line: {raw_line!r}")
        rows.append(IndexRow(message_number=message_number, byte_offset=byte_offset, line=raw_line))

    for expected, row in enumerate(rows, start=1):
        if row.message_number != expected:
            raise GribIndexError(
                f"non-sequential .idx message numbers: expected {expected}, got "
                f"{row.message_number} at line {row.line!r}"
            )
    for previous, current in zip(rows, rows[1:], strict=False):
        if current.byte_offset <= previous.byte_offset:
            raise GribIndexError(
                f"non-monotonic .idx byte offsets: {previous.byte_offset} then "
                f"{current.byte_offset}"
            )
    return tuple(rows)


def select_field_row(rows: tuple[IndexRow, ...], selector_pattern: str) -> IndexRow:
    """Match exactly one row's descriptor against ``selector_pattern``.
    Raises ``GribIndexError`` on zero or multiple matches (plan Section
    2.1: 'Zero or duplicate matches are terminal scientific validation
    errors')."""
    compiled = re.compile(selector_pattern)
    matches = [row for row in rows if compiled.search(row.descriptor)]
    if len(matches) == 0:
        raise GribIndexError(f"selector {selector_pattern!r} matched zero rows")
    if len(matches) > 1:
        raise GribIndexError(
            f"selector {selector_pattern!r} matched {len(matches)} rows "
            f"(ambiguous): {[m.line for m in matches]!r}"
        )
    return matches[0]


def select_field_rows(rows: tuple[IndexRow, ...], selector_pattern: str) -> tuple[IndexRow, ...]:
    """Like ``select_field_row`` but permits multiple matches (used by
    the GFS dual-parent APCP equivalence check, plan Section 2.4, where
    two candidate rows are deliberately both retained for the caller to
    validate). Raises ``GribIndexError`` on zero matches."""
    compiled = re.compile(selector_pattern)
    matches = tuple(row for row in rows if compiled.search(row.descriptor))
    if len(matches) == 0:
        raise GribIndexError(f"selector {selector_pattern!r} matched zero rows")
    return matches


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
            raise GribIndexError(
                f"message {selected.message_number} is the last row in the index; "
                "full_object_length (from a HEAD request) is required"
            )
        end = full_object_length
    return (selected.byte_offset, end)
