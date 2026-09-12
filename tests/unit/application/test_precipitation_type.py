"""Independent categorical extraction and provisional agreement checks."""

from copy import deepcopy

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application.precipitation_type import TYPES, TypeView, extract_precipitation_type
from mesoforge.guidance.sources.precipitation_type import SOURCES

VALID = "2026-09-11T19:00:00Z"


def type_view(model="HRRR", types=("rain",), *, values=None, valid=VALID):
    metadata = deepcopy(SOURCES[model])
    native = values if values is not None else {name: float(name in types) for name in TYPES}
    data = xr.Dataset(
        {
            name: (
                ("event", "y", "x"),
                np.full((1, 2, 2), value),
                {"units": metadata["native_unit"]},
            )
            for name, value in native.items()
        },
        coords={"event": [0], "x": [-94.0, -93.0], "y": [44.0, 45.0]},
    )
    event = {
        **metadata,
        "source_cycle": "2026-09-11T12:00:00Z",
        "source_lead_hours": 7,
        "valid_time": valid,
        "temporal_semantics": "instantaneous",
        "interval_start": None,
        "interval_end": None,
        "missing_reasons": [],
        "provenance": {"raw_sha256": "a" * 64},
    }
    return TypeView(
        data,
        pyproj.CRS.from_epsg(4326),
        {"model": model, "events": [event], "manifest_sha256": "b" * 64},
    )


def run(views, **kwargs):
    return extract_precipitation_type(
        views, latitude=44.4, longitude=-93.6, valid_time=kwargs.get("valid", VALID)
    )


@pytest.mark.parametrize(
    "left,right,expected",
    [
        (("rain",), ("rain",), "rain"),
        (("snow",), ("snow",), "snow"),
        (("freezing_rain",), ("freezing_rain",), "freezing_rain"),
        (("ice_pellets",), ("ice_pellets",), "ice_pellets"),
        (("rain", "snow"), ("snow", "rain"), "mixed"),
        (("rain",), ("snow",), "ambiguous"),
        ((), ("rain",), "ambiguous"),
        ((), (), "unknown"),
    ],
)
def test_interim_agreement_never_votes_or_infers_precipitation(left, right, expected):
    result = run([type_view(types=left), type_view("GFS", right)])
    assert result["field"]["value"] == expected
    assert result["field"]["supported_types"] == sorted(set(left) | set(right))
    assert result["field"]["policy"]["status"] == "interim_not_verified_or_optimized"
    assert result["field"]["contributor_disagreement"] == (set(left) != set(right))


def test_native_nearest_cell_is_not_an_average_and_ties_are_deterministic():
    a, b = type_view(), type_view("GFS")
    for view in (a, b):
        view.dataset.rain.values[0] = [[0, 1], [1, 1]]
        view.dataset.snow.values[0] = [[1, 0], [0, 0]]
    result = run([a, b])
    assert result["field"]["value"] == "snow"
    row = result["contributors"][0]
    assert row["native_values"]["rain"] == 0 and row["native_values"]["snow"] == 1
    assert row["provenance"]["raw_sha256"] == "a" * 64
    tied = extract_precipitation_type([a, b], latitude=44.5, longitude=-93.5, valid_time=VALID)
    assert tied["field"]["value"] == "snow"
    assert row["spatial_extraction"]["method"] == "nearest_native_cell"


@pytest.mark.parametrize("native,expected", [(np.nan, "unknown"), (0.5, "unknown"), (2, "unknown")])
def test_missing_or_nonbinary_flag_cannot_be_used_in_baseline(native, expected):
    view = type_view()
    view.dataset.rain.values[:] = native
    result = run([view, type_view("GFS")])
    assert result["field"]["value"] == expected
    assert result["contributors"][0]["missing_reasons"]


def test_missing_source_and_missing_valid_time_do_not_use_a_fallback():
    assert run([])["field"]["value"] == "unavailable"
    assert run([type_view()])["field"]["value"] == "unknown"
    result = run([type_view(), type_view("GFS")], valid="2026-09-11T20:00:00Z")
    assert result["field"]["value"] == "unavailable"
    assert "no temporal filling" in result["contributors"][0]["missing_reasons"][0]


@pytest.mark.parametrize(
    "code,status,types",
    [
        (0, "no_type_classified", []),
        (7, "classified", ["rain", "snow"]),
        (6, "classified", ["wet_snow"]),
        (12, "classified", ["freezing_drizzle"]),
        (9, "unknown", []),
        (255, "unavailable", []),
    ],
)
def test_native_codes_preserve_mixture_unknown_and_missing(code, status, types):
    result = run([type_view("IFS", values={"native_code": code})])
    row = next(row for row in result["contributors"] if row["model"] == "IFS")
    assert row["status"] == status and row["supported_types"] == types
    assert row["native_values"] == {"native_code": code}
    assert row["active_weight"] == 0


def test_shadow_disagreement_and_conditional_probabilities_do_not_change_active_type():
    views = [
        type_view(),
        type_view("GFS"),
        type_view("RAP", ("snow",)),
        type_view("NBM", values={"rain": 10, "snow": 90, "freezing_rain": 0, "ice_pellets": 0}),
    ]
    before = [v.dataset.copy(deep=True) for v in views]
    one = run(views)
    two = run(views)
    assert one == two
    assert one["field"]["value"] == "rain" and one["field"]["contributor_disagreement"]
    nbm = one["contributors"][-1]
    assert nbm["status"] == "probabilistic_evidence" and nbm["supported_types"] == []
    assert nbm["conditional_type_fractions"]["snow"] == 0.9
    for old, view in zip(before, views, strict=True):
        xr.testing.assert_identical(old, view.dataset)


def test_average_product_wrong_units_and_outside_coverage_are_rejected():
    view = type_view()
    view.manifest["events"][0]["temporal_semantics"] = "average"
    assert run([view])["contributors"][0]["status"] == "unavailable"
    view = type_view()
    view.dataset.rain.attrs["units"] = "percent"
    with pytest.raises(ValueError, match="units"):
        run([view])
    view = type_view()
    view.dataset.coords["x"] = [-101.0, -100.0]
    row = run([view])["contributors"][0]
    assert row["status"] == "unavailable" and "coverage" in row["missing_reasons"][0]
