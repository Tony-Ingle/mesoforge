"""Independent geometry/time/value checks for decoded native shadow preparation."""

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.alignment.station_frame import align_station_to_model
from mesoforge.application.prepared_shadow import normalize_shadow_temperature
from mesoforge.application.spatial_coverage import UnsupportedCoordinateError, bbox_in_grid
from mesoforge.catalog.domains import BoundingBox
from mesoforge.guidance.normalization import WindRotationError

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


def test_surface_winds_rotate_before_extraction_and_temperature_remains_identical():
    temperature = frame()
    baseline = normalize_shadow_temperature(
        {4: temperature}, model="RAP", cycle=CYCLE, target=TARGET
    )
    u, v = temperature.copy(deep=True), temperature.copy(deep=True)
    u.values[:], v.values[:] = 5.0, 0.0
    for component in (u, v):
        component.attrs.update(GRIB_units="m s**-1", GRIB_uvRelativeToGrid=1)
    result = normalize_shadow_temperature(
        {4: temperature},
        model="RAP",
        cycle=CYCLE,
        target=TARGET,
        decoded_surface={"eastward_wind_10m": {4: u}, "northward_wind_10m": {4: v}},
    )
    xr.testing.assert_identical(result.air_temperature_2m, baseline.air_temperature_2m)
    # Independent spherical initial-bearing formula between two native fixture
    # cells: a pure +x grid wind follows that bearing, not true due east.
    lat0, lat1 = np.deg2rad(temperature.latitude.values[0, :2])
    lon0, lon1 = np.deg2rad(temperature.longitude.values[0, :2])
    bearing = np.arctan2(
        np.sin(lon1 - lon0) * np.cos(lat1),
        np.cos(lat0) * np.sin(lat1) - np.sin(lat0) * np.cos(lat1) * np.cos(lon1 - lon0),
    )
    assert float(result.eastward_wind_10m[0, 0, 0]) == pytest.approx(5 * np.sin(bearing), abs=1e-9)
    assert float(result.northward_wind_10m[0, 0, 0]) == pytest.approx(5 * np.cos(bearing), abs=1e-9)
    assert result.attrs["wind_reference"] == "earth_relative"
    assert json.loads(result.attrs["wind_rotation_policy_json"])["4"] == "grid-to-earth-pyproj.v1"
    assert np.isnan(result.dew_point_temperature_2m).all()
    np.testing.assert_array_equal(u.values, 5.0)


@pytest.mark.parametrize("defect", ["units", "time", "wind_flag"])
def test_surface_metadata_mismatch_is_rejected(defect):
    u, v = frame(), frame()
    for component in (u, v):
        component.attrs.update(GRIB_units="m s**-1", GRIB_uvRelativeToGrid=0)
    if defect == "units":
        u.attrs["GRIB_units"] = "K"
    elif defect == "time":
        u = u.assign_coords(valid_time=u.valid_time.values + np.timedelta64(1, "h"))
    else:
        v.attrs["GRIB_uvRelativeToGrid"] = 1
    with pytest.raises((ValueError, WindRotationError)):
        normalize_shadow_temperature(
            {4: frame()},
            model="RAP",
            cycle=CYCLE,
            target=TARGET,
            decoded_surface={"eastward_wind_10m": {4: u}, "northward_wind_10m": {4: v}},
        )


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


@pytest.mark.parametrize("lead", [True, 3, 46])
def test_unrequested_or_noninteger_leads_are_rejected(lead):
    # Leads may reach the extended prepared window (hour 42), never beyond it.
    with pytest.raises(ValueError, match="target hours 1 through 42"):
        normalize_shadow_temperature({lead: frame()}, model="RAP", cycle=CYCLE, target=TARGET)


@pytest.mark.parametrize(
    "attribute,value,message",
    [
        ("GRIB_units", "degC", "use K"),
        ("GRIB_iScansNegatively", 1, "scanning"),
        ("GRIB_radius", 6371000, "6371229"),
        ("GRIB_gridType", "regular_ll", "latitude/longitude"),
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


def geographic_frame(lead=4, *, wrapped=False):
    """Small regular geographic fixture; values are not acquired model guidance."""
    longitude = np.array([180.0, 270.0, 0.0, 90.0]) if wrapped else 264.5 + 0.25 * np.arange(19)
    latitude = 47.0 - 0.25 * np.arange(17)
    lon, lat = np.meshgrid((longitude + 180) % 360 - 180, latitude)
    return xr.DataArray(
        270.0 + lead + 0.5 * (lat - 45) + 0.2 * (lon + 93),
        dims=("latitude", "longitude"),
        coords={
            "latitude": latitude,
            "longitude": longitude,
            "time": np.datetime64(CYCLE.replace(tzinfo=None), "ns"),
            "step": np.timedelta64(lead, "h"),
            "valid_time": np.datetime64((CYCLE + timedelta(hours=lead)).replace(tzinfo=None), "ns"),
        },
        attrs={
            "GRIB_units": "K",
            "GRIB_gridType": "regular_ll",
            "GRIB_iScansNegatively": 0,
            "GRIB_jScansPositively": 0,
            "GRIB_jPointsAreConsecutive": 0,
            "GRIB_alternativeRowScanning": 0,
            "GRIB_shapeOfTheEarth": 6,
            "GRIB_radius": 6371229,
            "GRIB_Ni": len(longitude),
            "GRIB_Nj": len(latitude),
            "GRIB_iDirectionIncrementInDegrees": 90.0 if wrapped else 0.25,
            "GRIB_jDirectionIncrementInDegrees": 0.25,
            "GRIB_latitudeOfFirstGridPointInDegrees": latitude[0],
            "GRIB_latitudeOfLastGridPointInDegrees": latitude[-1],
            "GRIB_longitudeOfFirstGridPointInDegrees": longitude[0],
            "GRIB_longitudeOfLastGridPointInDegrees": longitude[-1],
        },
    )


def test_ifs_surface_fields_keep_native_times_and_explicit_instantaneous_gust_gap():
    decoded = {6: geographic_frame(6), 9: geographic_frame(9)}
    surface = {
        name: {} for name in ("dew_point_temperature_2m", "eastward_wind_10m", "northward_wind_10m")
    }
    for lead, temperature in decoded.items():
        for variable, values in (
            ("dew_point_temperature_2m", 270.0),
            ("eastward_wind_10m", 3.0),
            ("northward_wind_10m", 4.0),
        ):
            field = temperature.copy(deep=True)
            field.values[:] = values
            field.attrs["GRIB_units"] = "K" if variable == "dew_point_temperature_2m" else "m s**-1"
            field.attrs["GRIB_uvRelativeToGrid"] = 0
            surface[variable][lead] = field
    actual = normalize_shadow_temperature(
        decoded, model="IFS", cycle=CYCLE, target=TARGET, decoded_surface=surface
    )
    assert list(actual.source_lead_time.values / np.timedelta64(1, "h")) == [6, 9]
    np.testing.assert_array_equal(actual.eastward_wind_10m, 3.0)
    np.testing.assert_array_equal(actual.northward_wind_10m, 4.0)
    assert np.isnan(actual.wind_gust_10m).all()
    reasons = json.loads(actual.attrs["field_missing_reasons_json"])
    assert "interval maximum" in reasons["wind_gust_10m"]["6"][0]


def test_sparse_geographic_native_steps_align_by_valid_time_without_hourly_interpolation():
    target = CYCLE + timedelta(hours=4)
    decoded = {lead: geographic_frame(lead) for lead in range(6, 40, 3)}
    originals = {lead: field.copy(deep=True) for lead, field in decoded.items()}
    result = normalize_shadow_temperature(
        decoded, model="SYNTH_GEOGRAPHIC", cycle=CYCLE, target=target
    )
    crs = pyproj.CRS.from_wkt(result.attrs["crs_wkt2"])
    assert crs.is_geographic and crs.ellipsoid.semi_major_metre == 6371229.0
    assert crs.ellipsoid.semi_minor_metre == 6371229.0
    assert result.attrs["source_earth_shape"] == 6
    assert result.attrs["source_earth_radius_m"] == 6371229.0
    assert result.air_temperature_2m.attrs == {"unit_id": "K", "units": "K"}
    assert result.sizes["source_lead_time"] == 12  # No fabricated hourly source slots.
    np.testing.assert_array_equal(result.y.values, decoded[6].latitude.values)
    np.testing.assert_array_equal(result.air_temperature_2m.values[0], decoded[6].values)
    aligned = align_station_to_model(
        result,
        crs=crs,
        station_latitude=45.8,
        station_longitude=-93.1,
        canonical_variable_id="air_temperature_2m",
        target_horizon_hours=tuple(range(1, 37)),
        target_reference_time=np.datetime64(target.replace(tzinfo=None), "ns"),
    )
    assert set(aligned) == set(range(2, 36, 3))  # Source cycle + lead, not target hour modulo 3.
    for horizon, value in aligned.items():
        assert value.source_lead_hour == horizon + 4
        assert value.value == pytest.approx(274.38 + horizon, abs=1e-10)
    for lead, field in decoded.items():
        xr.testing.assert_identical(field, originals[lead])


def test_wrapped_geographic_longitude_reorders_values_with_their_original_cells():
    field = geographic_frame(wrapped=True)
    result = normalize_shadow_temperature(
        {4: field}, model="SYNTH_GEOGRAPHIC", cycle=CYCLE, target=TARGET
    )
    np.testing.assert_array_equal(result.x.values, [-180.0, -90.0, 0.0, 90.0])
    np.testing.assert_array_equal(result.air_temperature_2m.values[0], field.values)
    shifted = field.roll(longitude=2, roll_coords=True)
    shifted.attrs.update(
        GRIB_longitudeOfFirstGridPointInDegrees=0.0,
        GRIB_longitudeOfLastGridPointInDegrees=270.0,
    )
    reordered = normalize_shadow_temperature(
        {4: shifted}, model="SYNTH_GEOGRAPHIC", cycle=CYCLE, target=TARGET
    )
    xr.testing.assert_identical(reordered, result)


def test_geographic_footprint_retains_descending_rows_native_cells_and_missing_values():
    field = geographic_frame()
    field.values[0, 0] = np.nan
    area = BoundingBox(south=45.79, north=45.81, west=-93.11, east=-93.09)
    full = normalize_shadow_temperature(
        {4: field}, model="SYNTH_GEOGRAPHIC", cycle=CYCLE, target=TARGET
    )
    subset = normalize_shadow_temperature(
        {4: field}, model="SYNTH_GEOGRAPHIC", cycle=CYCLE, target=TARGET, area=area
    )
    assert np.isnan(full.air_temperature_2m.values[0, 0, 0])
    assert subset.sizes["x"] < full.sizes["x"] and subset.sizes["y"] < full.sizes["y"]
    assert min(subset.sizes["x"], subset.sizes["y"]) >= 4
    assert np.all(np.diff(subset.y.values) < 0)
    assert bbox_in_grid(
        area, pyproj.CRS.from_wkt(subset.attrs["crs_wkt2"]), subset.x.values, subset.y.values
    )
    xr.testing.assert_equal(
        subset.air_temperature_2m, full.air_temperature_2m.sel(x=subset.x, y=subset.y)
    )
    for name in ("source_x_min", "source_x_max", "source_y_min", "source_y_max"):
        assert subset.attrs[name] == full.attrs[name]


@pytest.mark.parametrize(
    "attribute,value,message",
    [
        ("GRIB_shapeOfTheEarth", 5, "spherical-earth"),
        ("GRIB_radius", 0.0, "spherical-earth"),
        ("GRIB_Ni", 18, "dimensions"),
        ("GRIB_iDirectionIncrementInDegrees", 0.5, "increments"),
        ("GRIB_jScansPositively", 1, "increments"),
        ("GRIB_longitudeOfLastGridPointInDegrees", 268.0, "endpoints"),
        ("GRIB_jPointsAreConsecutive", 1, "scanning"),
    ],
)
def test_geographic_metadata_must_describe_the_decoded_native_cells(attribute, value, message):
    field = geographic_frame()
    field.attrs[attribute] = value
    with pytest.raises(ValueError, match=message):
        normalize_shadow_temperature(
            {4: field}, model="SYNTH_GEOGRAPHIC", cycle=CYCLE, target=TARGET
        )


def test_geographic_earth_metadata_and_grid_cannot_change_between_native_steps():
    field = geographic_frame(6)
    field.attrs.update(GRIB_shapeOfTheEarth=1, GRIB_radius=6371000)
    single = normalize_shadow_temperature(
        {6: field}, model="SYNTH_GEOGRAPHIC", cycle=CYCLE, target=TARGET
    )
    assert pyproj.CRS.from_wkt(single.attrs["crs_wkt2"]).ellipsoid.semi_major_metre == 6371000
    with pytest.raises(ValueError, match="grid changes"):
        normalize_shadow_temperature(
            {4: geographic_frame(4), 6: field}, model="SYNTH_GEOGRAPHIC", cycle=CYCLE, target=TARGET
        )
