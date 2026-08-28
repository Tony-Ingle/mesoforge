"""Unit tests for mesoforge.catalog.configuration (Task 6, plan Section 4.7).

RED: written before src/mesoforge/catalog/configuration.py exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mesoforge.catalog.configuration import (
    MesoForgeConfiguration,
    load_configuration_source,
    resolve_configuration,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_CONFIG = REPO_ROOT / "configs" / "base.yaml"
LOCAL_ENV_CONFIG = REPO_ROOT / "configs" / "environments" / "local.yaml"


class TestLoadConfigurationSource:
    def test_loads_and_validates_base_config(self) -> None:
        config, source_refs = load_configuration_source(base_path=BASE_CONFIG)
        assert isinstance(config, MesoForgeConfiguration)
        assert config.schema_version == "mesoforge-config.v1"
        assert len(config.grids) == 1
        assert source_refs[0].path.endswith("base.yaml")

    def test_merges_environment_overlay(self) -> None:
        config, _ = load_configuration_source(
            base_path=BASE_CONFIG, environment_path=LOCAL_ENV_CONFIG
        )
        assert config.artifact_store.bucket == "mesoforge-phase0-local"

    def test_rejects_unknown_top_level_key(self, tmp_path: Path) -> None:
        bad = tmp_path / "base.yaml"
        bad.write_text(
            "grids: []\nvertical_definitions: []\nvariables: []\n"
            "artifact_store:\n  endpoint_reference: X\n  bucket: b\n"
            "metadata_store:\n  dsn_environment_variable: D\n"
            "unknown_key: true\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            load_configuration_source(base_path=bad)

    def test_rejects_secret_shaped_field(self, tmp_path: Path) -> None:
        bad = tmp_path / "base.yaml"
        bad.write_text(
            "grids: []\nvertical_definitions: []\nvariables: []\n"
            "artifact_store:\n  endpoint_reference: X\n  bucket: b\n"
            "metadata_store:\n  dsn_environment_variable: D\n  dsn: postgresql://u:p@h/d\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            load_configuration_source(base_path=bad)

    def test_rejects_duplicate_grid_ids(self, tmp_path: Path) -> None:
        bad = tmp_path / "base.yaml"
        bad.write_text(
            """
grids:
  - grid_id: g.v1
    crs_wkt2: |
      GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",
      ELLIPSOID["WGS 84",6378137,298.257223563]],
      PRIMEM["Greenwich",0],
      CS[ellipsoidal,2],AXIS["geodetic latitude",north],
      AXIS["geodetic longitude",east],ANGLEUNIT["degree",0.0174532925199433],
      ID["EPSG",4326]]
    shape_y: 1
    shape_x: 1
    x_coordinates: [0.0]
    y_coordinates: [0.0]
    coordinate_reference: cell_center
    longitude_convention: minus_180_to_180
    orientation: x_east_y_north
    spatial_support: cell_mean
  - grid_id: g.v1
    crs_wkt2: |
      GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",
      ELLIPSOID["WGS 84",6378137,298.257223563]],
      PRIMEM["Greenwich",0],
      CS[ellipsoidal,2],AXIS["geodetic latitude",north],
      AXIS["geodetic longitude",east],ANGLEUNIT["degree",0.0174532925199433],
      ID["EPSG",4326]]
    shape_y: 1
    shape_x: 1
    x_coordinates: [1.0]
    y_coordinates: [1.0]
    coordinate_reference: cell_center
    longitude_convention: minus_180_to_180
    orientation: x_east_y_north
    spatial_support: cell_mean
vertical_definitions: []
variables: []
artifact_store:
  endpoint_reference: X
  bucket: b
metadata_store:
  dsn_environment_variable: D
""",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            load_configuration_source(base_path=bad)

    def test_environment_overlay_cannot_override_scientific_fields(self, tmp_path: Path) -> None:
        base = tmp_path / "base.yaml"
        base.write_text(
            "grids: []\nvertical_definitions: []\nvariables: []\n"
            "artifact_store:\n  endpoint_reference: X\n  bucket: b\n"
            "metadata_store:\n  dsn_environment_variable: D\n",
            encoding="utf-8",
        )
        env = tmp_path / "env.yaml"
        env.write_text("grids:\n  - grid_id: sneaky\n", encoding="utf-8")
        with pytest.raises(ValidationError):
            load_configuration_source(base_path=base, environment_path=env)


class TestResolveConfiguration:
    def test_override_paths_are_applied(self) -> None:
        config, _ = load_configuration_source(base_path=BASE_CONFIG)
        resolved = resolve_configuration(
            config,
            overrides={
                "artifact_store.bucket": "override-bucket",
                "artifact_store.endpoint_reference": "OVERRIDE_ENDPOINT",
                "metadata_store.dsn_environment_variable": "OVERRIDE_DSN",
            },
        )
        assert resolved.artifact_store.bucket == "override-bucket"
        assert resolved.artifact_store.endpoint_reference == "OVERRIDE_ENDPOINT"
        assert resolved.metadata_store.dsn_environment_variable == "OVERRIDE_DSN"

    def test_rejects_scientific_override_path(self) -> None:
        config, _ = load_configuration_source(base_path=BASE_CONFIG)
        with pytest.raises(ValueError, match="not a permitted override path"):
            resolve_configuration(config, overrides={"grids": []})

    def test_rejects_unknown_override_path(self) -> None:
        config, _ = load_configuration_source(base_path=BASE_CONFIG)
        with pytest.raises(ValueError, match="not a permitted override path"):
            resolve_configuration(config, overrides={"artifact_store.unknown": "x"})
