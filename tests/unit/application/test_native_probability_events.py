from datetime import datetime

import numpy as np
import pytest

from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.common.horizon import FIVE_DAY_HORIZON
from mesoforge.forecasting.field_blend import FieldBlendEngine
from mesoforge.forecasting.provisional_policy import POP6, PROVISIONAL_MULTIMODEL_POLICY
from mesoforge.forecasting.recipes import PROVISIONAL_CONFIGURATION
from tests.unit.application.test_native_surface_inputs import configuration as configuration


def test_native_probability_is_a_six_hour_event_not_hourly_pop(configuration):
    reference = np.datetime64("2026-10-08T06:00:00", "ns")
    prepared = PreparedPointForecast(
        {},
        reference,
        {},
        "synthetic_demonstration",
        {"forecast_horizon": FIVE_DAY_HORIZON.payload()},
        None,
        FIVE_DAY_HORIZON.leads,
        PROVISIONAL_CONFIGURATION,
        {},
        configuration,
    )
    engine = FieldBlendEngine(
        contributors=PROVISIONAL_CONFIGURATION,
        phase2=configuration,
        policy_family=PROVISIONAL_MULTIMODEL_POLICY,
    )
    rows = [
        {
            "source_id": f"{model}_6H",
            "event_id": f"{model}:event:0",
            "value": value,
            "unit": "1",
            "source_cycle": "2026-10-08T00:00:00Z",
            "source_lead_hours": 12,
            "interval_start": "2026-10-08T06:00:00Z",
            "interval_end": "2026-10-08T12:00:00Z",
            "interval_closure": "left_open_right_closed",
            "temporal_semantics": "probability",
            "threshold": {"value": 0.254, "unit": "kg/m^2", "comparison": "gt"},
            "spatial_support": {"kind": "grid_point"},
            "missing_reasons": [],
        }
        for model, value in (("NBM", 0.2), ("GEFS", 0.8))
    ]
    end = reference + np.timedelta64(6, "h")
    result = prepared._probability_event(engine, rows, valid_time=end, horizon=6)
    assert set(result["weights"]) == {"NBM", "GEFS"}
    assert result["value"] == pytest.approx(
        result["weights"]["NBM"] * 0.2 + result["weights"]["GEFS"] * 0.8
    )
    assert result["policy"].startswith("mesoforge.")
    assert result["event_duration_hours"] == 6
    assert datetime.fromisoformat(result["interval_end"]).hour == 12
    assert result["contributors"]["NBM"] == rows[0]
    assert (
        prepared._probability_event(
            engine, rows, valid_time=reference + np.timedelta64(1, "h"), horizon=1
        )["status"]
        == "not_applicable"
    )
    missing = prepared._probability_event(engine, [], valid_time=end, horizon=6)
    assert missing["value"] is None
    assert missing["policy"] == result["policy"]
    # Conflicting duplicate native evidence cannot be silently selected or averaged.
    ambiguous = prepared._probability_event(engine, [*rows, rows[0]], valid_time=end, horizon=6)
    assert ambiguous["weights"] == {"GEFS": 1.0}
    assert engine.policy_for(POP6).policy_id == result["policy"]
