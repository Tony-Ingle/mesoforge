"""Unit tests for mesoforge.catalog.stations.normalize_station_catalog
(plan Section 3.2, Task 6)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mesoforge.catalog.domains import BoundingBox, DomainDefinition
from mesoforge.catalog.stations import (
    StationCatalogError,
    StationDefinition,
    normalize_station_catalog,
)

_DOMAIN = DomainDefinition(
    domain_id="grasston-minnesota.v1",
    center_latitude=45.80265,
    center_longitude=-93.07956,
    bbox=BoundingBox(south=45.05265, north=46.55265, west=-94.07956, east=-92.07956),
    station_ids=("station.kcbg", "station.kjmr", "station.kros"),
)

_EXPECTED = (
    StationDefinition(
        station_id="station.kcbg",
        provider_icao_id="KCBG",
        expected_latitude=45.557,
        expected_longitude=-93.264,
        expected_elevation_m=285.0,
        site_name="Cambridge Muni",
        provider_site_types=("METAR",),
        provider_priority=1,
    ),
    StationDefinition(
        station_id="station.kjmr",
        provider_icao_id="KJMR",
        expected_latitude=45.88854,
        expected_longitude=-93.269,
        expected_elevation_m=301.0,
        site_name="Mora Muni",
        provider_site_types=("METAR",),
        provider_priority=1,
    ),
    StationDefinition(
        station_id="station.kros",
        provider_icao_id="KROS",
        expected_latitude=45.69624,
        expected_longitude=-92.95427,
        expected_elevation_m=282.0,
        site_name="Rush City Rgnl",
        provider_site_types=("METAR",),
        provider_priority=1,
    ),
)


def _raw_records(**overrides: dict) -> list[dict]:
    records = [
        {
            "icaoId": "KCBG",
            "lat": 45.557,
            "lon": -93.264,
            "elev": 285.0,
            "site": "Cambridge Muni",
            "siteType": {"METAR": True},
        },
        {
            "icaoId": "KJMR",
            "lat": 45.88854,
            "lon": -93.269,
            "elev": 301.0,
            "site": "Mora Muni",
            "siteType": {"METAR": True},
        },
        {
            "icaoId": "KROS",
            "lat": 45.69624,
            "lon": -92.95427,
            "elev": 282.0,
            "site": "Rush City Rgnl",
            "siteType": {"METAR": True},
        },
    ]
    for icao, patch in overrides.items():
        for record in records:
            if record["icaoId"] == icao:
                record.update(patch)
    return records


_NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


class TestNormalizeStationCatalog:
    def test_builds_snapshot_for_all_three_stations(self) -> None:
        snapshot = normalize_station_catalog(
            raw_records=_raw_records(),
            domain=_DOMAIN,
            expected_stations=_EXPECTED,
            source_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
            effective_from=_NOW,
        )
        assert len(snapshot.stations) == 3
        assert [s.station_id for s in snapshot.stations] == [
            "station.kcbg",
            "station.kjmr",
            "station.kros",
        ]

    def test_rejects_absent_expected_station(self) -> None:
        records = [r for r in _raw_records() if r["icaoId"] != "KROS"]
        with pytest.raises(StationCatalogError, match="absent"):
            normalize_station_catalog(
                raw_records=records,
                domain=_DOMAIN,
                expected_stations=_EXPECTED,
                source_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
                effective_from=_NOW,
            )

    def test_rejects_station_without_metar_capability(self) -> None:
        records = _raw_records(KROS={"siteType": {"AWOS": True}})
        with pytest.raises(StationCatalogError, match="METAR"):
            normalize_station_catalog(
                raw_records=records,
                domain=_DOMAIN,
                expected_stations=_EXPECTED,
                source_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
                effective_from=_NOW,
            )

    def test_rejects_station_outside_bbox(self) -> None:
        records = _raw_records(KROS={"lat": 10.0, "lon": -92.95427})
        with pytest.raises(StationCatalogError, match="outside"):
            normalize_station_catalog(
                raw_records=records,
                domain=_DOMAIN,
                expected_stations=_EXPECTED,
                source_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
                effective_from=_NOW,
            )

    def test_rejects_duplicate_icao_in_response(self) -> None:
        records = _raw_records()
        records.append(dict(records[0]))
        with pytest.raises(StationCatalogError, match="duplicate"):
            normalize_station_catalog(
                raw_records=records,
                domain=_DOMAIN,
                expected_stations=_EXPECTED,
                source_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
                effective_from=_NOW,
            )

    def test_rejects_malformed_coordinates(self) -> None:
        records = _raw_records(KCBG={"lat": "not-a-number"})
        with pytest.raises(StationCatalogError, match="malformed"):
            normalize_station_catalog(
                raw_records=records,
                domain=_DOMAIN,
                expected_stations=_EXPECTED,
                source_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
                effective_from=_NOW,
            )

    def test_accepts_list_shaped_site_type(self) -> None:
        records = _raw_records(KCBG={"siteType": ["METAR", "TAF"]})
        snapshot = normalize_station_catalog(
            raw_records=records,
            domain=_DOMAIN,
            expected_stations=_EXPECTED,
            source_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
            effective_from=_NOW,
        )
        kcbg = next(s for s in snapshot.stations if s.station_id == "station.kcbg")
        assert "METAR" in kcbg.site_types

    def test_snapshot_is_immutable(self) -> None:
        snapshot = normalize_station_catalog(
            raw_records=_raw_records(),
            domain=_DOMAIN,
            expected_stations=_EXPECTED,
            source_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
            effective_from=_NOW,
        )
        with pytest.raises(Exception):  # noqa: B017 -- pydantic frozen-model error type
            snapshot.stations = ()  # type: ignore[misc]
