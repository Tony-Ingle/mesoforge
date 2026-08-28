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
