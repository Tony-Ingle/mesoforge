"""Inspection limits describe omitted evidence without truncating scientific rows."""

import pytest

from mesoforge.application.forecast_desk_context import build_context, inspect_evidence
from mesoforge.contracts.serialization import canonical_json_bytes, canonical_json_digest
from mesoforge.forecasting.coherence import QPF
from tests.unit.application.test_forecast_desk import forecast


def test_inspection_explains_row_limit_separately_from_byte_limit():
    parent = forecast()
    context = build_context(parent)
    contract = context["inspection_contract"]
    assert contract["allowed_regions"] == ["point", "editable", "context"]
    assert contract["maximum_requested_rows"] == 144
    request = {
        "tool": "inspect_contributors",
        "field": QPF,
        "valid_times": [context["valid_times"][0]],
        "region": "context",
        "max_rows": 2,
    }
    result = inspect_evidence(parent, context, request, max_bytes=16384)
    assert result["returned_rows"] == len(result["rows"]) == 2
    assert result["total_matching_rows"] == 49
    assert result["truncation_reasons"] == ["row_limit"]
    assert "narrower_request_hint" not in result
    assert result["max_output_bytes"] == 16384

    bounded = inspect_evidence(parent, context, request, max_bytes=1300)
    assert bounded["truncation_reasons"] == ["row_limit", "byte_budget"]
    assert bounded["returned_rows"] == len(bounded["rows"]) < 2
    assert bounded["rows"] == result["rows"][: bounded["returned_rows"]]
    assert "one valid time" in bounded["narrower_request_hint"]
    assert bounded["max_output_bytes"] == 1300
    assert len(canonical_json_bytes(bounded)) <= 1300
    assert bounded["evidence_ref"] == str(
        canonical_json_digest({k: v for k, v in bounded.items() if k != "evidence_ref"})
    )


def test_single_row_byte_limitation_does_not_misreport_requested_row_limit():
    parent = forecast()
    context = build_context(parent)
    request = {
        "tool": "inspect_contributors",
        "field": QPF,
        "valid_times": [context["valid_times"][0]],
        "region": "point",
        "max_rows": 1,
    }
    full = inspect_evidence(parent, context, request, max_bytes=16384)
    cap = len(canonical_json_bytes(full)) - 3
    result = inspect_evidence(parent, context, request, max_bytes=cap)
    assert result["returned_rows"] == 0
    assert result["status"] == "output_budget_exceeded"
    assert result["truncation_reasons"] == ["byte_budget"]
    assert result["total_matching_rows"] == 1
    assert len(canonical_json_bytes(result)) <= cap


def test_evidence_reference_preserves_the_supplied_validated_checkpoint_identity():
    parent = forecast()
    context = build_context(parent)
    request = {
        "tool": "inspect_baseline",
        "field": QPF,
        "valid_times": [context["valid_times"][0]],
        "region": "point",
        "max_rows": 1,
    }
    original = inspect_evidence(parent, context, request, max_bytes=4096)
    assert original["current_state_digest"] is None
    a = inspect_evidence(
        parent, context, request, max_bytes=4096, state_digest="sha256:" + "a" * 64
    )
    b = inspect_evidence(
        parent, context, request, max_bytes=4096, state_digest="sha256:" + "b" * 64
    )
    assert a["rows"] == b["rows"]
    assert a["current_state_digest"] != b["current_state_digest"]
    assert a["evidence_ref"] != b["evidence_ref"]
    assert len(canonical_json_bytes(a)) <= 4096
    with pytest.raises(ValueError, match="Digest must"):
        inspect_evidence(parent, context, request, max_bytes=4096, state_digest="unproven")


def _point_hour(parent, index=0):
    cell = next(c for c in parent["local_grid_baseline"]["cells"] if c["is_forecast_point"])
    return cell, cell["hours"][index]


def test_cell_selector_summary_and_disagreement_are_distinct_bounded_tools():
    parent = forecast()
    cell, hour = _point_hour(parent)
    hour["surface"]["contributors"] = {
        "HRRR": {"fields": {QPF: {**hour["surface"]["fields"][QPF], "value": 1.0}}},
        "GFS": {"fields": {QPF: {**hour["surface"]["fields"][QPF], "value": 4.0}}},
        # A different accumulation event is never pooled with the hourly members.
        "NBM": {
            "fields": {
                QPF: {
                    **hour["surface"]["fields"][QPF],
                    "value": 9.0,
                    "interval_start": "2000-01-01T00:00:00+00:00",
                }
            }
        },
    }
    parent["hours"][0] = hour  # The issued point series is the saved grid center.
    context = build_context(parent)
    point_id = f"{cell['x_index']}:{cell['y_index']}"
    base = {"field": QPF, "valid_times": [hour["valid_time"]], "max_rows": 144}
    selected = inspect_evidence(
        parent,
        context,
        {**base, "tool": "inspect_baseline", "region": "context", "cell_ids": [point_id, "0:0"]},
        max_bytes=16384,
    )
    assert [row["cell_id"] for row in selected["rows"]] == ["0:0", point_id]
    disagreement = inspect_evidence(
        parent,
        context,
        {**base, "tool": "inspect_disagreement", "region": "point"},
        max_bytes=16384,
    )
    groups = disagreement["rows"][0]["comparable_groups"]
    assert len(groups) == 1 and groups[0]["spread"] == 3.0
    assert set(groups[0]["members"]) == {"HRRR", "GFS"}
    summary = inspect_evidence(
        parent,
        context,
        {**base, "tool": "summarize_field", "region": "editable"},
        max_bytes=16384,
    )
    row = summary["rows"][0]
    assert row["selected_cells"] == 9 and row["numerical"] == 9
    assert row["minimum"] == row["maximum"] == 2.0 and row["point_value"] == 2.0
    assert context["fields"][QPF]["maximum_comparable_contributor_spread"] == 3.0
    assert context["fields"][QPF]["spread_peak"] == {
        "valid_time": hour["valid_time"],
        "cell_id": point_id,
    }
    timing = context["fields"][QPF]["point_contributor_timing"]["contributors"]
    assert set(timing) == {"HRRR", "GFS"}  # NBM's other event is not the hourly amount.
    assert timing["GFS"]["peak"] == {"value": 4.0, "valid_time": hour["valid_time"]}
    for bad in (["9:9"], ["0:0", "0:0"], []):
        with pytest.raises(ValueError, match="cell selection"):
            inspect_evidence(
                parent,
                context,
                {**base, "tool": "inspect_baseline", "region": "context", "cell_ids": bad},
                max_bytes=16384,
            )


def test_no_comparable_pair_is_not_reported_as_agreement_and_direction_is_circular():
    from mesoforge.application.forecast_desk_context import minimum_arc

    parent = forecast()
    context = build_context(parent)
    temperature = context["fields"]["air_temperature_2m"]
    # One temperature contributor per cell-hour: no pair, so no spread claim.
    assert temperature["maximum_comparable_contributor_spread"] is None
    assert temperature["spread_status"] == "no_comparable_pairs"
    assert temperature["comparable_contributor_cell_hours"] == 0
    assert minimum_arc([350.0, 10.0]) == (350.0, 10.0, 20.0)
    assert minimum_arc([90.0]) == (90.0, 90.0, 0.0)
    center = next(c for c in parent["local_grid_baseline"]["cells"] if c["is_forecast_point"])
    for cell in parent["local_grid_baseline"]["cells"]:
        for index, hour in enumerate(cell["hours"]):
            hour["surface"]["fields"]["wind_from_direction_10m"] = {
                "value": 350.0 if index % 2 else 10.0,
                "unit": "degree",
            }
    parent["hours"] = center["hours"]
    direction = build_context(parent)["fields"]["wind_from_direction_10m"]
    assert direction["range"] == {"minimum_containing_arc": [350.0, 10.0], "arc_degrees": 20.0}
    assert direction["spatial_maximum"] is None
    assert direction["point_largest_hourly_direction_change"]["degrees"] == 20.0


def test_nested_acquisition_provenance_and_urls_never_reach_the_provider():
    from mesoforge.application.forecast_desk_context import field_view

    view = field_view(
        {
            "value": 1.0,
            "profile_metadata": {
                "level": "surface",
                "provenance": {"source_grib_url": "https://example.invalid/raw.grib2"},
                "raw_file": "raw/f001.grib2",
                "index_file": "raw/f001.idx",
                "etag": "abc",
            },
            "missing_reasons": ["fetch failed for https://example.invalid/private?sig=x"],
        }
    )
    text = canonical_json_bytes(view).decode()
    assert "example.invalid" not in text and "raw/f001" not in text and "abc" not in text
    assert view["profile_metadata"]["level"] == "surface"
    assert view["profile_metadata"]["private_acquisition_detail_omitted"] is True
    assert "[url omitted]" in view["missing_reasons"][0]


def test_context_overflow_is_an_explicit_budget_outcome():
    from dataclasses import replace

    from mesoforge.application.forecast_desk import run_forecast_desk
    from mesoforge.application.forecast_desk_context import DeskContextBudgetError
    from mesoforge.contracts.forecast_desk import DeskConfig
    from tests.unit.application.test_forecast_desk import NO_EDIT, Provider

    parent = forecast()
    with pytest.raises(DeskContextBudgetError):
        build_context(parent, max_bytes=2048)
    provider = Provider([NO_EDIT])
    config = replace(DeskConfig(), max_context_bytes=4096, max_total_tokens=300000)
    final, report = run_forecast_desk(parent, provider=provider, config=config)
    assert final is parent and provider.requests == []
    assert report["completion_reason"] == "context_budget_exceeded"
    assert report["validation"]["status"] == "valid"


def test_verification_history_status_reflects_the_pinned_evidence_row():
    parent = forecast()
    context = build_context(parent)
    result = inspect_evidence(
        parent,
        context,
        {
            "tool": "inspect_verification_history",
            "field": QPF,
            "valid_times": [context["valid_times"][0]],
            "region": "point",
            "max_rows": 1,
        },
        max_bytes=4096,
    )
    assert result["status"] == "unavailable"
    assert result["rows"] == [context["verification"]]
