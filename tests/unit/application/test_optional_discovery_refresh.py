"""Optional discovery shortfalls survive publication and later shadow recovery."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from mesoforge.application import prepared_snapshot as snapshots
from mesoforge.application.refresh_guidance import refresh_guidance
from mesoforge.common.identifiers import Digest
from tests.unit.application.test_batch_forecast import FIRST, write_config
from tests.unit.application.test_prepared_snapshot import (
    PREPARED_HOURS,
    TARGET,
    fixture_steps,
)
from tests.unit.application.test_prepared_snapshot import forbid_network as forbid_network


def test_missing_both_shadow_discoveries_publish_and_later_refresh_restores_them(tmp_path):
    steps = fixture_steps(hours=PREPARED_HOURS)

    def prepare_without_shadows(locations, selection_path, directory):
        # Use the retained-format fixture; the selected-preparation tests separately
        # prove that missing discovery never invokes a shadow adapter or creates its files.
        preparation = steps.prepare(locations, selection_path, directory)
        evidence = preparation["current_model_set"]
        selection = evidence["selection"]
        selection["optional_shadow_models"] = ["IFS", "RAP"]
        selection["shadow_discovery_shortfalls"] = {}
        preparation["shadow_shortfalls"] = {}
        for model in ("RAP", "IFS"):
            reason = f"{model}: fixture provider could not prove a usable cycle"
            shortfall = {
                "stage": "discovery",
                "status": "unavailable",
                "reason": reason,
                "decision_time": selection["decision_time"],
            }
            selection["shadow_discovery_shortfalls"][model] = shortfall
            del selection["selected_cycles"][model]
            selection["models"][model] = {
                "status": "unavailable",
                "selected_cycle": None,
                "reason": reason,
                "candidates": [],
            }
            missing = {str(hour): reason for hour in selection["horizon_hours"]}
            preparation["shadows"][model] = {
                "selected_cycle": None,
                "supported_hours": [],
                "missing_hours": missing,
                "retained_raw_bytes": 0,
                "discovery_shortfall": shortfall,
            }
            preparation["shadow_shortfalls"][model] = {
                **shortfall,
                "expected_hours": selection["horizon_hours"],
                "supported_hours": [],
                "missing_hours": missing,
                "object_failures": [],
            }
            del preparation["shadow_directories"][model]
        selection_bytes = json.dumps(selection).encode()
        selection_path.write_bytes(selection_bytes)
        evidence["selection_sha256"] = str(Digest.of_bytes(selection_bytes))
        control_path = Path(preparation["directory"]) / "manifest.json"
        control = json.loads(control_path.read_bytes())
        control["current_model_set"] = evidence
        control_path.write_text(json.dumps(control), encoding="utf-8")
        (directory / "preparation.json").write_text(json.dumps(preparation), encoding="utf-8")
        return preparation

    root = tmp_path / "snapshots"
    config = write_config(tmp_path, [FIRST])
    first = refresh_guidance(config, root, steps=replace(steps, prepare=prepare_without_shadows))
    assert first["status"] == "published", first
    _, manifest, first_directory = snapshots.resolve_latest_complete(root)
    first_bytes = (first_directory / "snapshot.json").read_bytes()
    assert manifest["coverage"]["prepared_hours"] == 42
    assert manifest["completeness"]["required_deterministic"] is True
    assert manifest["completeness"]["shadows"] == {"RAP": "unavailable", "IFS": "unavailable"}
    for model in ("RAP", "IFS"):
        contributor = manifest["contributors"][model]
        assert contributor["usage"] == "shadow_evidence"
        assert contributor["cycle"] is None and contributor["directory"] is None
        assert contributor["valid_times"] == []
        assert len(contributor["missing_valid_times"]) == 42
        assert contributor["discovery_shortfall"]["stage"] == "discovery"
    preparation = snapshots.verify_prepared_run(manifest)
    view = snapshots.load_preparation(preparation).reference_view(TARGET)
    column = view.point_column(latitude=FIRST["lat"], longitude=FIRST["lon"])
    assert len(column["hours"]) == 36
    for hour in column["hours"]:
        assert hour["temperature"]["value"] is not None
        assert {source["model"] for source in hour["sources"]} == {"HRRR", "GFS"}
        assert all(source["temperature"]["value"] is None for source in hour["shadow_sources"])

    second = refresh_guidance(config, root, steps=steps)
    assert second["status"] == "published", second
    _, recovered, _ = snapshots.resolve_latest_complete(root)
    assert recovered["snapshot_id"] != manifest["snapshot_id"]
    assert recovered["completeness"]["shadows"] == {"RAP": "complete", "IFS": "complete"}
    recovered_preparation = snapshots.verify_prepared_run(recovered)
    recovered_column = (
        snapshots.load_preparation(recovered_preparation)
        .reference_view(TARGET)
        .point_column(latitude=FIRST["lat"], longitude=FIRST["lon"])
    )
    assert [h["temperature"] for h in recovered_column["hours"]] == [
        h["temperature"] for h in column["hours"]
    ]
    assert (first_directory / "snapshot.json").read_bytes() == first_bytes
