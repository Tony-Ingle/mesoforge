"""Contract tests for mesoforge.catalog.grids.GridDefinition (Task 4, plan
Section 4.4).

RED: written before GridDefinition exists.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mesoforge.catalog.grids import GridDefinition


def _wkt2() -> str:
    # A minimal, valid WGS84 geographic CRS WKT2_2019 string that pyproj
    # can parse and re-normalize.
    return (
        'GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",'
        'ELLIPSOID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],'
        'CS[ellipsoidal,2],AXIS["geodetic latitude",north],'
        'AXIS["geodetic longitude",east],ANGLEUNIT["degree",0.0174532925199433],'
        'ID["EPSG",4326]]'
    )


def _base_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = dict(
        grid_id="synthetic-grid.v1",
        crs_wkt2=_wkt2(),
        shape_y=2,
        shape_x=2,
        x_coordinates=(-100.0, -99.0),
        y_coordinates=(39.0, 40.0),
        coordinate_reference="cell_center",
        longitude_convention="minus_180_to_180",
        orientation="x_east_y_north",
        spatial_support="cell_mean",
    )
    kwargs.update(overrides)
    return kwargs


class TestGridDefinition:
    def test_valid_grid_constructs(self) -> None:
        grid = GridDefinition(**_base_kwargs())
        assert grid.grid_id == "synthetic-grid.v1"
        assert grid.shape_y == 2
        assert grid.shape_x == 2

    def test_crs_wkt2_is_normalized(self) -> None:
        grid = GridDefinition(**_base_kwargs())
        assert "WGS 84" in grid.crs_wkt2
        assert grid.crs_wkt2.startswith("GEOGCRS") or "GEOGCRS" in grid.crs_wkt2

    def test_rejects_non_monotonic_x_coordinates(self) -> None:
        with pytest.raises(ValidationError):
            GridDefinition(**_base_kwargs(x_coordinates=(-99.0, -100.0, -98.0)))

    def test_rejects_length_mismatch_with_shape(self) -> None:
        with pytest.raises(ValidationError):
            GridDefinition(**_base_kwargs(x_coordinates=(-100.0, -99.0, -98.0)))

    def test_rejects_non_finite_coordinates(self) -> None:
        with pytest.raises(ValidationError):
            GridDefinition(**_base_kwargs(y_coordinates=(39.0, float("inf"))))

    def test_rejects_non_positive_shape(self) -> None:
        with pytest.raises(ValidationError):
            GridDefinition(**_base_kwargs(shape_x=0))

    def test_orientation_other_requires_note(self) -> None:
        with pytest.raises(ValidationError):
            GridDefinition(**_base_kwargs(orientation="other"))

    def test_orientation_other_with_note_is_valid(self) -> None:
        grid = GridDefinition(**_base_kwargs(orientation="other", orientation_note="custom"))
        assert grid.orientation == "other"
        assert grid.orientation_note == "custom"

    def test_definition_digest_excludes_grid_id(self) -> None:
        grid_a = GridDefinition(**_base_kwargs(grid_id="synthetic-grid.v1"))
        grid_b = GridDefinition(**_base_kwargs(grid_id="synthetic-grid.v2"))
        assert grid_a.definition_digest == grid_b.definition_digest

    def test_definition_digest_changes_with_coordinates(self) -> None:
        grid_a = GridDefinition(**_base_kwargs())
        grid_b = GridDefinition(**_base_kwargs(y_coordinates=(39.0, 41.0)))
        assert grid_a.definition_digest != grid_b.definition_digest

    def test_schema_version_pinned(self) -> None:
        grid = GridDefinition(**_base_kwargs())
        assert grid.schema_version == "grid-definition.v1"

    def test_frozen(self) -> None:
        grid = GridDefinition(**_base_kwargs())
        with pytest.raises(ValidationError):
            grid.shape_x = 3  # type: ignore[misc]

    def test_optional_terrain_artifact_id(self) -> None:
        grid = GridDefinition(
            **_base_kwargs(terrain_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000")
        )
        assert grid.terrain_artifact_id is not None
