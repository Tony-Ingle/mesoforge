"""Focused geographic envelopes, region sharing, and native-grid coverage checks."""

from __future__ import annotations

from itertools import permutations

import numpy as np
import pyproj
import pytest

from mesoforge.application.spatial_coverage import (
    CONTEXT_KM,
    MODEL_BUFFER_KM,
    UnsupportedCoordinateError,
    bbox_in_grid,
    bbox_within_prepared_domain,
    footprint,
    native_bbox_bounds,
    plan_regions,
    point_in_grid,
    validate_coordinate,
)
from mesoforge.catalog.domains import BoundingBox

LOCATIONS = [(45.8, -93.1), (44.98, -93.27), (45.9, -93.0)]


@pytest.mark.parametrize("latitude,longitude", [*LOCATIONS, (0.0, 0.0), (90.0, 180.0)])
def test_valid_coordinates_are_not_limited_to_a_previous_prepared_subset(
    latitude: float, longitude: float
) -> None:
    validate_coordinate(latitude, longitude)


@pytest.mark.parametrize(
    "latitude,longitude",
    [(91.0, 0.0), (0.0, -181.0), (float("nan"), 0.0), (0.0, float("inf")), (True, 0.0), ("45", 0)],
)
def test_invalid_geographic_inputs_are_rejected(latitude: float, longitude: float) -> None:
    with pytest.raises(UnsupportedCoordinateError, match="finite numbers"):
        validate_coordinate(latitude, longitude)


@pytest.mark.parametrize("radius", [MODEL_BUFFER_KM, CONTEXT_KM])
@pytest.mark.parametrize(
    "latitude,longitude", [*LOCATIONS, (0.0, 0.0), (88.9, 20.0), (20.0, 179.9)]
)
def test_footprint_contains_independently_calculated_wgs84_distance_circle(
    latitude: float, longitude: float, radius: float
) -> None:
    box = footprint(latitude, longitude, radius)
    geod = pyproj.Geod(ellps="WGS84")
    for bearing in np.linspace(0, 360, 721):
        lon, lat, _ = geod.fwd(longitude, latitude, bearing, radius * 1000)
        assert box.contains(latitude=lat, longitude=lon)
    assert box.contains(latitude=latitude, longitude=longitude)


@pytest.mark.parametrize("latitude,longitude", [(89.9, 0.0), (0.0, 179.9), (-89.9, -179.9)])
def test_pole_or_dateline_uses_a_conservative_full_longitude_envelope(
    latitude: float, longitude: float
) -> None:
    box = footprint(latitude, longitude, CONTEXT_KM)
    assert (box.west, box.east) == (-180.0, 180.0)


def test_all_requested_nearby_locations_share_one_context_preparation() -> None:
    expected = plan_regions(LOCATIONS)
    assert len(expected) == 1
    for order in permutations(LOCATIONS):
        assert plan_regions(list(order)) == expected
    for latitude, longitude in LOCATIONS:
        context = footprint(latitude, longitude, CONTEXT_KM)
        minimum = footprint(latitude, longitude, MODEL_BUFFER_KM)
        assert expected[0].south <= context.south < minimum.south
        assert expected[0].north >= context.north > minimum.north
        assert expected[0].west <= context.west < minimum.west
        assert expected[0].east >= context.east > minimum.east
    assert plan_regions(LOCATIONS + LOCATIONS) == expected


def test_distant_locations_remain_separate_and_empty_collection_needs_no_region() -> None:
    assert len(plan_regions([*LOCATIONS, (40.0, -105.0)])) == 2
    assert plan_regions([]) == []


def test_touching_chain_of_contexts_merges_transitively() -> None:
    points = [(45.0, -93.0), (47.0, -93.0), (49.0, -93.0)]
    assert len(plan_regions(points)) == 1


def test_native_geographic_coverage_preserves_descending_latitudes_and_360_longitudes() -> None:
    crs = pyproj.CRS.from_epsg(4326)
    x = np.array([266.0, 267.0, 268.0])
    y = np.array([46.0, 45.0, 44.0])
    assert point_in_grid(44.98, -93.27, crs, x, y)
    assert point_in_grid(44.0, -94.0, crs, x, y)
    assert not point_in_grid(43.99, -93.27, crs, x, y)
    assert bbox_in_grid(BoundingBox(south=44.5, north=45.5, west=-93.5, east=-92.5), crs, x, y)
    assert not bbox_in_grid(BoundingBox(south=43.5, north=45.5, west=-93.5, east=-92.5), crs, x, y)


def test_projected_coverage_checks_curved_boundary_between_geographic_corners() -> None:
    crs = pyproj.CRS.from_proj4(
        "+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5 +R=6371229 +units=m +no_defs"
    )
    transform = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    box = BoundingBox(south=40.0, north=46.0, west=-110.0, east=-85.0)
    west_x, corner_y = transform.transform(box.west, box.south)
    east_x, _ = transform.transform(box.east, box.south)
    _, top_y = transform.transform(box.west, box.north)
    _, interior_y = transform.transform(-97.5, box.south)
    assert interior_y < corner_y
    x = np.array([west_x, east_x])
    y = np.array([(interior_y + corner_y) / 2, top_y])
    assert all(
        point_in_grid(lat, lon, crs, x, y) for lat in (40.0, 46.0) for lon in (-110.0, -85.0)
    )
    assert not bbox_in_grid(box, crs, x, y)
    assert bbox_in_grid(box, crs, x, np.array([interior_y, top_y]))


def test_degenerate_or_nonmonotonic_grid_does_not_claim_coverage() -> None:
    crs = pyproj.CRS.from_epsg(4326)
    for x in (np.array([0.0]), np.array([0.0, 0.0]), np.array([0.0, 2.0, 1.0])):
        assert not point_in_grid(0.5, 0.5, crs, x, np.array([0.0, 1.0]))


def test_clipped_domain_coverage_requires_actual_retained_axes() -> None:
    crs = pyproj.CRS.from_epsg(4326)
    source_x, source_y = np.array([-100.0, -90.0]), np.array([40.0, 50.0])
    box = BoundingBox(south=42.0, north=45.0, west=-105.0, east=-95.0)
    retained_x, retained_y = np.array([-100.0, -95.0]), np.array([42.0, 45.0])
    assert not bbox_in_grid(box, crs, retained_x, retained_y)
    assert bbox_within_prepared_domain(box, crs, retained_x, retained_y, source_x, source_y)
    assert not bbox_within_prepared_domain(
        box, crs, np.array([-99.0, -95.0]), retained_y, source_x, source_y
    )
    assert not bbox_within_prepared_domain(
        box, crs, retained_x, np.array([42.1, 45.0]), source_x, source_y
    )
    assert not bbox_within_prepared_domain(
        box, crs, np.array([-101.0, -95.0]), retained_y, source_x, source_y
    )


def test_source_clipping_does_not_claim_that_an_unsupported_point_is_supported() -> None:
    crs = pyproj.CRS.from_epsg(4326)
    x, y = np.array([-100.0, -90.0]), np.array([40.0, 50.0])
    box = footprint(0.0, 0.0, CONTEXT_KM)
    assert bbox_within_prepared_domain(box, crs, x, y, x, y)
    assert not point_in_grid(0.0, 0.0, crs, x, y)


def test_native_bounds_include_lambert_interior_edge_extremum() -> None:
    crs = pyproj.CRS.from_proj4(
        "+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5 +R=6371229 +units=m +no_defs"
    )
    transform = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    box = BoundingBox(south=40.0, north=46.0, west=-110.0, east=-85.0)
    xmin, xmax, ymin, ymax = native_bbox_bounds(box, crs)
    _, true_ymin = transform.transform(-97.5, box.south)
    assert ymin == pytest.approx(true_ymin, abs=1e-8)
    assert bbox_in_grid(box, crs, np.array([xmin, xmax]), np.array([ymin, ymax]))
    assert not bbox_within_prepared_domain(
        box,
        crs,
        np.array([xmin, xmax]),
        np.array([ymin + 1, ymax]),
        np.array([xmin - 10000, xmax + 10000]),
        np.array([ymin - 10000, ymax + 10000]),
    )
