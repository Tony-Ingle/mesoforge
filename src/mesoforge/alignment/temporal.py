"""Section 3.3 exact temporal alignment: no interpolation, no
carry-forward -- instantaneous fields require an exact valid-time
match; interval fields (PoP/QPF) require an exact interval-bound
match. Pure functions over already-decoded source valid times/
interval bounds.
"""

from __future__ import annotations

import numpy as np

from mesoforge.common.errors import MesoForgeError


class TemporalAlignmentError(MesoForgeError):
    """Raised when a source valid time or interval bound does not
    exactly match the requested target -- Phase 2 never interpolates
    or carries a value forward across a missing source hour."""


def find_exact_valid_time_index(
    source_valid_times: np.ndarray, *, target_valid_time: np.datetime64
) -> int:
    """Section 3.3: locate the exact index in ``source_valid_times``
    equal to ``target_valid_time``. Raises ``TemporalAlignmentError``
    if no exact match exists (never nearest-neighbor)."""
    matches = np.flatnonzero(source_valid_times.astype("datetime64[ns]") == target_valid_time)
    if len(matches) == 0:
        raise TemporalAlignmentError(
            f"no source valid time exactly matches target {target_valid_time!r}; "
            "temporal interpolation/carry-forward is not permitted"
        )
    if len(matches) > 1:
        raise TemporalAlignmentError(
            f"multiple source valid times match target {target_valid_time!r}; ambiguous "
            "source time axis"
        )
    return int(matches[0])


def find_exact_interval_index(
    interval_starts: np.ndarray,
    interval_ends: np.ndarray,
    *,
    target_start: np.datetime64,
    target_end: np.datetime64,
) -> int:
    """Section 3.3: locate the exact index whose ``(start, end]``
    interval equals the target interval exactly. Raises
    ``TemporalAlignmentError`` on zero or multiple matches."""
    starts = interval_starts.astype("datetime64[ns]")
    ends = interval_ends.astype("datetime64[ns]")
    matches = np.flatnonzero((starts == target_start) & (ends == target_end))
    if len(matches) == 0:
        raise TemporalAlignmentError(
            f"no source interval exactly matches target ({target_start!r}, {target_end!r}]; "
            "interval interpolation/carry-forward is not permitted"
        )
    if len(matches) > 1:
        raise TemporalAlignmentError(
            f"multiple source intervals match target ({target_start!r}, {target_end!r}]; "
            "ambiguous source interval axis"
        )
    return int(matches[0])
