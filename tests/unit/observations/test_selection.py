"""Independent geometry, timing, revision, and field-QC checks for the preview."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mesoforge.catalog.configuration import ObservationNormalizationPolicy
from mesoforge.catalog.stations import StationDefinition
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.contracts.observations_v2 import NormalizedObservationV2
from mesoforge.observations.selection import select_temperature_observation

_TIME = datetime(2026, 9, 10, 13, tzinfo=UTC)
_SNAPSHOT = ArtifactId("art_00000000-0000-0000-0000-000000000001")
_RAW = ArtifactId("art_00000000-0000-0000-0000-000000000002")
_POLICY = ObservationNormalizationPolicy(policy_id="metar-normalization.v1")


def _station(icao: str = "KAAA", *, lat: float = 0.0, lon: float = 0.1) -> StationDefinition:
    return StationDefinition.model_validate(
        {
            "station_id": f"station.{icao.lower()}",
            "provider_icao_id": icao,
            "expected_latitude": lat,
            "expected_longitude": lon,
            "expected_elevation_m": 100.0,
            "site_name": "Synthetic test station",
            "provider_site_types": ("METAR",),
            "provider_priority": 0,
        }
    )


def _row(
    station: StationDefinition,
    *,
    seconds: int = 0,
    revision: str = "original",
    **overrides: Any,
) -> NormalizedObservationV2:
    event = _TIME + timedelta(seconds=seconds)
    values = {
        "logical_observation_digest": Digest.of_bytes(f"{station.station_id}/{event}".encode()),
        "revision_digest": Digest.of_bytes(revision.encode()),
        "station_id": station.station_id,
        "provider_station_id": station.provider_icao_id,
        "event_time": event,
        "report_time": event,
        "provider_available_at": event + timedelta(minutes=1),
        "ingested_at": event + timedelta(minutes=2),
        "metar_type": "METAR",
        "raw_observation": "Synthetic test report",
        "raw_record_digest": Digest.of_bytes(b"synthetic raw record"),
        "raw_artifact_id": _RAW,
        "raw_record_index": 0,
        "station_snapshot_artifact_id": _SNAPSHOT,
        "latitude_degrees": station.expected_latitude,
        "longitude_degrees": station.expected_longitude,
        "elevation_m": station.expected_elevation_m,
        "temperature_k": 288.15,
        "provider_qc_field": 0.0,
        "mesoforge_qc_state": "eligible",
    }
    values.update(overrides)
    return NormalizedObservationV2.model_validate(values)


def _select(
    stations: tuple[StationDefinition, ...], rows: list[NormalizedObservationV2]
) -> dict[str, Any]:
    return select_temperature_observation(
        latitude=0.0,
        longitude=0.0,
        valid_time=_TIME,
        observations=rows,
        station_snapshots={_SNAPSHOT: stations},
        policy=_POLICY,
    )


def test_nearest_station_wins_over_closer_observation_time_and_preserves_lineage() -> None:
    near, far = _station("KAAA", lon=0.1), _station("KBBB", lon=0.2)
    near_row, far_row = _row(near, seconds=899), _row(far)
    result = _select((far, near), [far_row, near_row])
    selected = result["selected"]
    assert result["status"] == "matched"
    assert selected["station_id"] == "KAAA"
    # At the equator the WGS84 distance is semimajor-axis * longitude in radians.
    assert selected["distance_km"] == pytest.approx(6378137.0 * math.radians(0.1) / 1000)
    assert selected["temperature"] == {"value": 288.15, "unit": "K"}
    assert selected["observation_time"] == "2026-09-10T13:14:59Z"
    assert selected["network"] == "METAR"
    assert selected["provider"] == "aviationweather.gov"
    assert selected["latitude"] == 0.0
    assert selected["longitude"] == 0.1
    assert selected["elevation_m"] == 100.0
    for key in (
        "raw_artifact_id",
        "raw_record_index",
        "raw_record_digest",
        "logical_observation_digest",
        "revision_digest",
        "station_snapshot_artifact_id",
    ):
        assert selected["provenance"][key] == getattr(near_row, key)
    assert selected["provenance"]["provider_available_at"] == "2026-09-10T13:15:59Z"
    assert result == _select((near, far), [near_row, far_row])


def test_distance_ties_choose_closest_time_then_station_id() -> None:
    first, second = _station("KAAA"), _station("KBBB")
    assert (
        _select((first, second), [_row(first, seconds=60), _row(second)])["selected"]["station_id"]
        == "KBBB"
    )
    rows = [_row(first, seconds=60), _row(second, seconds=-60)]
    assert _select((second, first), rows)["selected"]["station_id"] == "KAAA"


def test_same_station_symmetric_times_choose_earlier_event_deterministically() -> None:
    station = _station()
    rows = [_row(station, seconds=60), _row(station, seconds=-60)]
    result = _select((station,), rows)
    assert result["selected"]["observation_time"] == "2026-09-10T12:59:00Z"
    assert result == _select((station,), list(reversed(rows)))


@pytest.mark.parametrize("seconds", [-900, 900])
def test_time_window_includes_exact_fifteen_minute_boundaries(seconds: int) -> None:
    station = _station()
    assert _select((station,), [_row(station, seconds=seconds)])["status"] == "matched"


@pytest.mark.parametrize("seconds", [-901, 901])
def test_time_window_excludes_reports_just_outside(seconds: int) -> None:
    station = _station()
    result = _select((station,), [_row(station, seconds=seconds)])
    assert result["status"] == "unavailable"
    assert result["selected"] is None
    assert "outside_15_minute_window" in result["candidates"][0]["reasons"]


@pytest.mark.parametrize(
    "meters,expected", [(49999.0, "matched"), (50000.0, "matched"), (50001.0, "unavailable")]
)
def test_fifty_km_inclusive_boundary(meters: float, expected: str) -> None:
    # No selector/projection helper is used to calculate this test coordinate.
    station = _station(lon=math.degrees(meters / 6378137.0))
    result = _select((station,), [_row(station)])
    assert result["status"] == expected
    assert result["candidates"][0]["distance_km"] == pytest.approx(meters / 1000.0)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"temperature_k": None}, "temperature_missing"),
        ({"temperature_k": 341.0}, "temperature_out_of_range"),
        ({"temperature_k": 179.0}, "temperature_out_of_range"),
        ({"quality_flags": ("temperature_out_of_range",)}, "temperature_out_of_range"),
        ({"mesoforge_qc_state": "rejected"}, "qc_rejected"),
        ({"latitude_degrees": 0.03}, "station_metadata_conflict"),
        ({"elevation_m": 131.0}, "station_metadata_conflict"),
        ({"quality_flags": ("station_metadata_conflict",)}, "station_metadata_conflict"),
        ({"provider_available_at": _TIME - timedelta(seconds=301)}, "receipt_before_event"),
        ({"provider_station_id": "KBBB"}, "station_identity_mismatch"),
        ({"station_id": "station.unknown"}, "station_not_in_snapshot"),
        ({"station_snapshot_artifact_id": _RAW}, "station_snapshot_unavailable"),
    ],
)
def test_qc_and_missing_metadata_exclusions(overrides: dict[str, Any], reason: str) -> None:
    station = _station()
    result = _select((station,), [_row(station, **overrides)])
    assert result["status"] == "unavailable"
    assert any(reason in candidate["reasons"] for candidate in result["candidates"])
    assert result["unavailable_reason"]


@pytest.mark.parametrize("temperature", [180.0, 340.0])
def test_temperature_qc_boundaries_remain_inclusive(temperature: float) -> None:
    station = _station()
    assert _select((station,), [_row(station, temperature_k=temperature)])["status"] == "matched"


def test_partial_wind_qc_and_opaque_provider_qc_do_not_reject_valid_temperature() -> None:
    station = _station()
    row = _row(
        station,
        mesoforge_qc_state="partial",
        provider_qc_field=32.0,
        quality_flags=("wind_speed_missing",),
    )
    result = _select((station,), [row])
    assert result["status"] == "matched"
    assert result["selected"]["qc"] == {
        "state": "partial",
        "flags": ["wind_speed_missing"],
        "provider_qc_field": 32.0,
        "temperature_eligible": True,
    }


def test_rejected_correction_does_not_resurrect_older_good_temperature() -> None:
    station = _station()
    original = _row(station)
    correction = _row(
        station,
        revision="corrected",
        provider_available_at=_TIME + timedelta(minutes=2),
        ingested_at=_TIME + timedelta(minutes=3),
        temperature_k=None,
        mesoforge_qc_state="rejected",
    )
    result = _select((station,), [original, correction])
    assert result["status"] == "unavailable"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["provenance"]["revision_digest"] == correction.revision_digest
    assert result == _select((station,), [correction, original])


def test_revision_order_uses_ingestion_then_digest_after_provider_time() -> None:
    station = _station()
    original = _row(station)
    later = _row(
        station, revision="later", ingested_at=_TIME + timedelta(minutes=3), temperature_k=289.15
    )
    result = _select((station,), [later, original])
    assert result["selected"]["temperature"]["value"] == 289.15
    other = _row(station, revision="other", ingested_at=later.ingested_at, temperature_k=290.15)
    winner = max([later, other], key=lambda row: row.revision_digest)
    result = _select((station,), [other, later])
    assert result["selected"]["temperature"]["value"] == winner.temperature_k
    assert result == _select((station,), [later, other])


def test_bad_nearest_report_allows_farther_good_station() -> None:
    near, far = _station("KAAA", lon=0.1), _station("KBBB", lon=0.2)
    result = _select((near, far), [_row(near, temperature_k=None), _row(far)])
    assert result["selected"]["station_id"] == "KBBB"
    assert result["candidates"][0]["reasons"] == ["temperature_missing"]


def test_station_without_observation_and_outside_station_remain_visible() -> None:
    near, far = _station("KAAA"), _station("KBBB", lon=1.0)
    result = _select((near, far), [])
    assert result["status"] == "unavailable"
    assert result["selected"] is None
    assert result["candidates"][0]["reasons"] == ["no_retained_observation"]
    assert result["candidates"][1]["reasons"] == ["no_retained_observation", "outside_50_km"]
    assert result["candidates"][0]["temperature"] == {"value": None, "unit": "K"}
    assert _select((), [])["candidates"] == []


@pytest.mark.parametrize(
    "overrides",
    [{"latitude": 91.0}, {"longitude": float("nan")}, {"valid_time": datetime(2026, 9, 10)}],
)
def test_invalid_geography_or_naive_valid_time_is_rejected(overrides: dict[str, Any]) -> None:
    arguments: dict[str, Any] = {
        "latitude": 0.0,
        "longitude": 0.0,
        "valid_time": _TIME,
        "observations": [],
        "station_snapshots": {},
        "policy": _POLICY,
    }
    arguments.update(overrides)
    with pytest.raises(ValueError):
        select_temperature_observation(**arguments)
