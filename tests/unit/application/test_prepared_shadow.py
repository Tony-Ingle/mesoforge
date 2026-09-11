"""Independent geometry/time/value checks for decoded projected shadow preparation."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.alignment.station_frame import align_station_to_model
from mesoforge.application.prepared_shadow import normalize_shadow_temperature
from mesoforge.application.spatial_coverage import UnsupportedCoordinateError, bbox_in_grid
from mesoforge.catalog.domains import BoundingBox

CYCLE = datetime(2026, 9, 11, 3, tzinfo=UTC)
TARGET = CYCLE + timedelta(hours=3)
DX = 13545.087
CRS = pyproj.CRS.from_proj4(
    "+proj=lcc +lat_1=25 +lat_2=25 +lat_0=25 +lon_0=-95 +R=6371229 +units=m +no_defs"
)


def frame(lead=4, *, first_lon=-94.3):
    # Small synthetic grid in RAP's actual projection; no claimed real acquisition.
    x0, y0 = pyproj.Transformer.from_crs("EPSG:4326", CRS, always_xy=True).transform(
        first_lon, 44.9
    )
    xx, yy = np.meshgrid(x0 + DX * np.arange(19), y0 + DX * np.arange(17))
    lon, lat = pyproj.Transformer.from_crs(CRS, "EPSG:4326", always_xy=True).transform(xx, yy)
    columns, rows = np.meshgrid(np.arange(19), np.arange(17))
    return xr.DataArray(
        270.0 + lead + 0.2 * columns + 0.3 * rows,
        dims=("y", "x"),
        coords={
            "latitude": (("y", "x"), lat),
            "longitude": (("y", "x"), (lon + 360) % 360),
            "time": np.datetime64(CYCLE.replace(tzinfo=None), "ns"),
            "step": np.timedelta64(lead, "h"),
            "valid_time": np.datetime64((CYCLE + timedelta(hours=lead)).replace(tzinfo=None), "ns"),
        },
        attrs={
            "GRIB_units": "K",
            "GRIB_gridType": "lambert",
            "GRIB_iScansNegatively": 0,
            "GRIB_jScansPositively": 1,
            "GRIB_jPointsAreConsecutive": 0,
            "GRIB_alternativeRowScanning": 0,
            "GRIB_shapeOfTheEarth": 6,
            "GRIB_radius": 6371229,
            "GRIB_LoVInDegrees": 265.0,
            "GRIB_LaDInDegrees": 25.0,
            "GRIB_Latin1InDegrees": 25.0,
            "GRIB_Latin2InDegrees": 25.0,
            "GRIB_latitudeOfFirstGridPointInDegrees": 44.9,
            "GRIB_longitudeOfFirstGridPointInDegrees": first_lon + 360,
            "GRIB_DxInMetres": DX,
            "GRIB_DyInMetres": DX,
            "GRIB_Nx": 19,
            "GRIB_Ny": 17,
        },
    )


def test_actual_projection_values_missing_hours_and_source_times_are_preserved():
    decoded = {6: frame(6), 4: frame(4)}
    originals = {lead: value.copy(deep=True) for lead, value in decoded.items()}
    result = normalize_shadow_temperature(decoded, model="RAP", cycle=CYCLE, target=TARGET)
    assert result.attrs["model"] == "RAP"
    assert result.attrs["target_reference_time"] == "2026-09-11T06:00:00Z"
    assert pyproj.CRS.from_wkt(result.attrs["crs_wkt2"]) == CRS
    assert result.air_temperature_2m.attrs == {"unit_id": "K", "units": "K"}
    assert list(result.source_lead_time.values / np.timedelta64(1, "h")) == [4, 6]
    np.testing.assert_array_equal(
        result.source_valid_time.values,
        np.array(["2026-09-11T07", "2026-09-11T09"], dtype="datetime64[ns]"),
    )
    np.testing.assert_array_equal(result.air_temperature_2m.values[0], decoded[4].values)
    np.testing.assert_allclose(np.diff(result.x.values), DX, rtol=0, atol=1e-8)
    np.testing.assert_allclose(np.diff(result.y.values), DX, rtol=0, atol=1e-8)
    x, y = result.x.values[0] + 5.5 * DX, result.y.values[0] + 6.25 * DX
    lon, lat = pyproj.Transformer.from_crs(CRS, "EPSG:4326", always_xy=True).transform(x, y)
    aligned = align_station_to_model(
        result,
        crs=CRS,
        station_latitude=lat,
        station_longitude=lon,
        canonical_variable_id="air_temperature_2m",
        target_horizon_hours=(1, 2, 3),
        target_reference_time=np.datetime64(TARGET.replace(tzinfo=None), "ns"),
    )
    assert set(aligned) == {1, 3}
    assert aligned[1].value == pytest.approx(270 + 4 + 0.2 * 5.5 + 0.3 * 6.25, abs=1e-9)
    assert aligned[3].value == pytest.approx(270 + 6 + 0.2 * 5.5 + 0.3 * 6.25, abs=1e-9)
    for lead in decoded:
        xr.testing.assert_identical(decoded[lead], originals[lead])


def test_footprint_subset_keeps_native_values_halo_and_full_source_extent():
    source = frame()
    latitude, longitude = (
        float(source.latitude.values[8, 9]),
        float(source.longitude.values[8, 9]) - 360,
    )
    area = BoundingBox(
        south=latitude - 0.02, north=latitude + 0.02, west=longitude - 0.02, east=longitude + 0.02
    )
    full = normalize_shadow_temperature({4: source}, model="RAP", cycle=CYCLE, target=TARGET)
    subset = normalize_shadow_temperature(
        {4: source}, model="RAP", cycle=CYCLE, target=TARGET, area=area
    )
    assert subset.sizes["x"] < full.sizes["x"] and subset.sizes["y"] < full.sizes["y"]
    assert bbox_in_grid(area, CRS, subset.x.values, subset.y.values)
    assert min(subset.sizes["x"], subset.sizes["y"]) >= 4
    xr.testing.assert_equal(
        subset.air_temperature_2m, full.air_temperature_2m.sel(x=subset.x, y=subset.y)
    )
    for name in ("source_x_min", "source_x_max", "source_y_min", "source_y_max"):
        assert subset.attrs[name] == full.attrs[name]
    assert subset.attrs["prepared_area_json"] == area.model_dump_json()


def test_footprint_outside_native_domain_is_explicit():
    with pytest.raises(UnsupportedCoordinateError, match="does not intersect"):
        normalize_shadow_temperature(
            {4: frame()},
            model="RAP",
            cycle=CYCLE,
            target=TARGET,
            area=BoundingBox(south=0, north=1, west=0, east=1),
        )


@pytest.mark.parametrize("name", ["time", "step", "valid_time"])
def test_decoded_source_time_mismatch_is_rejected(name):
    source = frame()
    source = source.assign_coords({name: source[name].values[()] + np.timedelta64(1, "h")})
    with pytest.raises(ValueError, match="cycle/lead/valid time mismatch"):
        normalize_shadow_temperature({4: source}, model="RAP", cycle=CYCLE, target=TARGET)


@pytest.mark.parametrize("lead", [True, 3, 40])
def test_unrequested_or_noninteger_leads_are_rejected(lead):
    with pytest.raises(ValueError, match="target hours 1 through 36"):
        normalize_shadow_temperature({lead: frame()}, model="RAP", cycle=CYCLE, target=TARGET)


@pytest.mark.parametrize(
    "attribute,value,message",
    [
        ("GRIB_units", "degC", "use K"),
        ("GRIB_iScansNegatively", 1, "scanning"),
        ("GRIB_radius", 6371000, "6371229"),
        ("GRIB_gridType", "regular_ll", "Lambert"),
    ],
)
def test_invalid_units_or_native_grid_metadata_is_rejected(attribute, value, message):
    source = frame()
    source.attrs[attribute] = value
    with pytest.raises(ValueError, match=message):
        normalize_shadow_temperature({4: source}, model="RAP", cycle=CYCLE, target=TARGET)


def test_grid_changes_between_leads_and_disagreeing_cells_are_rejected():
    with pytest.raises(ValueError, match="grid changes"):
        normalize_shadow_temperature(
            {4: frame(4), 5: frame(5, first_lon=-94.2)}, model="RAP", cycle=CYCLE, target=TARGET
        )
    source = frame()
    source["latitude"].values[0, 0] += 0.1
    with pytest.raises(ValueError, match="cells disagree"):
        normalize_shadow_temperature({4: source}, model="RAP", cycle=CYCLE, target=TARGET)


def test_missing_values_remain_missing_and_empty_input_does_not_invent_grid():
    source = frame()
    source.values[0, 0] = np.nan
    result = normalize_shadow_temperature({4: source}, model="RAP", cycle=CYCLE, target=TARGET)
    assert np.isnan(result.air_temperature_2m.values[0, 0, 0])
    with pytest.raises(ValueError, match="No decoded"):
        normalize_shadow_temperature({}, model="RAP", cycle=CYCLE, target=TARGET)


@pytest.mark.parametrize(
    "cycle,target",
    [
        (CYCLE.replace(tzinfo=None), TARGET),
        (CYCLE, TARGET + timedelta(minutes=1)),
        (TARGET + timedelta(hours=1), TARGET),
    ],
)
def test_invalid_reference_times_are_rejected(cycle, target):
    with pytest.raises(ValueError, match="UTC|exact hours|after"):
        normalize_shadow_temperature({4: frame()}, model="RAP", cycle=cycle, target=target)
