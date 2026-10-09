"""An expired native cache must not break an immutable saved forecast."""

from __future__ import annotations

import json

import pytest

from mesoforge.application import baseline_snapshot as baselines
from mesoforge.application import guidance_retention as retention
from mesoforge.application.issuance_encoding import decode_issuance, encode_issuance
from mesoforge.contracts.serialization import canonical_json_bytes
from tests.unit.application.test_baseline_snapshot import (
    LOCATIONS,
    TARGET,
    forbid_location_calculation,
)
from tests.unit.application.test_baseline_snapshot import (
    baseline_case as baseline_case,
)
from tests.unit.application.test_guidance_retention import (
    COUNTS,
    NOW,
    _baseline,
    _generation,
    _receipt,
    _rows,
    _write,
)


def test_saved_baseline_and_issuance_are_exact_after_native_payload_expiration(
    baseline_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = baseline_case["guidance"].parent
    pointer = baseline_case["result"]["pointer"]
    source = baseline_case["prepared"][1]
    first, last = _generation(root, 22), _generation(root, 23)
    _baseline(root, first, "new-recovery")
    _baseline(root, last, "new-current")
    _write(
        root / "guidance/latest_complete.json",
        {
            "snapshot_id": last.name,
            "previous_snapshot_id": first.name,
        },
    )
    _write(
        root / "baseline/latest_baseline.json",
        {
            "baseline_snapshot_id": "new-current",
            "previous_baseline_snapshot_id": "new-recovery",
        },
    )
    pinned = baselines.load_baseline(baseline_case["baseline"], pointer=pointer)
    location = LOCATIONS[0]
    original = pinned.reference_view(TARGET).forecast(
        latitude=location["lat"], longitude=location["lon"]
    )
    saved = {"forecast": original, "issuance": {"id": "immutable-test-issuance"}}
    payload, digest = encode_issuance(saved)
    json_before = {
        path: path.read_bytes() for path in (root / "guidance/snapshots").rglob("*.json")
    }
    plan = retention.plan_retention(root, counts=COUNTS)
    row = _rows(plan)[source["snapshot_id"]]
    assert not plan["problems"], json.dumps(plan["problems"])
    assert row["removable_bytes"] > 0, json.dumps(row)
    assert row["action"] == "expire_payloads"
    receipt = _receipt(root, plan)
    outcome = retention.apply_retention(root, counts=COUNTS, backup_receipt=receipt, now=NOW)
    assert outcome["deleted_bytes"] > 0
    forbidden = forbid_location_calculation(monkeypatch)
    restored = baselines.load_baseline(baseline_case["baseline"], pointer=pointer)
    result = restored.reference_view(TARGET).forecast(
        latitude=location["lat"], longitude=location["lon"]
    )
    assert canonical_json_bytes(result) == canonical_json_bytes(original)
    assert canonical_json_bytes(
        decode_issuance(payload, expected_logical_digest=digest)
    ) == canonical_json_bytes(saved)
    assert all(path.read_bytes() == content for path, content in json_before.items())
    assert all(not (root / item["path"]).exists() for item in plan["candidates"])
    assert "Expired sources" in outcome["native_replay"]
    forbidden.assert_not_called()
