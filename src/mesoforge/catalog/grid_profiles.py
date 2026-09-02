"""Approved operational source-grid profiles (plan Section 2.3; Codex
re-review finding 2).

A model's native grid is part of its scientific contract, not incidental
metadata: two messages that agree on discipline/category/number/level but
disagree on projection, shape, increments, scan order, or geographic
coverage describe different physical fields sampled differently, and
blending or interpolating them as if they were the same grid is a silent
scientific error.

``NbmGridProfile`` pins every one of those properties exactly, so
``guidance.sources.nbm_decoding`` can assert a decoded message against the
configured profile instead of merely checking that *some* nonempty
``gridType`` and *some* positive ``Nx``/``Ny`` were present, and
``guidance.normalization_v2`` can build the real projected CRS from the
profile rather than treating projected grid indices as a geographic mesh.

``NBM_CONUS_OPERATIONAL_GRID_PROFILE`` is the exact approved operational
NBM CONUS core grid (NCEP grid 184-equivalent Lambert conformal conic,
2345 x 1597 at 2.539703 km, spherical earth radius 6371200 m). The
production configuration must declare exactly this profile; that
equality is asserted by ``tests/unit/catalog/test_grid_profiles.py``.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class NbmGridProfile(BaseModel):
    """One exactly-pinned NBM projected source-grid contract.

    ``first_longitude_degrees``/``last_longitude_degrees`` are stored in
    the provider's own ``[0, 360)`` GRIB convention (that is how eccodes
    reports ``longitudeOfFirstGridPointInDegrees`` for these products);
    conversion to ``[-180, 180)`` happens only where a projection
    transform needs it, never by rewriting the pinned contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["nbm-grid-profile.v1"] = "nbm-grid-profile.v1"
    profile_id: str
    grid_type: Literal["lambert"] = "lambert"
    nx: int
    ny: int
    dx_metres: float
    dy_metres: float
    lov_degrees: float
    lad_degrees: float
    latin1_degrees: float
    latin2_degrees: float
    earth_radius_metres: float
    first_latitude_degrees: float
    first_longitude_degrees: float
    last_latitude_degrees: float
    last_longitude_degrees: float
    i_scans_negatively: int
    j_scans_positively: int
    j_points_are_consecutive: int
    coordinate_tolerance_degrees: float = 0.01
    increment_tolerance_metres: float = 0.5

    @model_validator(mode="after")
    def _check_shape_and_geometry(self) -> NbmGridProfile:
        if not self.profile_id:
            raise ValueError("profile_id must not be empty")
        if self.nx <= 0 or self.ny <= 0:
            raise ValueError(f"nx/ny must both be positive, got {(self.nx, self.ny)!r}")
        for name, value in (("dx_metres", self.dx_metres), ("dy_metres", self.dy_metres)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive, got {value!r}")
        if not math.isfinite(self.earth_radius_metres) or self.earth_radius_metres <= 0:
            raise ValueError(
                f"earth_radius_metres must be finite and positive, got {self.earth_radius_metres!r}"
            )
        for name, value in (
            ("first_latitude_degrees", self.first_latitude_degrees),
            ("last_latitude_degrees", self.last_latitude_degrees),
            ("lad_degrees", self.lad_degrees),
            ("latin1_degrees", self.latin1_degrees),
            ("latin2_degrees", self.latin2_degrees),
        ):
            if not math.isfinite(value) or not (-90.0 <= value <= 90.0):
                raise ValueError(f"{name} must be finite and in [-90, 90], got {value!r}")
        for name, value in (
            ("first_longitude_degrees", self.first_longitude_degrees),
            ("last_longitude_degrees", self.last_longitude_degrees),
            ("lov_degrees", self.lov_degrees),
        ):
            if not math.isfinite(value) or not (0.0 <= value < 360.0):
                raise ValueError(
                    f"{name} must be finite and in the provider's [0, 360) convention, "
                    f"got {value!r}"
                )
        for name, value in (
            ("i_scans_negatively", self.i_scans_negatively),
            ("j_scans_positively", self.j_scans_positively),
            ("j_points_are_consecutive", self.j_points_are_consecutive),
        ):
            if value not in (0, 1):
                raise ValueError(f"{name} must be exactly 0 or 1, got {value!r}")
        if self.coordinate_tolerance_degrees <= 0 or self.coordinate_tolerance_degrees > 1.0:
            raise ValueError(
                "coordinate_tolerance_degrees must be a small positive value in (0, 1], got "
                f"{self.coordinate_tolerance_degrees!r}"
            )
        if self.increment_tolerance_metres <= 0 or self.increment_tolerance_metres > 100.0:
            raise ValueError(
                "increment_tolerance_metres must be a small positive value in (0, 100], got "
                f"{self.increment_tolerance_metres!r}"
            )
        return self

    @property
    def shape(self) -> tuple[int, int]:
        """The decoded array's expected ``(ny, nx)`` shape."""
        return (self.ny, self.nx)


# The exact approved operational NBM CONUS core grid. Longitudes are in
# the provider's [0, 360) GRIB convention: 233.7234 == -126.2766 and
# 300.9578099399543 == -59.0421900600457. The last-point coverage is the
# exact inverse projection of the (nx-1, ny-1) corner under this
# profile's own projection parameters, so any mutation of the
# projection, shape, or increments contradicts it.
NBM_CONUS_OPERATIONAL_GRID_PROFILE = NbmGridProfile(
    profile_id="nbm-core-conus-operational.v1",
    nx=2345,
    ny=1597,
    dx_metres=2539.703,
    dy_metres=2539.703,
    lov_degrees=265.0,
    lad_degrees=25.0,
    latin1_degrees=25.0,
    latin2_degrees=25.0,
    earth_radius_metres=6371200.0,
    first_latitude_degrees=19.229,
    first_longitude_degrees=233.7234,
    last_latitude_degrees=54.372790606545614,
    last_longitude_degrees=300.9578099399543,
    i_scans_negatively=0,
    j_scans_positively=1,
    j_points_are_consecutive=0,
)

# The deterministic, explicitly non-operational reduced Lambert grid used
# by synthetic NBM GRIB2 fixtures (unit, contract, and offline
# acceptance). It shares the operational projection parameters exactly --
# only the shape/increment/origin are reduced so eccodes fixtures stay
# small -- and it still covers the whole configured Grasston bbox plus a
# halo, so bilinear station extraction exercises the same code path.
# Declaring it as a registered profile (rather than letting any settings
# object invent a grid) means a mutated fixture grid is rejected at
# configuration load, exactly like a mutated operational grid.
NBM_CONUS_FIXTURE_GRID_PROFILE = NbmGridProfile(
    profile_id="nbm-core-conus-fixture.v1",
    nx=24,
    ny=22,
    dx_metres=25000.0,
    dy_metres=25000.0,
    lov_degrees=265.0,
    lad_degrees=25.0,
    latin1_degrees=25.0,
    latin2_degrees=25.0,
    earth_radius_metres=6371200.0,
    first_latitude_degrees=44.2,
    first_longitude_degrees=264.5,
    last_latitude_degrees=48.46694365058596,
    last_longitude_degrees=271.5835203897349,
    i_scans_negatively=0,
    j_scans_positively=1,
    j_points_are_consecutive=0,
)

APPROVED_NBM_GRID_PROFILES: dict[str, NbmGridProfile] = {
    NBM_CONUS_OPERATIONAL_GRID_PROFILE.profile_id: NBM_CONUS_OPERATIONAL_GRID_PROFILE,
    NBM_CONUS_FIXTURE_GRID_PROFILE.profile_id: NBM_CONUS_FIXTURE_GRID_PROFILE,
}


def require_approved_nbm_grid_profile(profile: NbmGridProfile) -> NbmGridProfile:
    """Return ``profile`` only when it is byte-identical to the approved
    registered profile carrying its ``profile_id``.

    This is the fail-closed gate the Codex re-review requires: a
    configuration that mutates the projection, shape, increments, scan
    order, or first/last-point coverage of an approved NBM grid no longer
    loads at all, instead of silently reaching decoding and being
    accepted because "some" grid keys were present.
    """
    approved = APPROVED_NBM_GRID_PROFILES.get(profile.profile_id)
    if approved is None:
        raise ValueError(
            f"NBM grid profile_id {profile.profile_id!r} is not an approved profile; "
            f"approved profiles are {sorted(APPROVED_NBM_GRID_PROFILES)!r}"
        )
    if profile != approved:
        differing = sorted(
            name
            for name in type(profile).model_fields
            if getattr(profile, name) != getattr(approved, name)
        )
        raise ValueError(
            f"NBM grid profile {profile.profile_id!r} does not match the approved profile; "
            f"differing field(s): {differing!r}"
        )
    return profile
