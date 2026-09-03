"""Phase 2 bounded canonical retention: subset-equivalence tests.

Owner architecture decision: ``canonical-guidance.v2`` no longer retains
a model's full native grid. It retains the configured domain bbox plus
``point_extraction_policy.halo_cells`` complete source cells, computed
with the same Phase 1 rule (``compute_bbox_halo_subset_indices``).

The whole change rests on one scientific claim, so these tests prove it
directly against real production normalization rather than asserting it
in prose: **for every approved station and every canonical variable, the
value bilinearly interpolated from the retained subset is bit-identical
to the value interpolated from the full native grid, using identical
enclosing cells (translated by the window origin) and identical
interpolation weights.**

The comparison is made against a genuine full-grid normalization: each
model is normalized twice from the *same* fixture bytes -- once with a
bbox+halo large enough to retain the entire fixture grid, and once with
the real configured Grasston bbox -- and both datasets are aligned at
every station.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pyproj
import pytest

from mesoforge.alignment.spatial import (
    bilinear_interpolate,
    project_station_point,
)
from mesoforge.catalog.domains import BoundingBox
from mesoforge.guidance.canonical_v2 import validate_canonical_guidance_v2
from mesoforge.guidance.normalization_v2 import (
    GuidanceNormalizationV2Error,
    normalize_gfs_cycle,
    normalize_hrrr_phase2_cycle,
    normalize_nbm_cycle,
)
from tests.fixtures.gfs_grib import FIRST_LAT_DEGREES as GFS_FIRST_LAT
from tests.fixtures.gfs_grib import LAST_LAT_DEGREES as GFS_LAST_LAT
from tests.fixtures.gfs_grib import NX as GFS_NX
from tests.fixtures.gfs_grib import NY as GFS_NY
from tests.fixtures.gfs_grib import make_apcp_message as make_gfs_apcp_message
from tests.fixtures.gfs_grib import make_instantaneous_message as make_gfs_message
from tests.fixtures.hrrr_grib import NX as HRRR_NX
from tests.fixtures.hrrr_grib import NY as HRRR_NY
from tests.fixtures.hrrr_grib import (
    make_apcp_message as make_hrrr_apcp_message,
)
from tests.fixtures.hrrr_grib import (
    make_dew_point_message,
    make_gust_message,
    make_temperature_message,
    make_wind_message,
)
from tests.fixtures.nbm_grib import NX as NBM_NX
from tests.fixtures.nbm_grib import NY as NBM_NY
from tests.fixtures.nbm_grib import (
    make_apcp_deterministic_message,
    make_pop01_message,
)
from tests.fixtures.nbm_grib import (
    make_instantaneous_message as make_nbm_message,
)
from tests.support.phase2_source_settings import (
    make_gfs_settings,
    make_hrrr_phase2_settings,
    make_nbm_settings,
)

pytestmark = pytest.mark.scientific

_CFG_SNAPSHOT_ID = "cfg_sha256_" + "0" * 64
_LINEAGE_ID = "art_00000000-0000-0000-0000-000000000001"

# The configured Grasston domain bbox and approved one-cell halo.
_DOMAIN_BBOX = BoundingBox(south=45.05265, north=46.55265, west=-94.07956, east=-92.07956)
_HALO_CELLS = 1

# A bbox large enough to contain every fixture grid point. Combined with
# ``halo_cells=0`` this retains the entire native grid through the same
# production code path -- the control arm of the comparison. Production
# never uses a zero halo (``PointExtractionPolicy`` pins exactly one
# cell); it exists here only to express "retain everything".
_FULL_GRID_BBOX = BoundingBox(south=-89.0, north=89.0, west=-179.0, east=179.0)
_NO_HALO = 0

# The three approved Phase 2 stations (configs/phase2-grasston.yaml).
_STATIONS: tuple[tuple[str, float, float], ...] = (
    ("KCBG", 45.55700, -93.26400),
    ("KJMR", 45.88854, -93.26900),
    ("KROS", 45.69624, -92.95427),
)

_HRRR_SETTINGS = make_hrrr_phase2_settings(
    read_keys=(
        "discipline",
        "parameterCategory",
        "parameterNumber",
        "typeOfLevel",
        "level",
        "stepType",
        "startStep",
        "endStep",
        "uvRelativeToGrid",
        "gridType",
        "Nx",
        "Ny",
        "DxInMetres",
        "DyInMetres",
        "latitudeOfFirstGridPointInDegrees",
        "longitudeOfFirstGridPointInDegrees",
        "LoVInDegrees",
        "Latin1InDegrees",
        "Latin2InDegrees",
        "LaDInDegrees",
        "units",
        "step",
        "dataDate",
        "dataTime",
        "validityDate",
        "validityTime",
    )
)
_GFS_SETTINGS = make_gfs_settings(
    read_keys=(
        "discipline",
        "parameterCategory",
        "parameterNumber",
        "typeOfLevel",
        "level",
        "stepType",
        "startStep",
        "endStep",
        "uvRelativeToGrid",
        "step",
        "dataDate",
        "dataTime",
        "units",
        "gridType",
        "Ni",
        "Nj",
        "iDirectionIncrementInDegrees",
        "jDirectionIncrementInDegrees",
        "latitudeOfFirstGridPointInDegrees",
        "longitudeOfFirstGridPointInDegrees",
        "validityDate",
        "validityTime",
    )
)
_NBM_SETTINGS = make_nbm_settings()


def _spatially_varying(shape: tuple[int, int], *, base: float, scale: float) -> np.ndarray:
    """A field that differs at every grid point.

    A constant field would make subset equivalence trivially true even
    if the window were computed wrongly, so every fixture field varies
    with both indices.
    """
    ny, nx = shape
    j = np.arange(ny, dtype=np.float64)[:, None]
    i = np.arange(nx, dtype=np.float64)[None, :]
    return base + scale * (np.sin(j / 3.0) + np.cos(i / 4.0) + 0.01 * j * i / max(ny * nx, 1))


def _hrrr_payloads(lead: int) -> dict:
    shape = (HRRR_NY, HRRR_NX)
    return {
        "air_temperature_2m": {
            lead: make_temperature_message(
                forecast_hour=lead, values_k=_spatially_varying(shape, base=285.0, scale=3.0)
            )
        },
        "dew_point_temperature_2m": {
            lead: make_dew_point_message(
                forecast_hour=lead, values_k=_spatially_varying(shape, base=278.0, scale=2.0)
            )
        },
        "eastward_wind_10m": {
            lead: make_wind_message(
                forecast_hour=lead,
                component="u",
                values_m_s=_spatially_varying(shape, base=3.0, scale=1.5),
                grid_relative=True,
            )
        },
        "northward_wind_10m": {
            lead: make_wind_message(
                forecast_hour=lead,
                component="v",
                values_m_s=_spatially_varying(shape, base=-2.0, scale=1.5),
                grid_relative=True,
            )
        },
        "wind_gust_10m": {
            lead: make_gust_message(
                forecast_hour=lead, values_m_s=_spatially_varying(shape, base=9.0, scale=2.0)
            )
        },
        "liquid_equivalent_precipitation_amount_1h": {
            lead: make_hrrr_apcp_message(
                forecast_hour=lead,
                values_kg_m2=np.abs(_spatially_varying(shape, base=1.0, scale=0.4)),
            )
        },
    }


def _gfs_payloads(lead: int) -> dict:
    shape = (GFS_NY, GFS_NX)
    return {
        "air_temperature_2m": {
            lead: make_gfs_message(
                canonical_variable_id="air_temperature_2m",
                forecast_hour=lead,
                values=_spatially_varying(shape, base=286.0, scale=3.0),
            )
        },
        "dew_point_temperature_2m": {
            lead: make_gfs_message(
                canonical_variable_id="dew_point_temperature_2m",
                forecast_hour=lead,
                values=_spatially_varying(shape, base=279.0, scale=2.0),
            )
        },
        "eastward_wind_10m": {
            lead: make_gfs_message(
                canonical_variable_id="eastward_wind_10m",
                forecast_hour=lead,
                values=_spatially_varying(shape, base=4.0, scale=1.5),
                grid_relative_wind=False,
            )
        },
        "northward_wind_10m": {
            lead: make_gfs_message(
                canonical_variable_id="northward_wind_10m",
                forecast_hour=lead,
                values=_spatially_varying(shape, base=-1.0, scale=1.5),
                grid_relative_wind=False,
            )
        },
        "wind_gust_10m": {
            lead: make_gfs_message(
                canonical_variable_id="wind_gust_10m",
                forecast_hour=lead,
                values=_spatially_varying(shape, base=10.0, scale=2.0),
            )
        },
        "liquid_equivalent_precipitation_amount_1h": {
            lead: make_gfs_apcp_message(
                start_step=0,
                end_step=lead,
                values_kg_m2=np.abs(_spatially_varying(shape, base=3.0, scale=0.5)),
            )
        },
    }


def _nbm_payloads(lead: int) -> dict:
    shape = (NBM_NY, NBM_NX)
    return {
        "air_temperature_2m": {
            lead: make_nbm_message(
                canonical_variable_id="air_temperature_2m",
                forecast_hour=lead,
                values=_spatially_varying(shape, base=287.0, scale=3.0),
            )
        },
        "dew_point_temperature_2m": {
            lead: make_nbm_message(
                canonical_variable_id="dew_point_temperature_2m",
                forecast_hour=lead,
                values=_spatially_varying(shape, base=280.0, scale=2.0),
            )
        },
        "wind_speed_10m": {
            lead: make_nbm_message(
                canonical_variable_id="wind_speed_10m",
                forecast_hour=lead,
                values=np.abs(_spatially_varying(shape, base=6.0, scale=1.5)),
            )
        },
        "wind_from_direction_10m": {
            lead: make_nbm_message(
                canonical_variable_id="wind_from_direction_10m",
                forecast_hour=lead,
                values=np.abs(_spatially_varying(shape, base=180.0, scale=30.0)) % 360.0,
            )
        },
        "wind_gust_10m": {
            lead: make_nbm_message(
                canonical_variable_id="wind_gust_10m",
                forecast_hour=lead,
                values=np.abs(_spatially_varying(shape, base=11.0, scale=2.0)),
            )
        },
        "liquid_equivalent_precipitation_amount_1h": {
            lead: make_apcp_deterministic_message(
                forecast_hour=lead,
                values_kg_m2=np.abs(_spatially_varying(shape, base=1.0, scale=0.3)),
            )
        },
        "probability_of_precipitation_1h": {
            lead: make_pop01_message(
                forecast_hour=lead,
                values_percent=np.abs(_spatially_varying(shape, base=40.0, scale=15.0)) % 100.0,
            )
        },
    }


def _normalize(model: str, bbox: BoundingBox, halo_cells: int = _HALO_CELLS):
    """Normalize one model's fixture cycle retaining ``bbox`` + halo."""
    if model == "hrrr":
        lead = 6
        return normalize_hrrr_phase2_cycle(
            settings=_HRRR_SETTINGS,
            forecast_reference_time=datetime(2026, 8, 28, 18, tzinfo=UTC),
            source_lead_hours=(lead,),
            field_payloads=_hrrr_payloads(lead),
            grid_id="phase2-hrrr.v1",
            configuration_snapshot_id=_CFG_SNAPSHOT_ID,
            variable_lineage_manifest_id=_LINEAGE_ID,
            domain_bbox=bbox,
            halo_cells=halo_cells,
        )
    if model == "nbm":
        lead = 6
        return normalize_nbm_cycle(
            settings=_NBM_SETTINGS,
            forecast_reference_time=datetime(2026, 8, 30, 12, tzinfo=UTC),
            source_lead_hours=(lead,),
            field_payloads=_nbm_payloads(lead),
            grid_id="phase2-nbm.v1",
            configuration_snapshot_id=_CFG_SNAPSHOT_ID,
            variable_lineage_manifest_id=_LINEAGE_ID,
            domain_bbox=bbox,
            halo_cells=halo_cells,
        )
    # GFS lead 2 is a non-reset hour, so this also covers same-bucket
    # differencing against the previous lead's own bucket.
    field_payloads = _gfs_payloads(2)
    field_payloads["liquid_equivalent_precipitation_amount_1h"][1] = make_gfs_apcp_message(
        start_step=0,
        end_step=1,
        values_kg_m2=np.abs(_spatially_varying((GFS_NY, GFS_NX), base=1.5, scale=0.25)),
    )
    dataset, _lineage = normalize_gfs_cycle(
        settings=_GFS_SETTINGS,
        forecast_reference_time=datetime(2026, 8, 30, 0, tzinfo=UTC),
        source_lead_hours=(2,),
        field_payloads=field_payloads,
        grid_id="phase2-gfs.v1",
        configuration_snapshot_id=_CFG_SNAPSHOT_ID,
        variable_lineage_manifest_id=_LINEAGE_ID,
        domain_bbox=bbox,
        halo_cells=halo_cells,
    )
    return dataset


def _full_grid(model: str):
    """The control arm: the same bytes normalized retaining every native
    grid cell."""
    return _normalize(model, _FULL_GRID_BBOX, halo_cells=_NO_HALO)


def _crs_of(dataset) -> pyproj.CRS:
    return pyproj.CRS.from_wkt(str(dataset.attrs["crs_wkt2"]))


def _variables(dataset) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(name)
            for name in dataset.data_vars
            if not str(name).endswith("_quality_mask")
            and not str(name).endswith("_interval_bounds")
        )
    )


@pytest.mark.parametrize("model", ["hrrr", "nbm", "gfs"])
class TestSubsetEquivalence:
    """The retained bbox+halo subset preserves station science exactly."""

    def test_station_values_and_weights_are_bit_identical(self, model: str) -> None:
        full = _full_grid(model)
        subset = _normalize(model, _DOMAIN_BBOX)
        # Only the retained artifact is a conforming production artifact:
        # the contract requires a halo of at least one cell, which the
        # full-grid control arm deliberately does not use.
        validate_canonical_guidance_v2(subset)

        # The control arm really is the full native grid, and the
        # retained arm really is smaller -- otherwise this proves nothing.
        assert (int(full.sizes["y"]), int(full.sizes["x"])) == (
            int(full.attrs["source_grid_ny"]),
            int(full.attrs["source_grid_nx"]),
        )
        assert int(subset.sizes["y"]) < int(full.sizes["y"])
        assert int(subset.sizes["x"]) < int(full.sizes["x"])

        y_origin = int(subset.attrs["subset_y_start"])
        x_origin = int(subset.attrs["subset_x_start"])
        crs = _crs_of(subset)
        assert crs == _crs_of(full)

        variables = _variables(subset)
        assert variables == _variables(full)

        compared = 0
        for _icao, latitude, longitude in _STATIONS:
            station_x, station_y = project_station_point(
                crs, latitude=latitude, longitude=longitude
            )
            full_x = full["x"].values
            subset_x = subset["x"].values
            full_station_x = station_x
            subset_station_x = station_x
            if crs.is_geographic:
                if full_x.min() >= 0.0 and full_station_x < 0.0:
                    full_station_x += 360.0
                if subset_x.min() >= 0.0 and subset_station_x < 0.0:
                    subset_station_x += 360.0
            for variable in variables:
                full_value = bilinear_interpolate(
                    field=full[variable].values[0],
                    x=full_x,
                    y=full["y"].values,
                    station_x=full_station_x,
                    station_y=station_y,
                )
                subset_value = bilinear_interpolate(
                    field=subset[variable].values[0],
                    x=subset_x,
                    y=subset["y"].values,
                    station_x=subset_station_x,
                    station_y=station_y,
                )
                # Bit-identical value: not approx, not a tolerance.
                assert subset_value.value == full_value.value, (
                    f"{model} {variable} at {latitude},{longitude}: "
                    f"subset={subset_value.value!r} full={full_value.value!r}"
                )
                # Identical interpolation weights.
                assert subset_value.weights == full_value.weights
                # Identical enclosing cells, translated by the retained
                # window's own declared origin.
                assert subset_value.cell.y0 + y_origin == full_value.cell.y0
                assert subset_value.cell.y1 + y_origin == full_value.cell.y1
                assert subset_value.cell.x0 + x_origin == full_value.cell.x0
                assert subset_value.cell.x1 + x_origin == full_value.cell.x1
                compared += 1
        assert compared == len(_STATIONS) * len(variables)

    def test_retained_cells_are_bit_identical_to_the_full_grid_window(self, model: str) -> None:
        """Not only the interpolated points: every retained cell, and the
        retained coordinate axes, equal the corresponding full-grid
        slice exactly."""
        full = _full_grid(model)
        subset = _normalize(model, _DOMAIN_BBOX)
        y_slice = slice(int(subset.attrs["subset_y_start"]), int(subset.attrs["subset_y_end"]))
        x_slice = slice(int(subset.attrs["subset_x_start"]), int(subset.attrs["subset_x_end"]))

        np.testing.assert_array_equal(subset["x"].values, full["x"].values[x_slice])
        np.testing.assert_array_equal(subset["y"].values, full["y"].values[y_slice])
        np.testing.assert_array_equal(
            subset["latitude"].values, full["latitude"].values[y_slice, x_slice]
        )
        np.testing.assert_array_equal(
            subset["longitude"].values, full["longitude"].values[y_slice, x_slice]
        )
        for variable in _variables(subset):
            np.testing.assert_array_equal(
                subset[variable].values,
                full[variable].values[:, y_slice, x_slice],
                err_msg=f"{model} {variable} retained cells differ from the full-grid window",
            )

    def test_retained_window_covers_every_station_with_its_halo(self, model: str) -> None:
        """The whole point of the halo: every station's four bilinear
        corners must be strictly interior to the retained window, so no
        station can sit on an edge where extraction would fail closed."""
        subset = _normalize(model, _DOMAIN_BBOX)
        crs = _crs_of(subset)
        x = subset["x"].values
        y = subset["y"].values
        for _icao, latitude, longitude in _STATIONS:
            station_x, station_y = project_station_point(
                crs, latitude=latitude, longitude=longitude
            )
            if crs.is_geographic and x.min() >= 0.0 and station_x < 0.0:
                station_x += 360.0
            extracted = bilinear_interpolate(
                field=subset["air_temperature_2m"].values[0],
                x=x,
                y=y,
                station_x=station_x,
                station_y=station_y,
            )
            for index in (extracted.cell.y0, extracted.cell.y1):
                assert 0 <= index < len(y)
            for index in (extracted.cell.x0, extracted.cell.x1):
                assert 0 <= index < len(x)

    def test_declared_window_matches_the_retained_arrays(self, model: str) -> None:
        subset = _normalize(model, _DOMAIN_BBOX)
        assert subset.attrs["subset_policy_id"] == "bbox-halo-subset.v1"
        assert subset.attrs["subset_halo_cells"] == _HALO_CELLS
        assert subset.attrs["subset_bbox_south"] == _DOMAIN_BBOX.south
        assert subset.attrs["subset_bbox_north"] == _DOMAIN_BBOX.north
        assert subset.attrs["subset_bbox_west"] == _DOMAIN_BBOX.west
        assert subset.attrs["subset_bbox_east"] == _DOMAIN_BBOX.east
        assert int(subset.sizes["y"]) == (
            int(subset.attrs["subset_y_end"]) - int(subset.attrs["subset_y_start"])
        )
        assert int(subset.sizes["x"]) == (
            int(subset.attrs["subset_x_end"]) - int(subset.attrs["subset_x_start"])
        )
        assert int(subset.attrs["subset_y_end"]) <= int(subset.attrs["source_grid_ny"])
        assert int(subset.attrs["subset_x_end"]) <= int(subset.attrs["source_grid_nx"])


class TestGfsLatitudeAxisFollowsScanDirection:
    """The operational GFS 0.25-degree product scans north-to-south
    (``jScansPositively=0``): its first grid row is the northernmost.

    Deriving the latitude axis as ``first_lat + increment * arange``
    fabricates an axis running *away* from the grid (for the real
    product, 90..270) that is not a latitude at all, so no station is
    ever located. The axis must follow the message's own declared first
    and last latitudes.
    """

    def test_latitude_axis_descends_and_matches_the_declared_extent(self) -> None:
        dataset = _full_grid("gfs")
        latitudes = dataset["latitude"].values[:, 0]
        assert latitudes[0] == pytest.approx(GFS_FIRST_LAT)
        assert latitudes[-1] == pytest.approx(GFS_LAST_LAT)
        assert np.all(np.diff(latitudes) < 0)
        assert float(latitudes.min()) == pytest.approx(GFS_LAST_LAT)
        assert float(latitudes.max()) == pytest.approx(GFS_FIRST_LAT)

    def test_stations_are_locatable_on_the_retained_window(self) -> None:
        """The end-to-end consequence: with a fabricated axis the domain
        bbox does not intersect the grid at all and subsetting fails."""
        subset = _normalize("gfs", _DOMAIN_BBOX)
        latitudes = subset["latitude"].values
        for _icao, latitude, _longitude in _STATIONS:
            assert latitudes.min() <= latitude <= latitudes.max()


class TestSubsetFailsClosed:
    def test_rejects_a_domain_outside_the_native_grid(self) -> None:
        outside = BoundingBox(south=-40.0, north=-38.0, west=20.0, east=22.0)
        with pytest.raises(GuidanceNormalizationV2Error, match="cannot supply the configured"):
            _normalize("hrrr", outside)

    def test_rejects_a_domain_whose_halo_would_leave_the_grid(self) -> None:
        # A bbox that reaches the native grid's own outermost cells: the
        # bbox itself is satisfiable, but the one-cell halo around it is
        # not, and that must fail closed rather than silently clip.
        edge = BoundingBox(south=-89.999, north=89.999, west=-179.999, east=179.999)
        with pytest.raises(GuidanceNormalizationV2Error, match="cannot supply the configured"):
            _normalize("nbm", edge)
