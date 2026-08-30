"""Unit tests for mesoforge.catalog.stations (Phase 1 plan Section 3.2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mesoforge.catalog.stations import StationDefinition


def _station(**overrides: object) -> StationDefinition:
    values: dict[str, object] = {
        "station_id": "station.kcbg",
        "provider_icao_id": "KCBG",
        "expected_latitude": 45.557,
        "expected_longitude": -93.264,
        "expected_elevation_m": 285.0,
        "site_name": "Cambridge Muni",
        "provider_site_types": ("METAR",),
        "provider_priority": 1,
    }
    values.update(overrides)
    return StationDefinition(**values)  # type: ignore[arg-type]


class TestStationDefinition:
    def test_accepts_valid_station(self) -> None:
        station = _station()
        assert station.exposure_identity == "unknown"
        assert station.instrument_identity == "unknown"

    def test_rejects_lowercase_icao(self) -> None:
        with pytest.raises(ValidationError):
            _station(provider_icao_id="kcbg")

    def test_rejects_too_short_icao(self) -> None:
        with pytest.raises(ValidationError):
            _station(provider_icao_id="KC")

    def test_rejects_too_long_icao(self) -> None:
        with pytest.raises(ValidationError):
            _station(provider_icao_id="KCBGX")

    def test_rejects_out_of_range_latitude(self) -> None:
        with pytest.raises(ValidationError):
            _station(expected_latitude=95.0)

    def test_rejects_out_of_range_longitude(self) -> None:
        with pytest.raises(ValidationError):
            _station(expected_longitude=200.0)

    def test_rejects_non_finite_elevation(self) -> None:
        with pytest.raises(ValidationError):
            _station(expected_elevation_m=float("nan"))

    def test_rejects_missing_metar_capability(self) -> None:
        with pytest.raises(ValidationError, match="METAR"):
            _station(provider_site_types=("AWOS",))

    def test_rejects_negative_priority(self) -> None:
        with pytest.raises(ValidationError):
            _station(provider_priority=-1)

    def test_cannot_set_exposure_identity_to_known_value(self) -> None:
        with pytest.raises(ValidationError):
            _station(exposure_identity="rooftop")
