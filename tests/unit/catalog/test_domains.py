"""Unit tests for mesoforge.catalog.domains (Phase 1 plan Section 3.2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mesoforge.catalog.domains import BoundingBox, DomainDefinition


def _bbox(**overrides: float) -> BoundingBox:
    values = {"south": 45.05265, "north": 46.55265, "west": -94.07956, "east": -92.07956}
    values.update(overrides)
    return BoundingBox(**values)


class TestBoundingBox:
    def test_accepts_grasston_bbox(self) -> None:
        bbox = _bbox()
        assert bbox.center_latitude == pytest.approx(45.80265)
        assert bbox.center_longitude == pytest.approx(-93.07956)

    def test_rejects_south_greater_than_north(self) -> None:
        with pytest.raises(ValidationError):
            _bbox(south=47.0, north=46.0)

    def test_rejects_west_greater_than_east(self) -> None:
        with pytest.raises(ValidationError):
            _bbox(west=-90.0, east=-95.0)

    def test_rejects_out_of_range_latitude(self) -> None:
        with pytest.raises(ValidationError):
            _bbox(south=-95.0)

    def test_rejects_out_of_range_longitude(self) -> None:
        with pytest.raises(ValidationError):
            _bbox(west=-185.0)

    def test_contains_point_inside(self) -> None:
        bbox = _bbox()
        assert bbox.contains(latitude=45.8, longitude=-93.1)

    def test_does_not_contain_point_outside(self) -> None:
        bbox = _bbox()
        assert not bbox.contains(latitude=50.0, longitude=-93.1)


class TestDomainDefinition:
    def test_accepts_grasston_domain(self) -> None:
        domain = DomainDefinition(
            domain_id="grasston-minnesota.v1",
            center_latitude=45.80265,
            center_longitude=-93.07956,
            bbox=_bbox(),
            station_ids=("station.kcbg", "station.kjmr", "station.kros"),
        )
        assert domain.domain_id == "grasston-minnesota.v1"

    def test_rejects_center_not_bbox_midpoint(self) -> None:
        with pytest.raises(ValidationError, match="midpoint"):
            DomainDefinition(
                domain_id="grasston-minnesota.v1",
                center_latitude=45.0,
                center_longitude=-93.07956,
                bbox=_bbox(),
                station_ids=("station.kcbg", "station.kjmr", "station.kros"),
            )

    def test_rejects_empty_station_ids(self) -> None:
        with pytest.raises(ValidationError):
            DomainDefinition(
                domain_id="grasston-minnesota.v1",
                center_latitude=45.80265,
                center_longitude=-93.07956,
                bbox=_bbox(),
                station_ids=(),
            )

    def test_rejects_duplicate_station_ids(self) -> None:
        with pytest.raises(ValidationError, match="duplicates"):
            DomainDefinition(
                domain_id="grasston-minnesota.v1",
                center_latitude=45.80265,
                center_longitude=-93.07956,
                bbox=_bbox(),
                station_ids=("station.kcbg", "station.kcbg"),
            )

    def test_rejects_non_lexicographic_station_order(self) -> None:
        with pytest.raises(ValidationError, match="lexicographic"):
            DomainDefinition(
                domain_id="grasston-minnesota.v1",
                center_latitude=45.80265,
                center_longitude=-93.07956,
                bbox=_bbox(),
                station_ids=("station.kros", "station.kcbg", "station.kjmr"),
            )

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            DomainDefinition(
                domain_id="grasston-minnesota.v1",
                center_latitude=45.80265,
                center_longitude=-93.07956,
                bbox=_bbox(),
                station_ids=("station.kcbg",),
                unknown_field=True,  # type: ignore[call-arg]
            )
