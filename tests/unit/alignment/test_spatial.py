"""Unit tests for mesoforge.alignment.spatial (plan Section 3.6, Task
6): bilinear native-grid interpolation, no extrapolation, weight-sum
invariant, monotonic-coordinate-safe search."""

from __future__ import annotations

import numpy as np
import pytest

from mesoforge.alignment.spatial import (
    PointExtractionError,
    bilinear_interpolate,
    compute_bilinear_weights,
    find_enclosing_cell,
    project_station_point,
)
from mesoforge.guidance.normalization import build_lambert_conformal_crs

pytestmark = pytest.mark.scientific

_X_ASC = np.array([0.0, 1.0, 2.0, 3.0])
_Y_ASC = np.array([10.0, 11.0, 12.0, 13.0])
_X_DESC = _X_ASC[::-1].copy()
_Y_DESC = _Y_ASC[::-1].copy()


class TestFindEnclosingCell:
    def test_finds_cell_ascending_axes(self) -> None:
        cell = find_enclosing_cell(x=_X_ASC, y=_Y_ASC, station_x=1.5, station_y=11.5)
        assert cell.x0 == 1 and cell.x1 == 2
        assert cell.y0 == 1 and cell.y1 == 2

    def test_finds_cell_descending_axes(self) -> None:
        cell = find_enclosing_cell(x=_X_DESC, y=_Y_DESC, station_x=1.5, station_y=11.5)
        assert (
            min(_X_DESC[cell.x0], _X_DESC[cell.x1])
            <= 1.5
            <= max(_X_DESC[cell.x0], _X_DESC[cell.x1])
        )
        assert (
            min(_Y_DESC[cell.y0], _Y_DESC[cell.y1])
            <= 11.5
            <= max(_Y_DESC[cell.y0], _Y_DESC[cell.y1])
        )

    def test_station_exactly_at_grid_point(self) -> None:
        cell = find_enclosing_cell(x=_X_ASC, y=_Y_ASC, station_x=1.0, station_y=11.0)
        assert cell.x0 <= 1 <= cell.x1
        assert cell.y0 <= 1 <= cell.y1

    def test_station_on_outer_edge_still_has_enclosing_cell(self) -> None:
        cell = find_enclosing_cell(x=_X_ASC, y=_Y_ASC, station_x=0.0, station_y=10.0)
        assert cell.x0 == 0 and cell.x1 == 1
        assert cell.y0 == 0 and cell.y1 == 1

    def test_station_beyond_outer_x_edge_rejected(self) -> None:
        with pytest.raises(PointExtractionError, match="outside"):
            find_enclosing_cell(x=_X_ASC, y=_Y_ASC, station_x=5.0, station_y=11.0)

    def test_station_beyond_outer_y_edge_rejected(self) -> None:
        with pytest.raises(PointExtractionError, match="outside"):
            find_enclosing_cell(x=_X_ASC, y=_Y_ASC, station_x=1.0, station_y=20.0)

    def test_station_below_lower_x_edge_rejected(self) -> None:
        with pytest.raises(PointExtractionError, match="outside"):
            find_enclosing_cell(x=_X_ASC, y=_Y_ASC, station_x=-1.0, station_y=11.0)


class TestComputeBilinearWeights:
    def test_weights_sum_to_one(self) -> None:
        cell = find_enclosing_cell(x=_X_ASC, y=_Y_ASC, station_x=1.3, station_y=11.7)
        weights = compute_bilinear_weights(
            x=_X_ASC, y=_Y_ASC, cell=cell, station_x=1.3, station_y=11.7
        )
        total = weights.w00 + weights.w01 + weights.w10 + weights.w11
        assert total == pytest.approx(1.0, abs=1e-12)

    def test_exact_corner_gives_weight_one(self) -> None:
        cell = find_enclosing_cell(x=_X_ASC, y=_Y_ASC, station_x=1.0, station_y=11.0)
        weights = compute_bilinear_weights(
            x=_X_ASC, y=_Y_ASC, cell=cell, station_x=1.0, station_y=11.0
        )
        assert weights.w00 == pytest.approx(1.0)
        assert weights.w01 == pytest.approx(0.0, abs=1e-12)
        assert weights.w10 == pytest.approx(0.0, abs=1e-12)
        assert weights.w11 == pytest.approx(0.0, abs=1e-12)

    def test_exact_center_gives_equal_weights(self) -> None:
        cell = find_enclosing_cell(x=_X_ASC, y=_Y_ASC, station_x=1.5, station_y=11.5)
        weights = compute_bilinear_weights(
            x=_X_ASC, y=_Y_ASC, cell=cell, station_x=1.5, station_y=11.5
        )
        assert weights.w00 == pytest.approx(0.25)
        assert weights.w01 == pytest.approx(0.25)
        assert weights.w10 == pytest.approx(0.25)
        assert weights.w11 == pytest.approx(0.25)


class TestBilinearInterpolate:
    def test_exact_corner_value(self) -> None:
        field = np.array(
            [
                [1.0, 2.0, 3.0, 4.0],
                [5.0, 6.0, 7.0, 8.0],
                [9.0, 10.0, 11.0, 12.0],
                [13.0, 14.0, 15.0, 16.0],
            ]
        )
        result = bilinear_interpolate(
            field=field, x=_X_ASC, y=_Y_ASC, station_x=0.0, station_y=10.0
        )
        assert result.value == pytest.approx(1.0)

    def test_exact_center_value_is_bilinear_average(self) -> None:
        field = np.array(
            [
                [0.0, 0.0, 0.0, 0.0],
                [0.0, 10.0, 20.0, 0.0],
                [0.0, 20.0, 30.0, 0.0],
                [0.0, 0.0, 0.0, 0.0],
            ]
        )
        result = bilinear_interpolate(
            field=field, x=_X_ASC, y=_Y_ASC, station_x=1.5, station_y=11.5
        )
        expected = (10.0 + 20.0 + 20.0 + 30.0) / 4.0
        assert result.value == pytest.approx(expected)

    def test_planar_field_is_linear_interpolation_exact(self) -> None:
        """Property: bilinear interpolation of an exactly-planar field
        (f(x,y) = a*x + b*y + c) reproduces the plane exactly, since
        bilinear interpolation is exact for linear/planar fields."""
        xx, yy = np.meshgrid(_X_ASC, _Y_ASC)
        field = 2.0 * xx + 3.0 * yy + 5.0
        result = bilinear_interpolate(
            field=field, x=_X_ASC, y=_Y_ASC, station_x=1.3, station_y=11.7
        )
        expected = 2.0 * 1.3 + 3.0 * 11.7 + 5.0
        assert result.value == pytest.approx(expected, abs=1e-9)

    def test_rejects_nonfinite_corner(self) -> None:
        field = np.array(
            [
                [1.0, 2.0, 3.0, 4.0],
                [5.0, np.nan, 7.0, 8.0],
                [9.0, 10.0, 11.0, 12.0],
                [13.0, 14.0, 15.0, 16.0],
            ]
        )
        with pytest.raises(PointExtractionError, match="non-finite"):
            bilinear_interpolate(field=field, x=_X_ASC, y=_Y_ASC, station_x=1.5, station_y=11.5)

    def test_rejects_out_of_domain_point(self) -> None:
        field = np.zeros((4, 4))
        with pytest.raises(PointExtractionError, match="outside"):
            bilinear_interpolate(field=field, x=_X_ASC, y=_Y_ASC, station_x=100.0, station_y=11.5)

    def test_descending_axes_produce_same_result_as_ascending(self) -> None:
        xx, yy = np.meshgrid(_X_ASC, _Y_ASC)
        field_asc = 2.0 * xx + 3.0 * yy + 5.0
        field_desc = field_asc[::-1, ::-1]
        result_asc = bilinear_interpolate(
            field=field_asc, x=_X_ASC, y=_Y_ASC, station_x=1.3, station_y=11.7
        )
        result_desc = bilinear_interpolate(
            field=field_desc, x=_X_DESC, y=_Y_DESC, station_x=1.3, station_y=11.7
        )
        assert result_asc.value == pytest.approx(result_desc.value, abs=1e-9)


class TestBboxEdgeStationsWithHalo:
    """Section 3.6: a station on a bbox edge still requires an
    enclosing four-corner source cell inside the halo; a station on
    the outer retained-grid edge fails with outside_interpolation_coverage."""

    def test_station_on_inner_bbox_edge_with_halo_succeeds(self) -> None:
        # Grid has one extra halo cell on each side beyond the bbox
        # edge stations sit on.
        x = np.array([0.0, 1.0, 2.0, 3.0, 4.0])  # halo at 0 and 4
        y = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        field = np.ones((5, 5)) * 42.0
        # station sits exactly at the inner bbox edge (x=1), which is
        # still one cell inside the halo -- succeeds.
        result = bilinear_interpolate(field=field, x=x, y=y, station_x=1.0, station_y=1.0)
        assert result.value == pytest.approx(42.0)

    def test_station_on_outer_grid_edge_fails(self) -> None:
        x = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        y = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        field = np.ones((5, 5)) * 42.0
        # station at x=4.5 is beyond the outer retained-grid edge
        with pytest.raises(PointExtractionError, match="outside"):
            bilinear_interpolate(field=field, x=x, y=y, station_x=4.5, station_y=1.0)


class TestProjectStationPoint:
    def test_projects_grasston_center(self) -> None:
        crs = build_lambert_conformal_crs(
            lov_degrees=262.5, lad_degrees=38.5, latin1_degrees=38.5, latin2_degrees=38.5
        )
        x, y = project_station_point(crs, latitude=45.80265, longitude=-93.07956)
        assert np.isfinite(x)
        assert np.isfinite(y)

    def test_handles_longitude_over_180(self) -> None:
        crs = build_lambert_conformal_crs(
            lov_degrees=262.5, lad_degrees=38.5, latin1_degrees=38.5, latin2_degrees=38.5
        )
        x1, y1 = project_station_point(crs, latitude=45.8, longitude=-93.1)
        x2, y2 = project_station_point(crs, latitude=45.8, longitude=266.9)
        assert x1 == pytest.approx(x2, abs=1e-6)
        assert y1 == pytest.approx(y2, abs=1e-6)
