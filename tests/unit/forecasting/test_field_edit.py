"""Local edit transactions preserve evidence, events, masks and exact replay."""

from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from mesoforge.application.local_surface_grid import extract_grid_point
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.forecasting.coherence import DEW_POINT, QPF, RH, TEMPERATURE
from mesoforge.forecasting.field_blend import FIELD_REGISTRY, field_edit_contract
from mesoforge.forecasting.field_edit import (
    FieldEditError,
    apply_edit,
    cell_identity,
    grid_values_digest,
    replay_edits,
    validate_edit_scope,
    validate_grid,
)
from mesoforge.forecasting.surface import relative_humidity_percent
from tests.unit.application.test_corrections import _forecast


@pytest.fixture
def grid():
    result = _forecast()["local_grid_baseline"]
    for cell in result["cells"]:
        for hour in cell["hours"]:
            end = datetime.fromisoformat(hour["valid_time"])
            hour["surface"]["fields"][QPF] = {
                "value": 2.0 if cell["is_forecast_point"] else 1.0,
                "unit": "kg/m^2",
                "interval_start": (end - timedelta(hours=1)).isoformat(),
                "interval_end": hour["valid_time"],
                "interval_closure": "left_open_right_closed",
                "temporal_semantics": "accumulation",
                "policy": "existing-qpf-policy",
                "missing_reasons": [],
                "weights": {"HRRR": 0.7, "GFS": 0.3},
            }
    return result


def proposal(grid, *, field=QPF, operation="scale", parameter=1.2, cells=None, taper=None):
    return {
        "field": field,
        "operation": operation,
        "cell_ids": cells
        or [cell_identity(c) for c in grid["cells"] if c["inside_editable_domain"]],
        "valid_times": [grid["cells"][0]["hours"][0]["valid_time"]],
        "parameters": {
            {"add": "delta", "scale": "factor", "smooth": "strength"}[operation]: parameter
        },
        "taper": taper,
        "rationale": "Fixture-only proposal; no claim of meteorological skill.",
        "evidence_refs": ["pinned-baseline"],
    }


def test_qpf_atomic_copy_on_write_provenance_events_point_and_offline_replay(grid):
    before = canonical_json_bytes(grid)
    changed, recipe = apply_edit(grid, proposal(grid))
    assert canonical_json_bytes(grid) == before
    assert len(recipe["changes"]) == 9
    for a, b in zip(grid["cells"], changed["cells"], strict=True):
        if a["context_only"]:
            assert a is b
        for old, new in zip(a["hours"], b["hours"], strict=True):
            assert old["surface"]["contributors"] is new["surface"]["contributors"]
            expected = deepcopy(old)
            if a["inside_editable_domain"] and old["horizon_hours"] == 1:
                expected["surface"]["fields"][QPF]["value"] *= 1.2
            assert expected == new
    center = changed["geometry"]["center"]
    point = extract_grid_point(changed, **center, copy_grid=False)
    assert point["hours"][0]["surface"]["fields"][QPF]["value"] == 2.4
    assert canonical_json_bytes(replay_edits(grid, [recipe])) == canonical_json_bytes(changed)
    assert all(not row["future_rules_enforced"] for row in recipe["coherence_reports"].values())
    assert any(
        "qpf_thunder" in row["relationships"] for row in recipe["coherence_reports"].values()
    )


def test_temperature_add_reuses_current_dew_rejection_and_rh_without_native_edit(grid):
    for cell in grid["cells"]:
        fields = cell["hours"][0]["surface"]["fields"]
        fields[DEW_POINT]["value"] = 286.0
        fields[RH]["value"] = relative_humidity_percent(
            temperature_k=fields[TEMPERATURE]["value"], dew_point_k=286.0
        )
    before = grid_values_digest(grid)
    # Cooling below the saved dew point would make Td/RH missing under current rules.
    # Dew point is inspect-only, so the edit is rejected rather than clamped or nulled.
    with pytest.raises(FieldEditError, match="missing under current"):
        apply_edit(grid, proposal(grid, field=TEMPERATURE, operation="add", parameter=-5))
    assert grid_values_digest(grid) == before
    changed, recipe = apply_edit(
        grid, proposal(grid, field=TEMPERATURE, operation="add", parameter=-3)
    )
    center = next(c for c in changed["cells"] if c["is_forecast_point"])
    fields = center["hours"][0]["surface"]["fields"]
    assert fields[TEMPERATURE]["value"] == 287.0
    assert fields[DEW_POINT]["value"] == 286.0
    assert fields[RH]["value"] == relative_humidity_percent(temperature_k=287, dew_point_k=286)
    assert recipe["changes"][0]["affected_values"].keys() == {TEMPERATURE, DEW_POINT, RH}
    validate_edit_scope(grid, changed, [recipe])
    warmer, _ = apply_edit(grid, proposal(grid, field=TEMPERATURE, operation="add", parameter=1))
    center = next(c for c in warmer["cells"] if c["is_forecast_point"])
    assert center["hours"][0]["surface"]["fields"][RH]["value"] == relative_humidity_percent(
        temperature_k=291, dew_point_k=286
    )


def test_taper_uses_existing_signed_boundary_distance_and_does_not_enlarge_domain(grid):
    changed, recipe = apply_edit(grid, proposal(grid, taper={"width_m": 6000}))
    assert len(recipe["changes"]) == 1
    assert recipe["changes"][0]["cell_id"] == "3:3"
    assert (
        next(c for c in changed["cells"] if c["is_forecast_point"])["hours"][0]["surface"][
            "fields"
        ][QPF]["value"]
        == 2.4
    )


def test_spatial_smoothing_single_pass_conserves_selected_present_node_sum(grid):
    selected = [c for c in grid["cells"] if c["inside_editable_domain"]]
    selected[0]["hours"][0]["surface"]["fields"][QPF]["value"] = None
    selected[1]["hours"][0]["surface"]["fields"][QPF]["value"] = 0.0

    def total(cells):
        return sum(
            c["hours"][0]["surface"]["fields"][QPF]["value"] or 0
            for c in cells
            if c["inside_editable_domain"]
        )

    changed, recipe = apply_edit(grid, proposal(grid, operation="smooth", parameter=1))
    assert total(changed["cells"]) == total(grid["cells"])
    assert recipe["missing_cell_hours_preserved"] == 1
    assert (
        next(c for c in changed["cells"] if cell_identity(c) == cell_identity(selected[0]))[
            "hours"
        ][0]["surface"]["fields"][QPF]["value"]
        is None
    )
    assert recipe["temporal_redistribution"] is False
    assert replay_edits(grid, [recipe]) == changed


@pytest.mark.parametrize(
    "mutation",
    [
        "context",
        "outside_time",
        "unknown_cell",
        "duplicate_cell",
        "unknown_key",
        "negative",
        "nan",
        "infinite",
        "bool",
        "temperature_bound",
        "wrong_unit",
        "wrong_interval",
        "naive_time",
        "empty_evidence",
        "empty_rationale",
        "scale_temperature",
        "inspect_only",
        "taper_width",
    ],
)
def test_invalid_edit_never_mutates_previous_checkpoint(grid, mutation):
    edit = proposal(grid)
    if mutation == "context":
        edit["cell_ids"] = ["0:0"]
    elif mutation == "outside_time":
        edit["valid_times"] = ["2020-01-01T00:00:00Z"]
    elif mutation == "unknown_cell":
        edit["cell_ids"] = ["999:999"]
    elif mutation == "duplicate_cell":
        edit["cell_ids"] *= 2
    elif mutation == "unknown_key":
        edit["shell"] = "whoami"
    elif mutation == "negative":
        edit.update(operation="add", parameters={"delta": -100})
    elif mutation in ("nan", "infinite", "bool"):
        edit["parameters"]["factor"] = {
            "nan": float("nan"),
            "infinite": float("inf"),
            "bool": True,
        }[mutation]
    elif mutation == "temperature_bound":
        edit = proposal(grid, field=TEMPERATURE, operation="add", parameter=100)
    elif mutation in ("wrong_unit", "wrong_interval"):
        center = next(c for c in grid["cells"] if c["is_forecast_point"])["hours"][0]["surface"][
            "fields"
        ][QPF]
        center["unit" if mutation == "wrong_unit" else "interval_start"] = (
            "inch" if mutation == "wrong_unit" else center["interval_end"]
        )
    elif mutation == "naive_time":
        edit["valid_times"] = ["2026-10-20T01:00:00"]
    elif mutation == "empty_evidence":
        edit["evidence_refs"] = []
    elif mutation == "empty_rationale":
        edit["rationale"] = ""
    elif mutation == "scale_temperature":
        edit["field"] = TEMPERATURE
    elif mutation == "inspect_only":
        edit["field"] = "wind_from_direction_10m"
    elif mutation == "taper_width":
        edit["taper"] = {"width_m": 0}
    before = canonical_json_bytes(grid)
    with pytest.raises(FieldEditError):
        apply_edit(grid, edit)
    assert canonical_json_bytes(grid) == before


def test_noop_inverse_identity_and_recipe_tamper_rejection(grid):
    with pytest.raises(FieldEditError, match="no-op"):
        apply_edit(grid, proposal(grid, parameter=1))
    changed, first = apply_edit(grid, proposal(grid, operation="add", parameter=1))
    inverse, second = apply_edit(changed, proposal(changed, operation="add", parameter=-1))
    assert grid_values_digest(inverse) == grid_values_digest(grid)
    assert replay_edits(grid, [first, second]) == grid
    tampered = deepcopy(first)
    tampered["changes"][0]["after"] += 1
    with pytest.raises(FieldEditError, match="retained deterministic recipe"):
        replay_edits(grid, [tampered])
    with pytest.raises(FieldEditError, match="parent values"):
        replay_edits(changed, [first])


@pytest.mark.parametrize(
    "field,operation,amount",
    [
        (TEMPERATURE, "add", 5.001),
        (TEMPERATURE, "add", -5.001),
        (QPF, "add", 10.001),
        (QPF, "add", -10.001),
        (QPF, "scale", 2.001),
        (QPF, "scale", -0.001),
        (QPF, "smooth", 1.001),
    ],
)
def test_editor_intervention_limits_do_not_change_baseline_science(grid, field, operation, amount):
    before = canonical_json_bytes(grid)
    with pytest.raises(FieldEditError, match="intervention limit"):
        apply_edit(grid, proposal(grid, field=field, operation=operation, parameter=amount))
    assert canonical_json_bytes(grid) == before


def test_canonical_field_registry_exposes_edit_safety_without_promoting_evidence(grid):
    assert field_edit_contract(TEMPERATURE) is FIELD_REGISTRY[TEMPERATURE].editing
    assert field_edit_contract(QPF).operations == ("add", "scale", "smooth")
    for field in (
        DEW_POINT,
        RH,
        "wind_10m",
        "probability_of_precipitation_1h",
        "cloud_area_fraction",
        "thunder",
        "precipitation_type",
        "visibility",
        "snowfall_amount",
    ):
        assert field_edit_contract(field).inspectable
        assert field_edit_contract(field).operations == ()
    assert validate_grid(grid)["cell_hours"] == 49 * 36


def test_validation_rejects_td_above_t_rh_drift_and_out_of_scope_changes(grid):
    changed, recipe = apply_edit(grid, proposal(grid))
    validate_edit_scope(grid, changed, [recipe])
    # A context-only cell or an unedited field changing is outside the recipe scope.
    for mutate in (
        lambda g: g["cells"][0]["hours"][0]["surface"]["fields"][QPF].update(value=3.0),
        lambda g: next(c for c in g["cells"] if c["is_forecast_point"])["hours"][0]["surface"][
            "fields"
        ][TEMPERATURE].update(value=291.0),
        lambda g: next(c for c in g["cells"] if c["is_forecast_point"])["hours"][0]["surface"][
            "contributors"
        ].update(injected={"fields": {}}),
    ):
        tampered = deepcopy(changed)
        mutate(tampered)
        with pytest.raises(FieldEditError):
            validate_edit_scope(grid, tampered, [recipe])
    with pytest.raises(FieldEditError):
        validate_edit_scope(grid, changed, [])
    broken = deepcopy(grid)
    fields = broken["cells"][0]["hours"][0]["surface"]["fields"]
    fields[DEW_POINT]["value"] = fields[TEMPERATURE]["value"] + 1
    with pytest.raises(FieldEditError, match="dew point exceeds"):
        validate_grid(broken)
    drift = deepcopy(grid)
    fields = drift["cells"][0]["hours"][0]["surface"]["fields"]
    fields[RH]["value"] = fields[RH]["value"] - 1
    with pytest.raises(FieldEditError, match="derivation"):
        validate_grid(drift)
