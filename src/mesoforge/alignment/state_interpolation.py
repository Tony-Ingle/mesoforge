"""Opt-in, versioned linear alignment of normalized instantaneous native states.

Legacy exact alignment remains unchanged. A caller must supply the source's
documented native cadence; this helper never treats an absent expected native
sample as permission to interpolate across a larger gap. It is not a forecast
blend, a provider eligibility decision, or an accumulation transformation.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal

from mesoforge.alignment.temporal import TemporalAlignmentError

STATE_INTERPOLATION_POLICY = "mesoforge.native-state-linear-interpolation.v1"
# These are the existing normalized surface/total-cloud bounds. Interval maxima,
# directions, probabilities, categories and amounts deliberately have no entry.
_FIELDS = {
    "air_temperature_2m": {"K": (150.0, 340.0)},
    "dew_point_temperature_2m": {"K": (150.0, 340.0)},
    "eastward_wind_10m": {"m/s": (-100.0, 100.0)},
    "northward_wind_10m": {"m/s": (-100.0, 100.0)},
    "cloud_area_fraction": {
        "1": (0.0, 1.0),
        "percent": (0.0, 100.0),
        "%": (0.0, 100.0),
    },
}


def _hour(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TemporalAlignmentError("State timestamps must be timezone-aware")
    value = value.astimezone(UTC)
    if value.minute or value.second or value.microsecond:
        raise TemporalAlignmentError("State timestamps must be exact UTC hours")
    return value


@dataclass(frozen=True, slots=True)
class NativeStateSample:
    """One normalized scalar at one spatial target, retaining native time/evidence.

    ``definition`` and ``vertical_extent`` identify physical meaning. The caller
    supplies ``spatial_support`` from its fixed native extraction contract so two
    different coordinates/grids cannot accidentally become temporal endpoints.
    Reference values are opaque immutable artifact identities/JSON pointers, not
    embedded provenance trees. No digest-prefix convention is imposed here.
    """

    source: str
    cycle: datetime
    field: str
    unit: str
    definition: str
    vertical_extent: str
    spatial_support: str
    valid_time: datetime
    value: float
    evidence_refs: Mapping[str, str]
    temporal_semantics: str = "instantaneous"

    def __post_init__(self) -> None:
        for name in ("source", "field", "unit", "definition", "vertical_extent", "spatial_support"):
            text = getattr(self, name)
            if not isinstance(text, str) or not text or text != text.strip():
                raise TemporalAlignmentError(f"State {name} must be a nonblank identity")
        if self.temporal_semantics != "instantaneous" or self.field not in _FIELDS:
            raise TemporalAlignmentError("Only supported instantaneous state fields may align")
        bounds = _FIELDS[self.field].get(self.unit)
        if bounds is None:
            raise TemporalAlignmentError("State units must match the normalized field contract")
        if (
            type(self.value) not in (int, float)
            or not math.isfinite(self.value)
            or not bounds[0] <= self.value <= bounds[1]
        ):
            raise TemporalAlignmentError("State value must be finite and within current bounds")
        if self.field == "cloud_area_fraction" and (
            self.definition != "total_cloud_cover" or self.vertical_extent != "entire_atmosphere"
        ):
            raise TemporalAlignmentError("Cloud alignment supports entire-atmosphere total cover")
        cycle, valid = _hour(self.cycle), _hour(self.valid_time)
        if valid < cycle:
            raise TemporalAlignmentError("Native state valid time cannot precede source cycle")
        object.__setattr__(self, "cycle", cycle)
        object.__setattr__(self, "valid_time", valid)
        if not isinstance(self.evidence_refs, Mapping) or not self.evidence_refs:
            raise TemporalAlignmentError("Native state requires retained evidence references")
        if any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
            for key, value in self.evidence_refs.items()
        ):
            raise TemporalAlignmentError("State evidence references must be nonblank strings")
        object.__setattr__(self, "evidence_refs", MappingProxyType(dict(self.evidence_refs)))

    @property
    def identity(self) -> tuple[str | datetime, ...]:
        return (
            self.source,
            self.cycle,
            self.field,
            self.unit,
            self.definition,
            self.vertical_extent,
            self.spatial_support,
            self.temporal_semantics,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "cycle": self.cycle.isoformat().replace("+00:00", "Z"),
            "field": self.field,
            "unit": self.unit,
            "definition": self.definition,
            "vertical_extent": self.vertical_extent,
            "spatial_support": self.spatial_support,
            "valid_time": self.valid_time.isoformat().replace("+00:00", "Z"),
            "source_lead_hours": int((self.valid_time - self.cycle).total_seconds() / 3600),
            "value": self.value,
            "evidence_refs": dict(self.evidence_refs),
            "temporal_semantics": self.temporal_semantics,
        }


@dataclass(frozen=True, slots=True)
class AlignedState:
    value: float
    target_valid_time: datetime
    mode: Literal["native", "interpolated"]
    native_step_hours: int
    endpoints: tuple[NativeStateSample, ...]
    weights: tuple[float, ...]
    native_lattice_origin: datetime

    def payload(self) -> dict[str, Any]:
        return {
            "policy": STATE_INTERPOLATION_POLICY,
            "value": self.value,
            "valid_time": self.target_valid_time.isoformat().replace("+00:00", "Z"),
            "mode": self.mode,
            "native_step_hours": self.native_step_hours,
            "native_lattice_origin": self.native_lattice_origin.isoformat().replace("+00:00", "Z"),
            "endpoints": [sample.payload() for sample in self.endpoints],
            "weights": list(self.weights),
        }


def align_state_samples(
    samples: Sequence[NativeStateSample],
    *,
    target_valid_time: datetime,
    native_step_hours: int,
    native_lattice_origin: datetime | None = None,
) -> AlignedState:
    """Use a native value or its two adjacent native endpoints; never extrapolate.

    Supported native spacings are 1/3/6 hours, plus an explicit two-hour cadence
    transition. Both endpoints must be one declared native step apart on the
    declared lattice (cycle-relative by default). Callers must not discard missing
    native records then invent a new cadence. Instantaneous U/V are independent
    components; direction is excluded.
    """
    target = _hour(target_valid_time)
    if type(native_step_hours) is not int or native_step_hours not in (1, 2, 3, 6):
        raise TemporalAlignmentError("State native cadence must be 1, 2, 3 or 6 integer hours")
    if native_step_hours == 2 and native_lattice_origin is None:
        raise TemporalAlignmentError("Two-hour cadence transition requires an explicit lattice")
    if not samples or any(not isinstance(sample, NativeStateSample) for sample in samples):
        raise TemporalAlignmentError("State alignment requires validated native samples")
    identity = samples[0].identity
    if any(sample.identity != identity for sample in samples):
        raise TemporalAlignmentError("State endpoints disagree on source, cycle or field support")
    ordered = sorted(samples, key=lambda sample: sample.valid_time)
    if len({sample.valid_time for sample in ordered}) != len(ordered):
        raise TemporalAlignmentError("Duplicate native state valid times are ambiguous")
    step_seconds = native_step_hours * 3600
    origin = _hour(native_lattice_origin) if native_lattice_origin is not None else ordered[0].cycle
    if any((sample.valid_time - origin).total_seconds() % step_seconds for sample in ordered):
        raise TemporalAlignmentError("Native state times disagree with the declared source cadence")
    for sample in ordered:
        if sample.valid_time == target:
            return AlignedState(
                sample.value, target, "native", native_step_hours, (sample,), (1.0,), origin
            )
    before = [sample for sample in ordered if sample.valid_time < target]
    after = [sample for sample in ordered if sample.valid_time > target]
    if not before or not after:
        raise TemporalAlignmentError("State interpolation cannot extrapolate or carry forward")
    left, right = before[-1], after[0]
    if (right.valid_time - left.valid_time).total_seconds() != step_seconds:
        raise TemporalAlignmentError("State interpolation cannot bridge a missing native step")
    right_weight = (target - left.valid_time).total_seconds() / step_seconds
    weights = (1.0 - right_weight, right_weight)
    value = (
        left.value
        if left.value == right.value
        else math.fsum((left.value * weights[0], right.value * weights[1]))
    )
    return AlignedState(
        value, target, "interpolated", native_step_hours, (left, right), weights, origin
    )
