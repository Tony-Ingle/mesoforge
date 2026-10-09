"""Native 120-hour science survives background storage, issuance and presentation.

Only provider/prepared-manifest loading is replaced by synthetic retained input.
The background field engine, coherence, codec, pointer, readback and PDF are real.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from pypdf import PdfReader

from mesoforge.application import baseline_snapshot as baselines
from mesoforge.application import build_baseline as background
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.local_surface_grid import extract_grid_point
from mesoforge.common.horizon import FIVE_DAY_HORIZON
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.forecasting.coherence import CoherenceEngine
from mesoforge.forecasting.field_blend import FieldBlendEngine
from mesoforge.forecasting.field_edit import apply_edit, replay_edits, validate_grid
from mesoforge.forecasting.provisional_policy import PROVISIONAL_MULTIMODEL_POLICY
from mesoforge.presentation.forecast_document import build_forecast_document
from mesoforge.presentation.forecast_pdf import render_forecast_pdf
from tests.support.in_memory_uow import InMemoryObjectStore, InMemoryUnitOfWorkFactory
from tests.unit.application.test_native_surface_inputs import native_prepared_120
from tests.unit.forecasting.test_field_edit import proposal
from tests.unit.forecasting.test_surface import configuration as configuration


def test_native_background_to_compact_issuance_and_product(configuration, tmp_path, monkeypatch):
    prepared = native_prepared_120(configuration, spatial_variation=True)
    reference = datetime(2026, 10, 8, 6, tzinfo=UTC)
    location = {
        "name": "Native fixture",
        "lat": 46.0,
        "lon": -93.0,
        "display_timezone": "America/Chicago",
    }
    retained = tmp_path / "prepared"
    retained.mkdir()
    manifest = {
        "snapshot_id": "synthetic-native-120",
        "forecast_horizon": FIVE_DAY_HORIZON.payload(),
        "completed_at": reference.isoformat(),
        "coverage": {
            "reference_time": reference.isoformat(),
            "last_valid_time": (reference + timedelta(hours=120))
            .isoformat()
            .replace("+00:00", "Z"),
        },
        "contributors": {
            model: {
                "cycle": "2026-10-08T00:00:00Z",
                "valid_times": [
                    str(value).split(".")[0] + "Z" for value in ds["source_valid_time"].values
                ],
                **({"products": {}} if model == "NBM" else {}),
            }
            for model, ds in prepared._guidance.items()
        },
        "field_policies": {"policy_family": PROVISIONAL_MULTIMODEL_POLICY},
    }
    raw = canonical_json_bytes(manifest)
    (retained / "snapshot.json").write_bytes(raw)
    pointer = {
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "published_at": reference.isoformat(),
    }
    # Availability is fixture metadata, not a fabricated real-provider assertion.
    information = {
        "status": "complete",
        "sources": [],
        "limitations": [],
        "rule": "Synthetic fixture created before its test analysis cutoff",
    }
    monkeypatch.setattr(
        background, "resolve_latest_complete", lambda _: (pointer, manifest, retained)
    )
    monkeypatch.setattr(background, "verify_prepared_run", lambda _: {"fixture": True})
    monkeypatch.setattr(background, "source_information", lambda _: information)
    expected = {}

    def calculate(*, latitude, longitude):
        result = prepared.forecast(latitude=latitude, longitude=longitude)
        expected.update(result)
        return result

    load = Mock(
        return_value=SimpleNamespace(reference_view=lambda _: SimpleNamespace(forecast=calculate))
    )
    monkeypatch.setattr(background, "load_preparation", load)
    root = tmp_path / "baselines"
    result = background.build_baseline(tmp_path, root, [location], reference_times=[reference])
    assert result["status"] == "published"
    assert load.call_count == 1
    assert result["manifest"]["coherence_and_derivation"]["status"] == "passed"
    assert result["manifest"]["forecast_horizon"] == FIVE_DAY_HORIZON.payload()
    forbidden = Mock(side_effect=AssertionError("Saved forecast consumption cannot recalculate"))
    monkeypatch.setattr(FieldBlendEngine, "blend_field", forbidden)
    monkeypatch.setattr(CoherenceEngine, "apply_baseline", forbidden)
    monkeypatch.setattr(background, "load_preparation", forbidden)
    pinned = baselines.load_baseline(root)
    forecast = pinned.reference_view(reference).forecast(latitude=46.0, longitude=-93.0)
    assert forecast == expected
    grid = forecast["local_grid_baseline"]
    assert len(grid["cells"]) == 49
    assert all(len(cell["hours"]) == 120 for cell in grid["cells"])
    assert validate_grid(grid)["cell_hours"] == 49 * 120
    assert forecast["qpf_intervals"][-1]["accumulation_duration_hours"] in (3, 6)
    assert (
        forecast["hours"][-1]["surface"]["fields"]["liquid_equivalent_precipitation_amount_1h"][
            "value"
        ]
        is None
    )
    # Current editable hourly QPF still replays in lockstep with the exact event.
    changed, recipe = apply_edit(grid, proposal(grid, parameter=1.2))
    assert replay_edits(grid, [recipe]) == changed
    final = {
        **forecast,
        **extract_grid_point(changed, latitude=46.0, longitude=-93.0, copy_grid=False),
    }
    assert final["qpf_intervals"][-1] == forecast["qpf_intervals"][-1]
    objects, factory = InMemoryObjectStore(), InMemoryUnitOfWorkFactory()
    issuer = ForecastIssuanceService(
        objects, factory, code_identity={"git_commit": "a" * 40}, clock=lambda: reference
    )
    record = issuer.issue(final, batch_run_id=uuid4(), location_index=0)
    saved = issuer.read(record.issued_forecast_id)
    assert saved["forecast"] == final
    assert record.forecast_horizon_hours == 120
    assert record.content_digest != record.payload_digest
    assert (
        len(objects.objects[record.content_digest])
        < result["manifest"]["domains"][0]["artifact"]["uncompressed_bytes"] / 4
    )
    document = build_forecast_document(saved, location=location, hours=120)
    assert len(document["days"]) == 5
    assert document["summary"]["qpf_kg_m2"] == sum(row["value"] for row in final["qpf_intervals"])
    pdf = render_forecast_pdf(document)
    reader = PdfReader(BytesIO(pdf))
    assert len(reader.pages) == 2
    assert "5-Day Weather Outlook" in reader.pages[0].extract_text()
    assert len(factory.issued_forecasts) == 1
    forbidden.assert_not_called()
