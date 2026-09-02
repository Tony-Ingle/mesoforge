"""Strict, network-free provider settings for HRRR and AviationWeather
(plan Section 2.1-2.4, Section 3.2 ``HrrrSourceSettings``/
``AviationWeatherSettings``).

These models hold only validated configuration data -- no HTTP client,
no Herbie/cfgrib import, no I/O. Concrete network adapters live in
``guidance/sources/hrrr.py`` and ``observations/sources/aviationweather.py``.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.catalog.grid_profiles import NbmGridProfile, require_approved_nbm_grid_profile

# Every eccodes key the approved NBM grid contract asserts in
# ``guidance.sources.nbm_decoding``. Requesting them is mandatory: an
# unrequested key decodes as absent, and an absent key would make the
# grid assertion fail open again (Codex re-review finding 2).
_NBM_REQUIRED_GRID_READ_KEYS: tuple[str, ...] = (
    "gridType",
    "Nx",
    "Ny",
    "DxInMetres",
    "DyInMetres",
    "LoVInDegrees",
    "LaDInDegrees",
    "Latin1InDegrees",
    "Latin2InDegrees",
    "latitudeOfFirstGridPointInDegrees",
    "longitudeOfFirstGridPointInDegrees",
    "iScansNegatively",
    "jScansPositively",
    "jPointsAreConsecutive",
    "shapeOfTheEarth",
    "radius",
)


class HrrrFieldAssertion(BaseModel):
    """One row of the Section 2.1 semantic-assertion table: the exact
    inventory selector plus the GRIB identity it must decode to."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    canonical_variable_id: str
    inventory_selector: str
    discipline: int
    parameter_category: int
    parameter_number: int
    type_of_level: str
    level: float
    expected_unit_id: str


class RetryPolicy(BaseModel):
    """Shared deterministic retry/backoff shape (Section 2.3/2.4): no
    random jitter, a bounded ``Retry-After`` cap, and a fixed attempt
    count per endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    connect_timeout_seconds: float
    read_timeout_seconds: float
    attempts_per_endpoint: int
    backoff_seconds: tuple[float, ...]
    retry_after_cap_seconds: float

    @model_validator(mode="after")
    def _check_timeouts(self) -> RetryPolicy:
        if self.connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        if self.read_timeout_seconds <= 0:
            raise ValueError("read_timeout_seconds must be positive")
        if self.retry_after_cap_seconds <= 0:
            raise ValueError("retry_after_cap_seconds must be positive")
        return self

    @model_validator(mode="after")
    def _check_attempts_and_backoff(self) -> RetryPolicy:
        if self.attempts_per_endpoint < 1:
            raise ValueError("attempts_per_endpoint must be >= 1")
        if len(self.backoff_seconds) == 0:
            raise ValueError("backoff_seconds must be non-empty")
        if any((not math.isfinite(v)) or v <= 0 for v in self.backoff_seconds):
            raise ValueError("backoff_seconds must all be finite and positive")
        pairs = zip(self.backoff_seconds, self.backoff_seconds[1:], strict=False)
        if any(b <= a for a, b in pairs):
            raise ValueError(
                "backoff_seconds must be strictly increasing (deterministic backoff, "
                "no random jitter)"
            )
        return self


class HrrrSourceSettings(BaseModel):
    """Section 2.1-2.3: exact HRRR templates, selectors, endpoint order,
    field assertions, and retry/availability policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["hrrr-source-settings.v1"] = "hrrr-source-settings.v1"
    model: Literal["hrrr"] = "hrrr"
    product: Literal["sfc"] = "sfc"
    sector: Literal["conus"] = "conus"
    cycle_frequency: Literal["hourly"] = "hourly"
    forecast_hours: tuple[int, ...]
    file_template: str
    index_suffix: Literal[".idx"] = ".idx"
    endpoint_order: tuple[Literal["aws", "nomads"], ...]
    endpoint_url_templates: dict[str, str]
    field_assertions: tuple[HrrrFieldAssertion, ...]
    read_keys: tuple[str, ...]
    retry_policy: RetryPolicy
    cycle_availability_deadline_minutes: float

    @model_validator(mode="after")
    def _check_forecast_hours(self) -> HrrrSourceSettings:
        if tuple(self.forecast_hours) != tuple(range(7)):
            raise ValueError(
                f"forecast_hours must be exactly 0..6 inclusive for Phase 1, got "
                f"{self.forecast_hours!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_endpoint_order(self) -> HrrrSourceSettings:
        if self.endpoint_order != ("aws", "nomads"):
            raise ValueError(
                f"endpoint_order must be exactly ('aws', 'nomads') for Phase 1, got "
                f"{self.endpoint_order!r}"
            )
        for name in self.endpoint_order:
            if name not in self.endpoint_url_templates:
                raise ValueError(f"endpoint_url_templates is missing an entry for {name!r}")
        return self

    @model_validator(mode="after")
    def _check_field_assertions(self) -> HrrrSourceSettings:
        variable_ids = [a.canonical_variable_id for a in self.field_assertions]
        if len(set(variable_ids)) != len(variable_ids):
            raise ValueError("field_assertions must not declare a duplicate canonical_variable_id")
        if len(self.field_assertions) != 3:
            raise ValueError(
                f"Phase 1 requires exactly 3 field assertions (temperature, U, V), got "
                f"{len(self.field_assertions)}"
            )
        return self

    @model_validator(mode="after")
    def _check_cycle_deadline(self) -> HrrrSourceSettings:
        if self.cycle_availability_deadline_minutes <= 0:
            raise ValueError("cycle_availability_deadline_minutes must be positive")
        return self


class Phase2FieldContract(BaseModel):
    """One canonical-variable source field contract for a Phase 2 model
    family (plan Section 2.2-2.4). Unlike ``HrrrFieldAssertion``, no
    static ``inventory_selector`` is stored here: Phase 2 inventory
    selectors are lead-dependent (rolling/bucket accumulation windows,
    per-lead step text) and are computed by the model-specific source
    module (``guidance/sources/hrrr.py``, ``nbm.py``, ``gfs.py``) from
    ``canonical_variable_id`` and the resolved source forecast hour --
    decoded GRIB keys remain authoritative regardless.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    canonical_variable_id: str
    discipline: int
    parameter_category: int
    parameter_number: int
    type_of_level: str
    level: float
    expected_unit_id: str
    is_accumulation: bool = False
    is_probability: bool = False


class ModelCycleHours(BaseModel):
    """Section 1.2: the allowed candidate cycle hours for one model
    family. ``hourly`` means every UTC hour is a candidate cycle;
    ``fixed`` pins the exact allowed set (HRRR/GFS use only the
    extended 00/06/12/18 cycles)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    cadence: Literal["hourly", "fixed"]
    hours: tuple[int, ...] = ()

    @model_validator(mode="after")
    def _check_shape(self) -> ModelCycleHours:
        if self.cadence == "hourly":
            if self.hours:
                raise ValueError("cadence='hourly' must not set an explicit 'hours' tuple")
        else:
            if not self.hours:
                raise ValueError("cadence='fixed' requires a non-empty 'hours' tuple")
            if any(not (0 <= h <= 23) for h in self.hours):
                raise ValueError(f"hours must each be in [0, 23], got {self.hours!r}")
            if tuple(sorted(set(self.hours))) != self.hours:
                raise ValueError("hours must be sorted, unique values")
        return self


class HrrrPhase2SourceSettings(BaseModel):
    """Section 1.2/2.1-2.2: HRRR extended-cycle Phase 2 settings.
    Distinct from Phase 1's ``HrrrSourceSettings`` (which is locked to
    the exact 7-hour 0..6 slice) -- Phase 2 candidates are only the
    00/06/12/18 UTC extended cycles, out through source lead <=48."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["hrrr-phase2-source-settings.v1"] = "hrrr-phase2-source-settings.v1"
    model: Literal["hrrr"] = "hrrr"
    product: Literal["sfc"] = "sfc"
    sector: Literal["conus"] = "conus"
    file_template: str
    index_suffix: Literal[".idx"] = ".idx"
    endpoint_order: tuple[Literal["aws", "nomads"], ...]
    endpoint_url_templates: dict[str, str]
    field_contracts: tuple[Phase2FieldContract, ...]
    read_keys: tuple[str, ...]
    retry_policy: RetryPolicy
    allowed_cycle_hours: tuple[int, ...]
    max_age_hours: float
    cycle_completion_deadline_minutes: float
    max_source_lead_hours: int

    @model_validator(mode="after")
    def _check_allowed_cycle_hours(self) -> HrrrPhase2SourceSettings:
        if self.allowed_cycle_hours != (0, 6, 12, 18):
            raise ValueError(
                f"HRRR Phase 2 allowed_cycle_hours must be exactly (0, 6, 12, 18), got "
                f"{self.allowed_cycle_hours!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_max_age_and_deadline(self) -> HrrrPhase2SourceSettings:
        if self.max_age_hours != 6.0:
            raise ValueError(
                f"HRRR Phase 2 max_age_hours must be exactly 6.0, got {self.max_age_hours!r}"
            )
        if self.cycle_completion_deadline_minutes != 120.0:
            raise ValueError(
                "HRRR Phase 2 cycle_completion_deadline_minutes must be exactly 120.0, got "
                f"{self.cycle_completion_deadline_minutes!r}"
            )
        if self.max_source_lead_hours != 48:
            raise ValueError(
                f"HRRR Phase 2 max_source_lead_hours must be exactly 48, got "
                f"{self.max_source_lead_hours!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_field_contracts(self) -> HrrrPhase2SourceSettings:
        ids = tuple(fc.canonical_variable_id for fc in self.field_contracts)
        expected = (
            "air_temperature_2m",
            "dew_point_temperature_2m",
            "eastward_wind_10m",
            "northward_wind_10m",
            "wind_gust_10m",
            "liquid_equivalent_precipitation_amount_1h",
        )
        if set(ids) != set(expected) or len(ids) != len(expected):
            raise ValueError(
                f"HRRR Phase 2 field_contracts must declare exactly {sorted(expected)!r}, got "
                f"{sorted(ids)!r}"
            )
        return self


class NbmSourceSettings(BaseModel):
    """Section 1.2/2.3: NBM CONUS core Phase 2 settings. PoP01 is the
    sole configured probability field; NBM is the sole PoP contributor
    (Section 4.5).

    ``grid_profile`` pins the exact approved operational projected grid
    (Codex re-review finding 2). It is validated against the approved
    registry at load time, so a configuration cannot introduce an
    unreviewed projection/shape/increment/scan/coverage combination, and
    decoding/normalization assert every decoded message against it
    rather than accepting any nonempty ``gridType`` and any positive
    ``Nx``/``Ny``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["nbm-source-settings.v1"] = "nbm-source-settings.v1"
    model: Literal["nbm"] = "nbm"
    product: Literal["core"] = "core"
    sector: Literal["co"] = "co"
    contract_profile: Literal["nbm-core-conus-operational.v1"] = "nbm-core-conus-operational.v1"
    file_template: str
    index_suffix: Literal[".idx"] = ".idx"
    endpoint_order: tuple[Literal["noaa_s3", "nomads"], ...]
    endpoint_url_templates: dict[str, str]
    field_contracts: tuple[Phase2FieldContract, ...]
    grid_profile: NbmGridProfile
    read_keys: tuple[str, ...]
    retry_policy: RetryPolicy
    max_age_hours: float
    cycle_completion_deadline_minutes: float
    probability_threshold_kg_m2: float

    @model_validator(mode="after")
    def _check_grid_profile_is_approved(self) -> NbmSourceSettings:
        require_approved_nbm_grid_profile(self.grid_profile)
        return self

    @model_validator(mode="after")
    def _check_read_keys_cover_the_grid_contract(self) -> NbmSourceSettings:
        """Every grid/projection/scan key the decoder must assert has to
        be requested from eccodes; otherwise the assertion would silently
        see ``None`` and the contract would fail open again."""
        missing = tuple(key for key in _NBM_REQUIRED_GRID_READ_KEYS if key not in self.read_keys)
        if missing:
            raise ValueError(
                "NBM read_keys must request every key the approved grid contract asserts; "
                f"missing {sorted(missing)!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_endpoint_order(self) -> NbmSourceSettings:
        if self.endpoint_order != ("noaa_s3", "nomads"):
            raise ValueError(
                f"NBM endpoint_order must be exactly ('noaa_s3', 'nomads'), got "
                f"{self.endpoint_order!r}"
            )
        for name in self.endpoint_order:
            if name not in self.endpoint_url_templates:
                raise ValueError(f"endpoint_url_templates is missing an entry for {name!r}")
        return self

    @model_validator(mode="after")
    def _check_max_age_and_deadline(self) -> NbmSourceSettings:
        if self.max_age_hours != 3.0:
            raise ValueError(f"NBM max_age_hours must be exactly 3.0, got {self.max_age_hours!r}")
        if self.cycle_completion_deadline_minutes != 90.0:
            raise ValueError(
                "NBM cycle_completion_deadline_minutes must be exactly 90.0, got "
                f"{self.cycle_completion_deadline_minutes!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_probability_threshold(self) -> NbmSourceSettings:
        if abs(self.probability_threshold_kg_m2 - 0.254) > 1e-9:
            raise ValueError(
                "NBM probability_threshold_kg_m2 must be exactly 0.254 (0.01 in liquid), got "
                f"{self.probability_threshold_kg_m2!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_field_contracts(self) -> NbmSourceSettings:
        ids = tuple(fc.canonical_variable_id for fc in self.field_contracts)
        expected = (
            "air_temperature_2m",
            "dew_point_temperature_2m",
            "wind_speed_10m",
            "wind_from_direction_10m",
            "wind_gust_10m",
            "liquid_equivalent_precipitation_amount_1h",
            "probability_of_precipitation_1h",
        )
        if set(ids) != set(expected) or len(ids) != len(expected):
            raise ValueError(
                f"NBM field_contracts must declare exactly {sorted(expected)!r}, got "
                f"{sorted(ids)!r}"
            )
        probability_ids = {
            fc.canonical_variable_id for fc in self.field_contracts if fc.is_probability
        }
        if probability_ids != {"probability_of_precipitation_1h"}:
            raise ValueError(
                "exactly and only 'probability_of_precipitation_1h' must be marked "
                f"is_probability=True, got {sorted(probability_ids)!r}"
            )
        return self


class GfsSourceSettings(BaseModel):
    """Section 1.2/2.4: GFS 0.25-degree pgrb2 Phase 2 settings. GFS
    contributes no probability field (Section 4.5)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["gfs-source-settings.v1"] = "gfs-source-settings.v1"
    model: Literal["gfs"] = "gfs"
    product_profile: Literal["gfs-pgrb2-0p25.v1"] = "gfs-pgrb2-0p25.v1"
    file_template: str
    index_suffix: Literal[".idx"] = ".idx"
    endpoint_order: tuple[Literal["gcs_archive", "aws_archive", "nomads"], ...]
    endpoint_url_templates: dict[str, str]
    field_contracts: tuple[Phase2FieldContract, ...]
    read_keys: tuple[str, ...]
    retry_policy: RetryPolicy
    allowed_cycle_hours: tuple[int, ...]
    max_age_hours: float
    cycle_completion_deadline_minutes: float
    max_source_lead_hours: int

    @model_validator(mode="after")
    def _check_allowed_cycle_hours(self) -> GfsSourceSettings:
        if self.allowed_cycle_hours != (0, 6, 12, 18):
            raise ValueError(
                f"GFS allowed_cycle_hours must be exactly (0, 6, 12, 18), got "
                f"{self.allowed_cycle_hours!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_max_age_and_deadline(self) -> GfsSourceSettings:
        if self.max_age_hours != 12.0:
            raise ValueError(f"GFS max_age_hours must be exactly 12.0, got {self.max_age_hours!r}")
        if self.cycle_completion_deadline_minutes != 360.0:
            raise ValueError(
                "GFS cycle_completion_deadline_minutes must be exactly 360.0, got "
                f"{self.cycle_completion_deadline_minutes!r}"
            )
        if self.max_source_lead_hours != 48:
            raise ValueError(
                f"GFS max_source_lead_hours must be exactly 48, got {self.max_source_lead_hours!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_field_contracts(self) -> GfsSourceSettings:
        ids = tuple(fc.canonical_variable_id for fc in self.field_contracts)
        expected = (
            "air_temperature_2m",
            "dew_point_temperature_2m",
            "eastward_wind_10m",
            "northward_wind_10m",
            "wind_gust_10m",
            "liquid_equivalent_precipitation_amount_1h",
        )
        if set(ids) != set(expected) or len(ids) != len(expected):
            raise ValueError(
                f"GFS field_contracts must declare exactly {sorted(expected)!r}, got "
                f"{sorted(ids)!r}"
            )
        return self


class AviationWeatherSettings(BaseModel):
    """Section 2.4: exact endpoints, query ordering, rate limiting, and
    retry policy for AviationWeather.gov."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["aviationweather-source-settings.v1"] = (
        "aviationweather-source-settings.v1"
    )
    base_url: str
    stationinfo_path: str
    metar_path: str
    query_parameter_order: tuple[str, ...]
    user_agent: str
    max_requests_per_minute: int
    min_request_interval_seconds: float
    metar_window_hours: float
    metar_completion_offset_minutes: float
    retry_policy: RetryPolicy

    @model_validator(mode="after")
    def _check_query_parameter_order(self) -> AviationWeatherSettings:
        if self.query_parameter_order != ("ids", "format", "date", "hours"):
            raise ValueError(
                "query_parameter_order must be exactly ('ids', 'format', 'date', 'hours'), got "
                f"{self.query_parameter_order!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_rate_limit(self) -> AviationWeatherSettings:
        if not (0 < self.max_requests_per_minute <= 100):
            raise ValueError(
                "max_requests_per_minute must be in (0, 100] per the documented provider limit"
            )
        if self.min_request_interval_seconds <= 0:
            raise ValueError("min_request_interval_seconds must be positive")
        # Residual review finding (Codex review t_30309949):
        # ``RequestRateLimiter.wait()`` is now invoked by
        # ``observations.acquisition._fetch_with_retry`` immediately
        # before *every* actual transport attempt -- the first request
        # and every retry -- so when a rate limiter is supplied the
        # limiter itself is authoritative and this backoff/interval
        # relationship is no longer required for correctness. This
        # check is retained as defense-in-depth for any call site that
        # invokes ``acquire_stationinfo``/``acquire_metar_batch``
        # directly without an injected limiter (``rate_limiter=None``),
        # where ``retry_policy.backoff_seconds`` alone spaces retries:
        # requiring the first (smallest) backoff to be at least the
        # configured minimum interval keeps even that unthrottled path
        # from firing retries closer together than
        # ``min_request_interval_seconds``. ``backoff_seconds`` is
        # already required to be strictly increasing, so this alone
        # guarantees every later retry gap is >= min_request_interval_seconds
        # too.
        if self.retry_policy.backoff_seconds[0] < self.min_request_interval_seconds:
            raise ValueError(
                "retry_policy.backoff_seconds[0] "
                f"({self.retry_policy.backoff_seconds[0]!r}) must be >= "
                f"min_request_interval_seconds ({self.min_request_interval_seconds!r}) so "
                "every retry attempt also respects the configured process interval"
            )
        return self

    @model_validator(mode="after")
    def _check_window(self) -> AviationWeatherSettings:
        if self.metar_window_hours <= 0:
            raise ValueError("metar_window_hours must be positive")
        if self.metar_completion_offset_minutes < 0:
            raise ValueError("metar_completion_offset_minutes must be nonnegative")
        if not self.user_agent.strip():
            raise ValueError("user_agent must be a non-empty, descriptive value")
        return self
