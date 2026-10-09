"""Nominal native time support, not discovery, freshness, or blend eligibility.

The catalogue describes specific public products. A retrieval plan is only a
request shape: provider inventories, decoded event metadata, retained evidence,
availability cutoffs and missingness must still prove each requested input.
No production adapter envelope or active field policy is changed here.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

NATIVE_HORIZON_CONTRACT = "mesoforge.native-field-horizons.v1"

_PRODUCTS = {
    "HRRR": "hrrr.conus.wrfsfcf",
    "RAP": "rap.awp130pgrb",
    "GFS": "gfs.pgrb2.0p25",
    "IFS": "ifs.open-data.oper.fc.0p25",
    "NBM": "nbm.v5.conus.core",
    "GEFS": "gefs.bias-corrected.pqpf.0p50",
}
_DOCUMENTATION = {
    "HRRR": "https://www.nco.ncep.noaa.gov/pmb/products/hrrr/",
    "RAP": "https://www.nco.ncep.noaa.gov/pmb/products/rap/",
    "GFS": "https://www.nco.ncep.noaa.gov/pmb/products/gfs/",
    "IFS": "https://www.ecmwf.int/en/forecasts/datasets/open-data",
    "NBM": "https://www.nco.ncep.noaa.gov/pmb/products/blend/",
    "GEFS": "https://www.nco.ncep.noaa.gov/pmb/products/gens/",
}
TemporalKind = Literal[
    "state", "instantaneous_gust", "interval_maximum", "accumulation", "probability"
]
PlanStatus = Literal["native", "bracketed", "outside_native_horizon", "no_native_event"]


def _hour(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Native time support requires timezone-aware timestamps")
    value = value.astimezone(UTC)
    if value.minute or value.second or value.microsecond:
        raise ValueError("Native time support requires exact UTC hours")
    return value


def _source_leads(source: str, cycle: datetime) -> tuple[int, ...]:
    if source == "HRRR":
        return tuple(range(49 if cycle.hour in (0, 6, 12, 18) else 19))
    if source == "RAP":
        return tuple(range(52 if cycle.hour in (3, 9, 15, 21) else 22))
    if source in ("GFS", "IFS", "GEFS") and cycle.hour not in (0, 6, 12, 18):
        raise ValueError(f"{source} has no nominal cycle at this UTC hour")
    if source == "GFS":
        return (*range(121), *range(123, 385, 3))
    if source == "IFS":
        # Open-data oper/fc, not the ensemble or AIFS dissemination schedule.
        return (
            (*range(0, 145, 3), *range(150, 361, 6))
            if cycle.hour in (0, 12)
            else tuple(range(0, 145, 3))
        )
    if source == "NBM":
        # Beyond the hourly segment the product aligns to valid UTC hours,
        # not to lead multiples from every hourly NBM cycle.
        return (
            *range(1, 49),
            *(lead for lead in range(49, 193) if (cycle.hour + lead) % 3 == 0),
            *(lead for lead in range(193, 265) if (cycle.hour + lead) % 6 == 0),
        )
    if source == "GEFS":
        return tuple(range(6, 385, 6))
    raise ValueError(f"Unregistered native source: {source}")


@dataclass(frozen=True, slots=True)
class NativeRetrievalPlan:
    """Requested native endpoints only; their existence/validity is unproven."""

    source: str
    product: str
    field: str
    cycle: datetime
    valid_time: datetime
    status: PlanStatus
    source_leads: tuple[int, ...]
    native_step_hours: int | None
    expected_interval: tuple[datetime, datetime] | None
    contract: str = NATIVE_HORIZON_CONTRACT


@dataclass(frozen=True, slots=True)
class NativeFieldContract:
    source: str
    field: str
    temporal_kind: TemporalKind
    normalization: str
    maximum_lead_hours: int | None = None
    interval_hours: int | None = None

    @property
    def product(self) -> str:
        return _PRODUCTS[self.source]

    @property
    def documentation(self) -> str:
        return _DOCUMENTATION[self.source]

    def native_leads(self, cycle: datetime) -> tuple[int, ...]:
        cycle = _hour(cycle)
        leads = _source_leads(self.source, cycle)
        if self.source == "NBM" and self.field == "liquid_equivalent_precipitation_amount_1h":
            leads = tuple(range(1, 265))
        return tuple(
            lead
            for lead in leads
            if (self.maximum_lead_hours is None or lead <= self.maximum_lead_hours)
            and (self.interval_hours is None or lead >= self.interval_hours)
            and (self.field != "probability_of_precipitation_6h" or (cycle.hour + lead) % 6 == 0)
            and (
                self.temporal_kind not in ("accumulation", "probability", "interval_maximum")
                or lead > 0
            )
        )

    def plan(self, cycle: datetime, valid_time: datetime) -> NativeRetrievalPlan:
        """Account for source age through absolute times; never extend its horizon.

        State bracketing requests both adjacent *scheduled* endpoints, so a
        missing native endpoint cannot silently turn into a wider interpolation.
        All interval products and gusts require their exact native valid time.
        Expected one-hour intervals must still match the actual GRIB metadata.
        """
        cycle, valid_time = _hour(cycle), _hour(valid_time)
        leads = self.native_leads(cycle)
        target = int((valid_time - cycle).total_seconds() // 3600)
        status: PlanStatus
        requested: tuple[int, ...] = ()
        step = None
        interval = None
        if not leads or target < leads[0] or target > leads[-1]:
            status = "outside_native_horizon"
        elif target in leads:
            status, requested = "native", (target,)
            if self.interval_hours is not None:
                interval = (valid_time - timedelta(hours=self.interval_hours), valid_time)
        elif self.temporal_kind == "state":
            index = bisect_left(leads, target)
            requested = (leads[index - 1], leads[index])
            step = requested[1] - requested[0]
            status = "bracketed"
        else:
            status = "no_native_event"
        return NativeRetrievalPlan(
            self.source,
            self.product,
            self.field,
            cycle,
            valid_time,
            status,
            requested,
            step,
            interval,
        )


# These fields have explicit instantaneous scalar/component definitions suitable
# for the separate state-interpolation kernel. NBM winds must first be converted
# from its native speed/direction, never interpolate compass-direction degrees.
_STATE_FIELDS = (
    "air_temperature_2m",
    "dew_point_temperature_2m",
    "eastward_wind_10m",
    "northward_wind_10m",
    "cloud_area_fraction",
)
_CONTRACTS = {
    (source, field): NativeFieldContract(
        source,
        field,
        "state",
        "earth_relative_components_from_native_speed_direction"
        if source == "NBM" and field in ("eastward_wind_10m", "northward_wind_10m")
        else "normalized_native_state",
    )
    for source in ("HRRR", "RAP", "GFS", "IFS", "NBM")
    for field in _STATE_FIELDS
}
for _source in ("HRRR", "RAP", "GFS", "NBM"):
    _CONTRACTS[_source, "wind_gust_10m"] = NativeFieldContract(
        _source, "wind_gust_10m", "instantaneous_gust", "exact_native_instantaneous_gust"
    )
    # RAP's nominal hourly request does not create a normalized RAP QPF stream;
    # acquisition/decoding must still establish its exact accumulation interval.
    _CONTRACTS[_source, "liquid_equivalent_precipitation_amount_1h"] = NativeFieldContract(
        _source,
        "liquid_equivalent_precipitation_amount_1h",
        "accumulation",
        "exact_one_hour_native_or_validated_accumulation_difference",
        maximum_lead_hours=120 if _source == "GFS" else None,
        interval_hours=1,
    )
    _CONTRACTS[_source, "liquid_equivalent_precipitation_amount"] = NativeFieldContract(
        _source,
        "liquid_equivalent_precipitation_amount",
        "accumulation",
        "native_encoded_accumulation_interval_required",
    )

# Distinct IFS interval products are inspectable evidence, not instantaneous gust
# or hourly QPF. The encoded native intervals remain authoritative.
_IFS_INTERVAL_FIELDS: tuple[tuple[str, TemporalKind, str], ...] = (
    ("wind_gust_10m_interval_maximum", "interval_maximum", "maximum_since_previous_postprocessing"),
    ("liquid_equivalent_precipitation_amount", "accumulation", "cycle_accumulation"),
)
for _field, _kind, _normalization in _IFS_INTERVAL_FIELDS:
    _CONTRACTS["IFS", _field] = NativeFieldContract("IFS", _field, _kind, _normalization)

# NBM's file horizon is not its hourly probability horizon. Later 3/6/12-hour
# probability events need their own contracts and never fill hourly gaps.
_CONTRACTS["NBM", "probability_of_precipitation_1h"] = NativeFieldContract(
    "NBM",
    "probability_of_precipitation_1h",
    "probability",
    "native_event_threshold_preserved",
    48,
    1,
)
_CONTRACTS["NBM", "probability_of_thunder_1h"] = NativeFieldContract(
    "NBM", "probability_of_thunder_1h", "probability", "native_thunder_event_preserved", 36, 1
)
for _source in ("NBM", "GEFS"):
    _CONTRACTS[_source, "probability_of_precipitation_6h"] = NativeFieldContract(
        _source,
        "probability_of_precipitation_6h",
        "probability",
        "native_six_hour_gt_0.254_kg_m2_gridpoint_probability",
        interval_hours=6,
    )


def native_field_contract(source: str, field: str) -> NativeFieldContract:
    """Reject unknown combinations rather than borrowing another field's range."""
    try:
        return _CONTRACTS[source, field]
    except KeyError as exc:
        raise ValueError(f"Unregistered native source/field contract: {source}/{field}") from exc
