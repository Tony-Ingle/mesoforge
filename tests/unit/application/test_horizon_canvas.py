"""Declared forecast horizons survive background storage and read-only presentation."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from mesoforge.application.build_baseline import _availability, _references
from mesoforge.application.local_surface_grid import (
    SurfaceGridGeometry,
    build_local_surface_grid,
    extract_grid_point,
)
from mesoforge.application.prepared_snapshot import SnapshotError, coverage_for
from mesoforge.common.errors import IntegrityError
from mesoforge.common.horizon import FIVE_DAY_HORIZON, LEGACY_HORIZON
from mesoforge.forecasting.conditions import build_conditions_preview
from tests.unit.forecasting.test_conditions import LATITUDE, LONGITUDE, TARGET, _hour

REFERENCE = datetime.fromisoformat(TARGET)


def canvas():
    def column(*, latitude, longitude):
        return {
            "latitude": latitude,
            "longitude": longitude,
            "target_reference_time": TARGET,
            "forecast_horizon": FIVE_DAY_HORIZON.payload(),
            "data_kind": "synthetic_demonstration",
            "hours": [_hour(h) for h in FIVE_DAY_HORIZON.leads],
        }

    return build_local_surface_grid(
        latitude=LATITUDE,
        longitude=LONGITUDE,
        calculate_column=column,
        geometry=SurfaceGridGeometry(context_half_width_cells=2, editable_half_width_cells=1),
    )


def test_declared_canvas_retains_all_leads_and_exact_point_without_calculation():
    grid = canvas()
    counts = _availability(grid, REFERENCE)
    assert counts["air_temperature_2m"] == {"available": 25 * 120, "missing": 0}
    with patch(
        "mesoforge.forecasting.field_blend.FieldBlendEngine.blend_field",
        side_effect=AssertionError("readback cannot blend"),
    ):
        forecast = extract_grid_point(grid, latitude=LATITUDE, longitude=LONGITUDE)
        assert forecast["forecast_horizon"] == FIVE_DAY_HORIZON.payload()
        assert forecast["hours"] == next(
            c["hours"] for c in grid["cells"] if c["is_forecast_point"]
        )
        preview = build_conditions_preview(
            {
                "schema_version": "issued-forecast.v1",
                "issued_forecast_id": "a98ae7e6-8eec-4f1d-8f0e-b8f4c4632940",
                "issued_at": TARGET,
                "forecast": forecast,
            }
        )
    assert [h["horizon_hours"] for h in preview["center_point"]["hours"]] == list(
        FIVE_DAY_HORIZON.leads
    )
    assert preview["center_point"]["hours"][-1]["valid_time"] == (
        REFERENCE + timedelta(hours=120)
    ).isoformat().replace("+00:00", "Z")


def test_horizon_mismatch_and_truncated_canvas_cannot_masquerade_as_five_days():
    grid = canvas()
    grid["forecast_context"]["forecast_horizon"] = LEGACY_HORIZON.payload()
    with pytest.raises(ValueError, match="horizon"):
        extract_grid_point(grid, latitude=LATITUDE, longitude=LONGITUDE)
    grid["forecast_context"]["forecast_horizon"] = FIVE_DAY_HORIZON.payload()
    forecast = extract_grid_point(grid, latitude=LATITUDE, longitude=LONGITUDE)
    forecast["forecast_horizon"] = LEGACY_HORIZON.payload()
    with pytest.raises(IntegrityError, match="horizons"):
        build_conditions_preview(
            {
                "schema_version": "issued-forecast.v1",
                "issued_forecast_id": "a98ae7e6-8eec-4f1d-8f0e-b8f4c4632940",
                "forecast": forecast,
            }
        )
    grid["cells"][0]["hours"].pop()
    with pytest.raises(SnapshotError, match="incomplete"):
        _availability(grid, REFERENCE)


def test_background_reference_views_require_full_declared_window():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    times = [(start + timedelta(hours=h)).isoformat().replace("+00:00", "Z") for h in range(1, 123)]
    manifest = {
        "forecast_horizon": FIVE_DAY_HORIZON.payload(),
        "coverage": {
            "reference_time": start.isoformat(),
            "last_valid_time": times[-1],
            "usability_rule": {"required_complete": ["GFS"]},
        },
        "contributors": {
            "GFS": {
                "cycle": start.isoformat(),
                "valid_times": [
                    *times[:120],
                    (start + timedelta(hours=123)).isoformat().replace("+00:00", "Z"),
                ],
            },
            "NBM": {"products": {}},
        },
    }
    # This tests coverage machinery, not a new approval of GFS-only blend science.
    assert _references(manifest) == [start + timedelta(hours=h) for h in range(3)]
    assert coverage_for(manifest, start + timedelta(hours=2))["usable"]
    assert not coverage_for(manifest, start + timedelta(hours=3))["usable"]
    legacy = deepcopy(manifest)
    del legacy["forecast_horizon"]
    legacy["contributors"]["GFS"]["valid_times"] = times
    assert len(_references(legacy)) == 87


def test_grid_builder_rejects_columns_with_different_declared_horizons():
    center = {
        "target_reference_time": TARGET,
        "forecast_horizon": FIVE_DAY_HORIZON.payload(),
        "hours": [_hour(h) for h in FIVE_DAY_HORIZON.leads],
    }

    def column(*, latitude, longitude):
        result = deepcopy(center)
        if (latitude, longitude) != (LATITUDE, LONGITUDE):
            result["forecast_horizon"] = LEGACY_HORIZON.payload()
        return result

    with pytest.raises(ValueError, match="inconsistent forecast horizons"):
        build_local_surface_grid(
            latitude=LATITUDE,
            longitude=LONGITUDE,
            calculate_column=column,
        )
