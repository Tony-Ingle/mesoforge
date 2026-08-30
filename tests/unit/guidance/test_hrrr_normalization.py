"""Unit tests for mesoforge.guidance.normalization (plan Section 3.5,
Task 5): projection, grid-to-earth wind rotation, and bbox+halo
subsetting."""

from __future__ import annotations

import numpy as np
import pytest

from mesoforge.catalog.domains import BoundingBox
from mesoforge.guidance.normalization import (
    SubsettingError,
    WindRotationError,
    assemble_canonical_hrrr_dataset,
    build_lambert_conformal_crs,
    compute_bbox_halo_subset_indices,
    compute_latlon_grid,
    compute_projected_coordinates,
    rotate_wind_to_earth_relative,
)

pytestmark = pytest.mark.scientific

_LOV = 262.5
_LAD = 38.5
_LATIN1 = 38.5
_LATIN2 = 38.5


def _crs():
    return build_lambert_conformal_crs(
        lov_degrees=_LOV, lad_degrees=_LAD, latin1_degrees=_LATIN1, latin2_degrees=_LATIN2
    )


class TestBuildLambertConformalCrs:
    def test_builds_valid_crs(self) -> None:
        crs = _crs()
        assert crs.is_projected

    def test_handles_longitude_over_180(self) -> None:
        # 262.5 should be normalized to -97.5 internally
        crs = _crs()
        params = {p.name: p.value for p in crs.coordinate_operation.params}
        assert params.get("Longitude of false origin") == pytest.approx(-97.5)


class TestComputeProjectedCoordinates:
    def test_produces_strictly_increasing_coordinates(self) -> None:
        crs = _crs()
        x, y = compute_projected_coordinates(
            crs,
            first_lat_degrees=44.5,
            first_lon_degrees=267.5,
            dx_m=3000.0,
            dy_m=3000.0,
            nx=6,
            ny=6,
        )
        assert np.all(np.diff(x) > 0)
        assert np.all(np.diff(y) > 0)
        assert len(x) == 6
        assert len(y) == 6


class TestComputeLatlonGrid:
    def test_round_trips_first_grid_point(self) -> None:
        crs = _crs()
        x, y = compute_projected_coordinates(
            crs,
            first_lat_degrees=44.5,
            first_lon_degrees=267.5,
            dx_m=3000.0,
            dy_m=3000.0,
            nx=6,
            ny=6,
        )
        lat, lon = compute_latlon_grid(crs, x=x, y=y)
        assert lat[0, 0] == pytest.approx(44.5, abs=1e-6)
        assert lon[0, 0] == pytest.approx(267.5 - 360.0, abs=1e-6)

    def test_longitude_in_minus_180_to_180_convention(self) -> None:
        crs = _crs()
        x, y = compute_projected_coordinates(
            crs,
            first_lat_degrees=44.5,
            first_lon_degrees=267.5,
            dx_m=3000.0,
            dy_m=3000.0,
            nx=6,
            ny=6,
        )
        _lat, lon = compute_latlon_grid(crs, x=x, y=y)
        assert np.all(lon >= -180.0) and np.all(lon <= 180.0)


class TestRotateWindToEarthRelative:
    def test_identity_when_already_earth_relative(self) -> None:
        u = np.array([[1.0, 2.0], [3.0, 4.0]])
        v = np.array([[5.0, 6.0], [7.0, 8.0]])
        crs = _crs()
        result = rotate_wind_to_earth_relative(
            u_grid=u,
            v_grid=v,
            x=np.array([0.0, 3000.0]),
            y=np.array([0.0, 3000.0]),
            crs=crs,
            u_relative_to_grid=False,
            v_relative_to_grid=False,
        )
        assert result.policy == "identity"
        np.testing.assert_array_equal(result.eastward, u)
        np.testing.assert_array_equal(result.northward, v)

    def test_rejects_disagreeing_uv_relative_to_grid(self) -> None:
        crs = _crs()
        with pytest.raises(WindRotationError, match="disagree"):
            rotate_wind_to_earth_relative(
                u_grid=np.zeros((2, 2)),
                v_grid=np.zeros((2, 2)),
                x=np.array([0.0, 3000.0]),
                y=np.array([0.0, 3000.0]),
                crs=crs,
                u_relative_to_grid=True,
                v_relative_to_grid=False,
            )

    def test_rotation_preserves_speed_at_grid_origin(self) -> None:
        """Property: |rotated vector| == |original vector| (rotation is
        norm-preserving) -- checked at the projection's true origin
        where azimuth is exactly true north/east and the rotation is
        exactly identity, avoiding floating-point sensitivity far from
        origin in this minimal test grid."""
        crs = _crs()
        # At lon_0/lat_0 (the LCC true origin), grid x/y axes coincide
        # exactly with east/north, so grid-relative == earth-relative.
        x = np.array([-3000.0, 0.0, 3000.0])
        y = np.array([-3000.0, 0.0, 3000.0])
        u_grid = np.full((3, 3), 5.0)
        v_grid = np.full((3, 3), 0.0)
        result = rotate_wind_to_earth_relative(
            u_grid=u_grid,
            v_grid=v_grid,
            x=x,
            y=y,
            crs=crs,
            u_relative_to_grid=True,
            v_relative_to_grid=True,
        )
        speed_before = np.hypot(u_grid, v_grid)
        speed_after = np.hypot(result.eastward, result.northward)
        np.testing.assert_allclose(speed_after, speed_before, atol=1e-6)
        assert result.policy == "grid-to-earth-pyproj.v1"

    def test_rotation_at_true_origin_is_near_identity(self) -> None:
        """At the LCC true origin (lon_0, lat_0), the grid x/y axes are
        aligned with east/north, so grid-relative and earth-relative
        wind components should be numerically close."""
        crs = _crs()
        x = np.array([-3000.0, 0.0, 3000.0])
        y = np.array([-3000.0, 0.0, 3000.0])
        u_grid = np.full((3, 3), 5.0)
        v_grid = np.full((3, 3), 2.0)
        result = rotate_wind_to_earth_relative(
            u_grid=u_grid,
            v_grid=v_grid,
            x=x,
            y=y,
            crs=crs,
            u_relative_to_grid=True,
            v_relative_to_grid=True,
        )
        # Center point (index 1,1) sits exactly at the true origin.
        assert result.eastward[1, 1] == pytest.approx(5.0, abs=1e-3)
        assert result.northward[1, 1] == pytest.approx(2.0, abs=1e-3)


class TestComputeBboxHaloSubsetIndices:
    def _grid(self):
        crs = _crs()
        x, y = compute_projected_coordinates(
            crs,
            first_lat_degrees=44.5,
            first_lon_degrees=267.5,
            dx_m=3000.0,
            dy_m=3000.0,
            nx=6,
            ny=6,
        )
        lat, lon = compute_latlon_grid(crs, x=x, y=y)
        return x, y, lat, lon

    def test_finds_subset_with_halo(self) -> None:
        x, y, lat, lon = self._grid()
        bbox = BoundingBox(
            south=float(lat[2, 2]) - 0.001,
            north=float(lat[3, 3]) + 0.001,
            west=float(lon[2, 2]) - 0.001,
            east=float(lon[3, 3]) + 0.001,
        )
        indices = compute_bbox_halo_subset_indices(x=x, y=y, lat=lat, lon=lon, bbox=bbox)
        assert indices.y_start <= 2
        assert indices.y_end >= 4
        assert indices.x_start <= 2
        assert indices.x_end >= 4

    def test_rejects_halo_extending_outside_grid(self) -> None:
        x, y, lat, lon = self._grid()
        bbox = BoundingBox(
            south=float(lat[0, 0]) - 0.001,
            north=float(lat[-1, -1]) + 0.001,
            west=float(lon[0, 0]) - 0.001,
            east=float(lon[-1, -1]) + 0.001,
        )
        with pytest.raises(SubsettingError, match="extends outside"):
            compute_bbox_halo_subset_indices(x=x, y=y, lat=lat, lon=lon, bbox=bbox)

    def test_rejects_bbox_outside_grid(self) -> None:
        x, y, lat, lon = self._grid()
        bbox = BoundingBox(south=1.0, north=2.0, west=1.0, east=2.0)
        with pytest.raises(SubsettingError, match="does not intersect"):
            compute_bbox_halo_subset_indices(x=x, y=y, lat=lat, lon=lon, bbox=bbox)


class TestAssembleCanonicalHrrrDataset:
    def test_builds_expected_dataset_shape_and_attrs(self) -> None:
        x = np.array([0.0, 3000.0, 6000.0])
        y = np.array([0.0, 3000.0, 6000.0])
        lat = np.full((3, 3), 44.5)
        lon = np.full((3, 3), -93.0)
        dataset = assemble_canonical_hrrr_dataset(
            forecast_reference_time=np.datetime64("2026-08-28T18:00:00", "ns"),
            lead_hours=(0, 1),
            x=x,
            y=y,
            lat=lat,
            lon=lon,
            temperature_k=np.full((2, 3, 3), 280.0),
            eastward_wind_m_s=np.full((2, 3, 3), 1.0),
            northward_wind_m_s=np.full((2, 3, 3), 2.0),
            grid_id="hrrr-conus-grasston-subset.v1",
            configuration_snapshot_id="cfg_sha256_" + "a" * 64,
            variable_lineage_manifest_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
        )
        assert dataset.attrs["schema_version"] == "canonical-guidance.v1"
        assert dataset.attrs["time_encoding"] == "UTC"
        assert dataset["air_temperature_2m"].dims == ("lead_time", "y", "x")
        assert dataset.sizes["lead_time"] == 2
        expected_valid = np.datetime64("2026-08-28T18:00:00", "ns") + np.array(
            [np.timedelta64(0, "h"), np.timedelta64(1, "h")]
        )
        np.testing.assert_array_equal(dataset["valid_time"].values, expected_valid)
