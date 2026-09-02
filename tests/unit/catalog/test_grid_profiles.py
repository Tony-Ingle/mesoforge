"""Approved source-grid profile registry tests (Codex re-review
finding 2).

The operational NBM CONUS grid is part of the scientific contract, so
these tests pin it two ways: the shipped production configuration must
declare exactly the approved operational profile, and the registry must
refuse any mutation of an approved profile's projection, shape,
increments, scan order, or coverage.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyproj
import pytest

from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.catalog.grid_profiles import (
    APPROVED_NBM_GRID_PROFILES,
    NBM_CONUS_FIXTURE_GRID_PROFILE,
    NBM_CONUS_OPERATIONAL_GRID_PROFILE,
    NbmGridProfile,
    require_approved_nbm_grid_profile,
)
from mesoforge.guidance.nbm_geometry import compute_last_grid_point, compute_nbm_grid

ROOT = Path(__file__).resolve().parents[3]


def _phase2_configuration():
    configuration, _ = load_configuration_source(
        base_path=ROOT / "configs/base.yaml",
        environment_path=ROOT / "configs/phase1-grasston.yaml",
        additional_overlay_paths=(ROOT / "configs/phase2-grasston.yaml",),
    )
    assert configuration.phase2 is not None
    return configuration.phase2


class TestOperationalProfileIsShipped:
    def test_production_configuration_declares_the_operational_profile(self) -> None:
        """The shipped configuration must pin the real operational grid.

        A fixture-scale grid is an approved profile too, so this is the
        assertion that keeps the acceptance overlay from silently
        weakening what production actually runs against.
        """
        profile = _phase2_configuration().nbm.grid_profile
        assert profile == NBM_CONUS_OPERATIONAL_GRID_PROFILE
        assert profile.profile_id == "nbm-core-conus-operational.v1"
        assert profile.nx == 2345
        assert profile.ny == 1597
        assert profile.shape == (1597, 2345)
        assert profile.dx_metres == pytest.approx(2539.703)
        assert profile.dy_metres == pytest.approx(2539.703)
        assert profile.grid_type == "lambert"
        assert profile.lov_degrees == pytest.approx(265.0)
        assert profile.lad_degrees == pytest.approx(25.0)
        assert profile.latin1_degrees == pytest.approx(25.0)
        assert profile.latin2_degrees == pytest.approx(25.0)
        assert profile.earth_radius_metres == pytest.approx(6371200.0)
        assert (profile.i_scans_negatively, profile.j_scans_positively) == (0, 1)
        assert profile.j_points_are_consecutive == 0

    def test_configuration_contract_profile_matches_the_grid_profile(self) -> None:
        nbm = _phase2_configuration().nbm
        assert nbm.contract_profile == nbm.grid_profile.profile_id


class TestApprovedProfileRegistry:
    def test_approved_profiles_round_trip(self) -> None:
        for profile in APPROVED_NBM_GRID_PROFILES.values():
            assert require_approved_nbm_grid_profile(profile) is profile

    def test_rejects_an_unregistered_profile_id(self) -> None:
        unknown = NBM_CONUS_FIXTURE_GRID_PROFILE.model_copy(
            update={"profile_id": "nbm-invented.v1"}
        )
        with pytest.raises(ValueError, match="is not an approved profile"):
            require_approved_nbm_grid_profile(unknown)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("nx", 2344),
            ("ny", 1596),
            ("dx_metres", 3000.0),
            ("dy_metres", 3000.0),
            ("lov_degrees", 262.5),
            ("lad_degrees", 38.5),
            ("latin1_degrees", 38.5),
            ("latin2_degrees", 38.5),
            ("earth_radius_metres", 6371229.0),
            ("first_latitude_degrees", 20.0),
            ("first_longitude_degrees", 234.0),
            ("last_latitude_degrees", 55.0),
            ("last_longitude_degrees", 301.5),
            ("i_scans_negatively", 1),
            ("j_scans_positively", 0),
            ("j_points_are_consecutive", 1),
        ],
    )
    def test_rejects_every_single_field_mutation(self, field: str, value: object) -> None:
        """Each clause of the approved grid is independently pinned: no
        single-field mutation may slip through the registry."""
        mutated = NBM_CONUS_OPERATIONAL_GRID_PROFILE.model_copy(update={field: value})
        with pytest.raises(ValueError, match="does not match the approved profile"):
            require_approved_nbm_grid_profile(mutated)

    def test_rejects_structurally_impossible_profiles(self) -> None:
        for update in ({"nx": 0}, {"dx_metres": 0.0}, {"j_scans_positively": 2}):
            with pytest.raises(ValueError):
                NbmGridProfile(**{**NBM_CONUS_OPERATIONAL_GRID_PROFILE.model_dump(), **update})


class TestProjectedGeometry:
    def test_coverage_matches_the_pinned_last_point(self) -> None:
        """The pinned last point is exactly what the profile's own
        projection, shape, and increments produce -- so the coverage
        clause is a real cross-check, not a restatement."""
        for profile in APPROVED_NBM_GRID_PROFILES.values():
            last_lat, last_lon = compute_last_grid_point(
                profile,
                nx=profile.nx,
                ny=profile.ny,
                dx_metres=profile.dx_metres,
                dy_metres=profile.dy_metres,
                first_latitude_degrees=profile.first_latitude_degrees,
                first_longitude_degrees=profile.first_longitude_degrees,
            )
            assert last_lat == pytest.approx(profile.last_latitude_degrees, abs=1e-6)
            assert last_lon == pytest.approx(profile.last_longitude_degrees, abs=1e-6)

    def test_a_wrong_projection_moves_the_far_corner(self) -> None:
        profile = NBM_CONUS_FIXTURE_GRID_PROFILE
        shifted = profile.model_copy(update={"lov_degrees": 255.0})
        _lat, lon = compute_last_grid_point(
            shifted,
            nx=profile.nx,
            ny=profile.ny,
            dx_metres=profile.dx_metres,
            dy_metres=profile.dy_metres,
            first_latitude_degrees=profile.first_latitude_degrees,
            first_longitude_degrees=profile.first_longitude_degrees,
        )
        assert abs(lon - profile.last_longitude_degrees) > profile.coordinate_tolerance_degrees

    def test_grid_is_projected_metres_not_degrees(self) -> None:
        """Regression probe for the finding itself: normalization must
        build a projected axis, never a geographic mesh whose 'x' is
        just a longitude ramp."""
        profile = NBM_CONUS_FIXTURE_GRID_PROFILE
        crs, x, y, lat, lon = compute_nbm_grid(profile)
        assert isinstance(crs, pyproj.CRS)
        assert crs.is_projected
        assert x.shape == (profile.nx,)
        assert y.shape == (profile.ny,)
        # Projected axes advance by exactly the profile's metre spacing.
        assert np.diff(x) == pytest.approx(profile.dx_metres)
        assert np.diff(y) == pytest.approx(profile.dy_metres)
        # ... which is nothing like a degree ramp.
        assert abs(float(x[1] - x[0])) > 1000.0
        # The lat/lon mesh is a true inverse projection: its rows are not
        # constant-latitude lines, which a geographic mesh would be.
        assert lat.shape == (profile.ny, profile.nx)
        assert lon.shape == (profile.ny, profile.nx)
        assert float(lat[0].max() - lat[0].min()) > 1e-3
        assert np.isfinite(lat).all() and np.isfinite(lon).all()

    def test_fixture_grid_covers_the_configured_domain_plus_halo(self) -> None:
        """The reduced fixture grid must still contain the whole
        Grasston bbox, so bilinear station extraction is genuinely
        exercised rather than trivially skipped."""
        domain = _phase2_configuration().domain
        _crs, _x, _y, lat, lon = compute_nbm_grid(NBM_CONUS_FIXTURE_GRID_PROFILE)
        lon_180 = np.where(lon > 180.0, lon - 360.0, lon)
        assert lat.min() < domain.bbox.south and lat.max() > domain.bbox.north
        assert lon_180.min() < domain.bbox.west and lon_180.max() > domain.bbox.east
