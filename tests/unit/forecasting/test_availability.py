"""Unit tests for mesoforge.forecasting.availability (plan Section 4.1,
Task 7): deterministic fallback selection, PoP NBM-only rule, and run
state evaluation.
"""

from __future__ import annotations

import pytest

from mesoforge.catalog.configuration import FallbackWeightRow, FallbackWeightTable
from mesoforge.forecasting.availability import (
    AvailabilityError,
    evaluate_pop_availability,
    evaluate_run_state,
    evaluate_scalar_vector_availability,
)


def _table() -> FallbackWeightTable:
    from itertools import combinations

    rows = []
    weight_map = {
        ("HRRR", "NBM", "GFS"): (0.50, 0.30, 0.20),
        ("HRRR", "NBM"): (0.60, 0.40, 0.0),
        ("HRRR", "GFS"): (0.70, 0.0, 0.30),
        ("NBM", "GFS"): (0.0, 0.60, 0.40),
        ("HRRR",): (1.0, 0.0, 0.0),
        ("NBM",): (0.0, 1.0, 0.0),
        ("GFS",): (0.0, 0.0, 1.0),
    }
    for size in range(1, 4):
        for combo in combinations(("HRRR", "NBM", "GFS"), size):
            for band in ("h01-h18", "h19-h36"):
                rows.append(
                    FallbackWeightRow(
                        row_id=f"{'.'.join(combo).lower()}.{band}",
                        available_models=combo,
                        horizon_band=band,
                        weights=weight_map[combo],
                    )
                )
    return FallbackWeightTable(table_id="phase2-scalar-vector-fallback.v1", rows=tuple(rows))


class TestEvaluateScalarVectorAvailability:
    def test_all_three_models_is_complete(self) -> None:
        result = evaluate_scalar_vector_availability(
            table=_table(),
            variable_id="air_temperature_2m",
            location="station.kcbg",
            target_horizon=5,
            available_models=frozenset({"HRRR", "NBM", "GFS"}),
        )
        assert result.state == "complete"
        assert result.available_models == ("HRRR", "NBM", "GFS")

    def test_two_models_is_fallback(self) -> None:
        result = evaluate_scalar_vector_availability(
            table=_table(),
            variable_id="air_temperature_2m",
            location="station.kcbg",
            target_horizon=5,
            available_models=frozenset({"HRRR", "NBM"}),
        )
        assert result.state == "fallback"
        assert result.fallback_row is not None
        assert result.fallback_row.weights == (0.60, 0.40, 0.0)

    def test_no_models_is_unavailable(self) -> None:
        result = evaluate_scalar_vector_availability(
            table=_table(),
            variable_id="air_temperature_2m",
            location="station.kcbg",
            target_horizon=5,
            available_models=frozenset(),
        )
        assert result.state == "unavailable"

    def test_inconsistent_model_is_excluded_before_fallback(self) -> None:
        """Section 4.1: a source-level inconsistency disqualifies that
        entire model cycle before fallback selection."""
        result = evaluate_scalar_vector_availability(
            table=_table(),
            variable_id="air_temperature_2m",
            location="station.kcbg",
            target_horizon=5,
            available_models=frozenset({"HRRR", "NBM", "GFS"}),
            inconsistent_models=frozenset({"GFS"}),
        )
        assert result.state == "fallback"
        assert result.available_models == ("HRRR", "NBM")

    def test_all_inconsistent_yields_unavailable(self) -> None:
        result = evaluate_scalar_vector_availability(
            table=_table(),
            variable_id="air_temperature_2m",
            location="station.kcbg",
            target_horizon=5,
            available_models=frozenset({"HRRR"}),
            inconsistent_models=frozenset({"HRRR"}),
        )
        assert result.state == "unavailable"

    def test_never_renormalizes_silently(self) -> None:
        """Weights come from the exact configured row -- never computed
        by renormalizing a preferred row's weights over survivors."""
        result = evaluate_scalar_vector_availability(
            table=_table(),
            variable_id="air_temperature_2m",
            location="station.kcbg",
            target_horizon=5,
            available_models=frozenset({"NBM", "GFS"}),
        )
        assert result.fallback_row is not None
        assert result.fallback_row.weights == (0.0, 0.60, 0.40)

    def test_late_horizon_uses_h19_h36_band(self) -> None:
        result = evaluate_scalar_vector_availability(
            table=_table(),
            variable_id="air_temperature_2m",
            location="station.kcbg",
            target_horizon=20,
            available_models=frozenset({"HRRR", "NBM", "GFS"}),
        )
        assert result.fallback_row is not None
        assert result.fallback_row.horizon_band == "h19-h36"


class TestEvaluatePopAvailability:
    def test_nbm_available_is_complete(self) -> None:
        result = evaluate_pop_availability(
            location="station.kcbg", target_horizon=5, nbm_available=True
        )
        assert result.state == "complete"
        assert result.available_models == ("NBM",)

    def test_nbm_unavailable_is_explicitly_unavailable(self) -> None:
        result = evaluate_pop_availability(
            location="station.kcbg", target_horizon=5, nbm_available=False
        )
        assert result.state == "unavailable"
        assert "NBM unavailable" in (result.reason or "")

    def test_never_synthesizes_from_deterministic_precipitation(self) -> None:
        """No deterministic-precipitation input is even accepted by this
        function's signature -- the only input is NBM availability."""
        result = evaluate_pop_availability(
            location="station.kcbg", target_horizon=5, nbm_available=False
        )
        assert result.fallback_row is None


class TestEvaluateRunState:
    def test_all_complete_yields_complete_run(self) -> None:
        availabilities = (
            evaluate_scalar_vector_availability(
                table=_table(),
                variable_id="air_temperature_2m",
                location="station.kcbg",
                target_horizon=1,
                available_models=frozenset({"HRRR", "NBM", "GFS"}),
            ),
            evaluate_pop_availability(
                location="station.kcbg", target_horizon=1, nbm_available=True
            ),
        )
        summary = evaluate_run_state(availabilities)
        assert summary.state == "complete"

    def test_one_fallback_yields_degraded_run(self) -> None:
        availabilities = (
            evaluate_scalar_vector_availability(
                table=_table(),
                variable_id="air_temperature_2m",
                location="station.kcbg",
                target_horizon=1,
                available_models=frozenset({"HRRR", "NBM"}),
            ),
        )
        summary = evaluate_run_state(availabilities)
        assert summary.state == "degraded"
        assert summary.used_fallback is True

    def test_pop_unavailable_alone_yields_degraded(self) -> None:
        availabilities = (
            evaluate_scalar_vector_availability(
                table=_table(),
                variable_id="air_temperature_2m",
                location="station.kcbg",
                target_horizon=1,
                available_models=frozenset({"HRRR", "NBM", "GFS"}),
            ),
            evaluate_pop_availability(
                location="station.kcbg", target_horizon=1, nbm_available=False
            ),
        )
        summary = evaluate_run_state(availabilities)
        assert summary.state == "degraded"

    def test_deterministic_variable_unavailable_yields_invalid(self) -> None:
        availabilities = (
            evaluate_scalar_vector_availability(
                table=_table(),
                variable_id="air_temperature_2m",
                location="station.kcbg",
                target_horizon=1,
                available_models=frozenset(),
            ),
        )
        summary = evaluate_run_state(availabilities)
        assert summary.state == "invalid"
        assert summary.invalid_variables == (("air_temperature_2m", "station.kcbg", 1),)

    def test_disqualified_model_with_approved_fallback_is_not_automatic_invalidity(self) -> None:
        """A disqualified model with an approved remaining fallback row
        yields 'fallback', not automatic run invalidity."""
        availabilities = (
            evaluate_scalar_vector_availability(
                table=_table(),
                variable_id="air_temperature_2m",
                location="station.kcbg",
                target_horizon=1,
                available_models=frozenset({"HRRR", "NBM", "GFS"}),
                inconsistent_models=frozenset({"GFS"}),
            ),
        )
        summary = evaluate_run_state(availabilities)
        assert summary.state == "degraded"


class TestUnsupportedCombination:
    def test_fallback_table_construction_enforces_full_coverage(self) -> None:
        """FallbackWeightTable's own validator (catalog.configuration)
        rejects an incomplete table at construction time, so
        evaluate_scalar_vector_availability's AvailabilityError path
        (a table missing a row for a requested subset) can only be
        reached by a malformed/bypassed table -- this test documents
        that the construction-time guarantee is what actually prevents
        an unsupported combination from ever reaching evaluation."""
        with pytest.raises(Exception, match="missing"):
            FallbackWeightTable(
                table_id="incomplete-test.v1",
                rows=(
                    FallbackWeightRow(
                        row_id="h.h01-h18",
                        available_models=("HRRR",),
                        horizon_band="h01-h18",
                        weights=(1.0, 0.0, 0.0),
                    ),
                ),
            )

    def test_raises_availability_error_for_a_bypassed_incomplete_table(self) -> None:
        """Exercise the AvailabilityError path directly by constructing
        an incomplete table via model_construct (bypassing Pydantic's
        own validators) -- defense-in-depth if a table were ever built
        outside the normal validated constructor."""
        row = FallbackWeightRow(
            row_id="h.h01-h18",
            available_models=("HRRR",),
            horizon_band="h01-h18",
            weights=(1.0, 0.0, 0.0),
        )
        incomplete_table = FallbackWeightTable.model_construct(
            table_id="bypassed-incomplete.v1", rows=(row,)
        )
        with pytest.raises(AvailabilityError, match="no approved fallback row"):
            evaluate_scalar_vector_availability(
                table=incomplete_table,
                variable_id="air_temperature_2m",
                location="station.kcbg",
                target_horizon=1,
                available_models=frozenset({"NBM"}),
            )
