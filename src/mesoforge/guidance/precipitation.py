"""Pure precipitation normalization and exact interval composition.

GFS publishes both six-hour-bucket and continuous accumulation
records. Phase 2 deliberately uses the bucket series: it yields
bounded one-hour differences across every required source lead. Pure
arithmetic/selection logic -- no I/O, no GRIB decoding (that happens
in ``gfs_decoding.py``); this module receives already-decoded bucket
values and applies the exact selection/differencing contract. The independent
opt-in interval composer only sums a complete contiguous set of compatible
amounts; it never splits, interpolates or changes the production blend policy.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal

from mesoforge.common.errors import MesoForgeError

_NEGATIVE_TOLERANCE_KG_M2 = -1e-6


class GfsPrecipitationError(MesoForgeError):
    """Raised when GFS APCP bucket selection or differencing violates
    the Section 2.4 contract (wrong bucket boundary, non-monotonic
    difference below tolerance, or a bucket/continuous mismatch)."""


def compute_bucket_start(forecast_hour: int) -> int:
    """Section 2.4: ``bucket_start = 6 * floor((lead - 1) / 6)`` for
    GFS source forecast hour ``lead`` (1-indexed, <=48)."""
    if forecast_hour < 1:
        raise ValueError(f"forecast_hour must be >= 1, got {forecast_hour!r}")
    return 6 * ((forecast_hour - 1) // 6)


def is_bucket_reset_hour(forecast_hour: int) -> bool:
    """``True`` when ``forecast_hour`` is the first hour after a bucket
    reset (``f = 6k+1``), i.e. the one-hour QPF is the bucket value
    itself rather than a difference."""
    return (forecast_hour - 1) % 6 == 0


@dataclass(frozen=True, slots=True)
class BucketPrecipitationResult:
    one_hour_qpf_kg_m2: float
    bucket_start_hour: int
    is_reset_passthrough: bool
    finite_precision_floor_applied: bool
    parent_bucket_value_current: float
    parent_bucket_value_previous: float | None


def compute_one_hour_qpf(
    *,
    forecast_hour: int,
    bucket_value_current_kg_m2: float,
    bucket_value_previous_kg_m2: float | None,
) -> BucketPrecipitationResult:
    """Section 2.4: compute the exact one-hour QPF for GFS source lead
    ``forecast_hour`` from its selected bucket-accumulation value(s).

    For ``f = 6k+1`` (a reset hour), the one-hour value is the bucket
    value itself (``bucket_start = f - 1``, so the bucket contains
    exactly one hour). Otherwise it is
    ``bucket[f] - bucket[f-1]`` where both values come from the *same*
    ``startStep`` bucket. A difference in ``[-1e-6, 0)`` kg/m2 is
    floored to zero with ``finite_precision_floor_applied=True``; a
    larger decrease is a terminal ``GfsPrecipitationError``.
    """
    bucket_start = compute_bucket_start(forecast_hour)
    reset = is_bucket_reset_hour(forecast_hour)

    if reset:
        if bucket_value_previous_kg_m2 is not None:
            raise GfsPrecipitationError(
                f"forecast_hour={forecast_hour!r} is a bucket reset hour (f=6k+1); "
                "no previous-bucket value should be supplied for passthrough"
            )
        return BucketPrecipitationResult(
            one_hour_qpf_kg_m2=bucket_value_current_kg_m2,
            bucket_start_hour=bucket_start,
            is_reset_passthrough=True,
            finite_precision_floor_applied=False,
            parent_bucket_value_current=bucket_value_current_kg_m2,
            parent_bucket_value_previous=None,
        )

    if bucket_value_previous_kg_m2 is None:
        raise GfsPrecipitationError(
            f"forecast_hour={forecast_hour!r} requires a previous-bucket value for "
            "same-bucket differencing (not a reset hour)"
        )

    difference = bucket_value_current_kg_m2 - bucket_value_previous_kg_m2
    floor_applied = False
    if difference < 0:
        if difference >= _NEGATIVE_TOLERANCE_KG_M2:
            difference = 0.0
            floor_applied = True
        else:
            raise GfsPrecipitationError(
                f"forecast_hour={forecast_hour!r} bucket difference {difference!r} kg/m2 "
                f"is below the finite-precision tolerance {_NEGATIVE_TOLERANCE_KG_M2!r}; "
                "non-monotonic GFS bucket accumulation is a terminal validation error"
            )

    return BucketPrecipitationResult(
        one_hour_qpf_kg_m2=difference,
        bucket_start_hour=bucket_start,
        is_reset_passthrough=False,
        finite_precision_floor_applied=floor_applied,
        parent_bucket_value_current=bucket_value_current_kg_m2,
        parent_bucket_value_previous=bucket_value_previous_kg_m2,
    )


@dataclass(frozen=True, slots=True)
class ApcpCandidateRecord:
    """One decoded APCP inventory candidate for a GFS lead: its exact
    decoded start/end step, statistical process, unit, grid shape, and
    quality mask, used to select between the required bucket record
    and a possible duplicate continuous-total record at early leads
    (finding 4: full identity, not values/start/end alone)."""

    start_step: int
    end_step: int
    is_accumulation: bool
    values_kg_m2: tuple[float, ...]
    unit_id: str = "kg/m^2"
    grid_shape: tuple[int, int] = (0, 0)
    mask: tuple[int, ...] = ()


def select_bucket_record(
    candidates: tuple[ApcpCandidateRecord, ...], *, forecast_hour: int
) -> ApcpCandidateRecord:
    """Section 2.4: select the record whose decoded ``startStep`` equals
    the computed bucket start and ``endStep`` equals ``forecast_hour``,
    with an accumulation statistical process. For ``f=1..6`` the bucket
    and continuous inventory descriptions may be duplicates; both
    candidates are validated for equivalence by
    ``validate_dual_parent_equivalence`` when present, and this
    function returns only the bucket-boundary match, never selecting
    by inventory message order."""
    expected_start = compute_bucket_start(forecast_hour)
    matches = [
        c
        for c in candidates
        if c.is_accumulation and c.start_step == expected_start and c.end_step == forecast_hour
    ]
    if len(matches) == 0:
        raise GfsPrecipitationError(
            f"no APCP candidate with startStep={expected_start!r}, endStep={forecast_hour!r}, "
            "accumulation statistical process found for the required GFS bucket record"
        )
    if len(matches) > 1:
        canonical = matches[0]
        if all(validate_dual_parent_equivalence(canonical, item) for item in matches[1:]):
            return canonical
        raise GfsPrecipitationError(
            f"multiple divergent APCP candidates share startStep={expected_start!r}, "
            f"endStep={forecast_hour!r}; ambiguous bucket selection"
        )
    return matches[0]


_EQUIVALENCE_TOLERANCE_KG_M2 = 1e-6


def validate_dual_parent_equivalence(
    bucket_record: ApcpCandidateRecord, continuous_record: ApcpCandidateRecord
) -> bool:
    """Section 2.4 (finding 4): for ``f=1..6`` the bucket and continuous
    accumulation records are duplicates (``startStep=0``). Canonicalize
    only when full arrays, start/end steps, statistical process, unit,
    grid shape, and quality mask are *all* equivalent; return the
    equivalence result (never silently assume equivalence -- the
    caller records both parents plus this boolean in lineage)."""
    if bucket_record.start_step != continuous_record.start_step:
        return False
    if bucket_record.end_step != continuous_record.end_step:
        return False
    if bucket_record.is_accumulation != continuous_record.is_accumulation:
        return False
    if bucket_record.unit_id != continuous_record.unit_id:
        return False
    if bucket_record.grid_shape != continuous_record.grid_shape:
        return False
    if bucket_record.mask != continuous_record.mask:
        return False
    if len(bucket_record.values_kg_m2) != len(continuous_record.values_kg_m2):
        return False
    return all(
        abs(a - b) <= _EQUIVALENCE_TOLERANCE_KG_M2
        for a, b in zip(bucket_record.values_kg_m2, continuous_record.values_kg_m2, strict=True)
    )


QPF_INTERVAL_COMPOSITION_POLICY = "mesoforge.qpf-exact-interval-sum.v1"
QpfAmountState = Literal["known", "missing", "unavailable"]


class QpfIntervalError(MesoForgeError):
    """An interval amount or exact cover is scientifically incompatible."""


def _qpf_hour(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise QpfIntervalError("QPF interval times must be timezone-aware")
    value = value.astimezone(UTC)
    if value.minute or value.second or value.microsecond:
        raise QpfIntervalError("QPF interval times must be exact UTC hours")
    return value


def _qpf_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class QpfIntervalAmount:
    """Normalized total liquid-equivalent amount over one exact (start, end].

    Units stay as provided; mm and kg/m² must not be mixed implicitly. Spatial
    support identifies the fixed native extraction and target. Evidence refs
    point to retained native/normalized parents rather than embedding grids.
    This is not a precipitation probability, frozen amount, or accreted ice.
    """

    source: str
    cycle: datetime
    interval_start: datetime
    interval_end: datetime
    unit: str
    spatial_support: str
    value: float | None
    evidence_refs: Mapping[str, str]
    state: QpfAmountState = "known"
    missing_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label in ("source", "spatial_support"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value or value != value.strip():
                raise QpfIntervalError(f"QPF {label} must be a nonblank identity")
        if self.unit not in ("mm", "kg/m^2"):
            raise QpfIntervalError("QPF requires normalized mm or kg/m^2 units")
        cycle, start, end = map(_qpf_hour, (self.cycle, self.interval_start, self.interval_end))
        if not cycle <= start < end:
            raise QpfIntervalError("QPF requires cycle <= interval start < interval end")
        for label, value in (("cycle", cycle), ("interval_start", start), ("interval_end", end)):
            object.__setattr__(self, label, value)
        if not isinstance(self.missing_reasons, tuple) or any(
            not isinstance(reason, str) or not reason.strip() for reason in self.missing_reasons
        ):
            raise QpfIntervalError("QPF missing reasons must be immutable nonblank strings")
        if self.state == "known":
            if (
                self.value is None
                or type(self.value) not in (int, float)
                or not math.isfinite(self.value)
                or self.value < 0
                or self.missing_reasons
            ):
                raise QpfIntervalError(
                    "Known QPF must be finite, nonnegative and without missingness"
                )
        elif self.state in ("missing", "unavailable"):
            if self.value is not None or not self.missing_reasons:
                raise QpfIntervalError("Missing/unavailable QPF requires None and explicit reasons")
        else:
            raise QpfIntervalError("Unrecognized QPF amount state")
        if (
            not isinstance(self.evidence_refs, Mapping)
            or not self.evidence_refs
            or any(
                not isinstance(key, str)
                or not key.strip()
                or not isinstance(value, str)
                or not value.strip()
                for key, value in self.evidence_refs.items()
            )
        ):
            raise QpfIntervalError("QPF requires retained nonblank evidence references")
        object.__setattr__(self, "evidence_refs", MappingProxyType(dict(self.evidence_refs)))

    @property
    def identity(self) -> tuple[str | datetime, ...]:
        return self.source, self.cycle, self.unit, self.spatial_support

    def payload(self) -> dict[str, Any]:
        return {
            "field": "liquid_equivalent_precipitation_amount",
            "source": self.source,
            "cycle": _qpf_iso(self.cycle),
            "source_lead_hours": int((self.interval_end - self.cycle).total_seconds() / 3600),
            "interval_start": _qpf_iso(self.interval_start),
            "interval_end": _qpf_iso(self.interval_end),
            "interval_closure": "left_open_right_closed",
            "temporal_semantics": "accumulation",
            "unit": self.unit,
            "spatial_support": self.spatial_support,
            "value": self.value,
            "state": self.state,
            "missing_reasons": list(self.missing_reasons),
            "evidence_refs": dict(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class QpfIntervalComposition:
    """Exact aggregate retaining every input, including explicit absent amounts."""

    interval_start: datetime
    interval_end: datetime
    value: float | None
    state: QpfAmountState
    inputs: tuple[QpfIntervalAmount, ...]

    def payload(self) -> dict[str, Any]:
        first = self.inputs[0]
        return {
            "policy": QPF_INTERVAL_COMPOSITION_POLICY,
            "operation": "native_interval" if len(self.inputs) == 1 else "sum_contiguous_intervals",
            "field": "liquid_equivalent_precipitation_amount",
            "source": first.source,
            "cycle": _qpf_iso(first.cycle),
            "unit": first.unit,
            "spatial_support": first.spatial_support,
            "interval_start": _qpf_iso(self.interval_start),
            "interval_end": _qpf_iso(self.interval_end),
            "duration_hours": int((self.interval_end - self.interval_start).total_seconds() / 3600),
            "interval_closure": "left_open_right_closed",
            "temporal_semantics": "accumulation",
            "value": self.value,
            "state": self.state,
            "missing_reasons": list(
                dict.fromkeys(reason for row in self.inputs for reason in row.missing_reasons)
            ),
            "inputs": [row.payload() for row in self.inputs],
        }


def compose_qpf_interval(
    intervals: Sequence[QpfIntervalAmount],
    *,
    target_start: datetime,
    target_end: datetime,
) -> QpfIntervalComposition:
    """Sum exactly one contiguous cover; never select, split or repair events.

    Callers supply the selected evidence, including missing interval records.
    Overlap, duplicates, gaps, extra intervals or mixed identity fail explicitly.
    A complete time cover with an absent amount returns no numerical aggregate:
    unavailable dominates missing; individual native states remain in ``inputs``.
    """
    start, end = _qpf_hour(target_start), _qpf_hour(target_end)
    if start >= end:
        raise QpfIntervalError("QPF target interval must have positive duration")
    if not intervals or any(not isinstance(row, QpfIntervalAmount) for row in intervals):
        raise QpfIntervalError("QPF composition requires validated interval evidence")
    ordered = tuple(sorted(intervals, key=lambda row: (row.interval_start, row.interval_end)))
    if any(row.identity != ordered[0].identity for row in ordered):
        raise QpfIntervalError("QPF intervals disagree on source, cycle, unit or spatial support")
    cursor = start
    for row in ordered:
        if row.interval_start != cursor or row.interval_end > end:
            raise QpfIntervalError(
                "QPF intervals must cover target exactly without gaps, overlap or splitting"
            )
        cursor = row.interval_end
    if cursor != end:
        raise QpfIntervalError("QPF intervals do not completely cover target")
    state: QpfAmountState = "known"
    if any(row.state == "unavailable" for row in ordered):
        state = "unavailable"
    elif any(row.state == "missing" for row in ordered):
        state = "missing"
    total = None
    if state == "known":
        try:
            total = math.fsum(row.value for row in ordered if row.value is not None)
        except OverflowError as exc:
            raise QpfIntervalError("QPF interval sum must remain finite") from exc
        if not math.isfinite(total):
            raise QpfIntervalError("QPF interval sum must remain finite")
    return QpfIntervalComposition(start, end, total, state, ordered)
