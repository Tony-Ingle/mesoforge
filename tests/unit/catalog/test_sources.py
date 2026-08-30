"""Unit tests for mesoforge.catalog.sources (Phase 1 plan Section 2.1-2.4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mesoforge.catalog.sources import (
    AviationWeatherSettings,
    HrrrFieldAssertion,
    HrrrSourceSettings,
    RetryPolicy,
)

_RETRY = RetryPolicy(
    connect_timeout_seconds=10.0,
    read_timeout_seconds=60.0,
    attempts_per_endpoint=4,
    backoff_seconds=(1.0, 2.0, 4.0, 8.0),
    retry_after_cap_seconds=60.0,
)

_FIELD_ASSERTIONS = (
    HrrrFieldAssertion(
        canonical_variable_id="air_temperature_2m",
        inventory_selector=":TMP:2 m above ground:(anl|[0-9]+ hour fcst):",
        discipline=0,
        parameter_category=0,
        parameter_number=0,
        type_of_level="heightAboveGround",
        level=2.0,
        expected_unit_id="K",
    ),
    HrrrFieldAssertion(
        canonical_variable_id="eastward_wind_10m",
        inventory_selector=":UGRD:10 m above ground:(anl|[0-9]+ hour fcst):",
        discipline=0,
        parameter_category=2,
        parameter_number=2,
        type_of_level="heightAboveGround",
        level=10.0,
        expected_unit_id="m/s",
    ),
    HrrrFieldAssertion(
        canonical_variable_id="northward_wind_10m",
        inventory_selector=":VGRD:10 m above ground:(anl|[0-9]+ hour fcst):",
        discipline=0,
        parameter_category=2,
        parameter_number=3,
        type_of_level="heightAboveGround",
        level=10.0,
        expected_unit_id="m/s",
    ),
)


def _hrrr(**overrides: object) -> HrrrSourceSettings:
    values: dict[str, object] = {
        "forecast_hours": tuple(range(7)),
        "file_template": "hrrr.t{HH:02d}z.wrfsfcf{FF:02d}.grib2",
        "endpoint_order": ("aws", "nomads"),
        "endpoint_url_templates": {"aws": "https://aws/{FILE}", "nomads": "https://nomads/{FILE}"},
        "field_assertions": _FIELD_ASSERTIONS,
        "read_keys": ("discipline", "typeOfLevel"),
        "retry_policy": _RETRY,
        "cycle_availability_deadline_minutes": 90.0,
    }
    values.update(overrides)
    return HrrrSourceSettings(**values)  # type: ignore[arg-type]


class TestRetryPolicy:
    def test_accepts_deterministic_backoff(self) -> None:
        assert _RETRY.backoff_seconds == (1.0, 2.0, 4.0, 8.0)

    def test_rejects_non_increasing_backoff(self) -> None:
        with pytest.raises(ValidationError, match="strictly increasing"):
            RetryPolicy(
                connect_timeout_seconds=10.0,
                read_timeout_seconds=60.0,
                attempts_per_endpoint=4,
                backoff_seconds=(1.0, 1.0, 4.0, 8.0),
                retry_after_cap_seconds=60.0,
            )

    def test_rejects_zero_attempts(self) -> None:
        with pytest.raises(ValidationError):
            RetryPolicy(
                connect_timeout_seconds=10.0,
                read_timeout_seconds=60.0,
                attempts_per_endpoint=0,
                backoff_seconds=(1.0,),
                retry_after_cap_seconds=60.0,
            )

    def test_rejects_nonpositive_timeout(self) -> None:
        with pytest.raises(ValidationError):
            RetryPolicy(
                connect_timeout_seconds=0.0,
                read_timeout_seconds=60.0,
                attempts_per_endpoint=4,
                backoff_seconds=(1.0,),
                retry_after_cap_seconds=60.0,
            )


class TestHrrrSourceSettings:
    def test_accepts_exact_leads(self) -> None:
        settings = _hrrr()
        assert settings.forecast_hours == tuple(range(7))

    def test_rejects_lead_gap(self) -> None:
        with pytest.raises(ValidationError, match="forecast_hours"):
            _hrrr(forecast_hours=(0, 1, 2, 3, 4, 6))

    def test_rejects_extra_lead(self) -> None:
        with pytest.raises(ValidationError):
            _hrrr(forecast_hours=tuple(range(8)))

    def test_rejects_wrong_endpoint_order(self) -> None:
        with pytest.raises(ValidationError, match="endpoint_order"):
            _hrrr(endpoint_order=("nomads", "aws"))

    def test_rejects_missing_endpoint_template(self) -> None:
        with pytest.raises(ValidationError):
            _hrrr(endpoint_url_templates={"aws": "https://aws/{FILE}"})

    def test_rejects_duplicate_field_assertion(self) -> None:
        with pytest.raises(ValidationError):
            _hrrr(field_assertions=(_FIELD_ASSERTIONS[0], _FIELD_ASSERTIONS[0]))

    def test_rejects_wrong_field_assertion_count(self) -> None:
        with pytest.raises(ValidationError, match="exactly 3"):
            _hrrr(field_assertions=_FIELD_ASSERTIONS[:2])

    def test_rejects_nonpositive_cycle_deadline(self) -> None:
        with pytest.raises(ValidationError):
            _hrrr(cycle_availability_deadline_minutes=0.0)


class TestAviationWeatherSettings:
    def _settings(self, **overrides: object) -> AviationWeatherSettings:
        values: dict[str, object] = {
            "base_url": "https://aviationweather.gov",
            "stationinfo_path": "/api/data/stationinfo",
            "metar_path": "/api/data/metar",
            "query_parameter_order": ("ids", "format", "date", "hours"),
            "user_agent": "MesoForge/0.1",
            "max_requests_per_minute": 60,
            "min_request_interval_seconds": 1.0,
            "metar_window_hours": 6.5,
            "metar_completion_offset_minutes": 15.0,
            "retry_policy": _RETRY,
        }
        values.update(overrides)
        return AviationWeatherSettings(**values)  # type: ignore[arg-type]

    def test_accepts_valid_settings(self) -> None:
        settings = self._settings()
        assert settings.max_requests_per_minute == 60

    def test_rejects_wrong_query_parameter_order(self) -> None:
        with pytest.raises(ValidationError, match="query_parameter_order"):
            self._settings(query_parameter_order=("format", "ids", "date", "hours"))

    def test_rejects_rate_limit_above_documented_maximum(self) -> None:
        with pytest.raises(ValidationError):
            self._settings(max_requests_per_minute=200)

    def test_rejects_empty_user_agent(self) -> None:
        with pytest.raises(ValidationError):
            self._settings(user_agent="   ")

    def test_rejects_nonpositive_window(self) -> None:
        with pytest.raises(ValidationError):
            self._settings(metar_window_hours=0.0)

    def test_rejects_backoff_smaller_than_min_interval(self) -> None:
        """MEDIUM review finding 3: every actual HTTP attempt, including
        retries, must respect the configured process interval. The
        first (smallest) backoff step must be at least
        min_request_interval_seconds so no retry attempt can fire
        sooner than the configured throttle."""
        under_interval_retry = RetryPolicy(
            connect_timeout_seconds=10.0,
            read_timeout_seconds=60.0,
            attempts_per_endpoint=4,
            backoff_seconds=(0.1, 2.0, 4.0, 8.0),
            retry_after_cap_seconds=60.0,
        )
        with pytest.raises(ValidationError, match="backoff_seconds"):
            self._settings(min_request_interval_seconds=1.0, retry_policy=under_interval_retry)
