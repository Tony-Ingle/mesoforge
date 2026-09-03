"""Regression probes for Phase 2 model-exclusion granularity and for the
live Minnesota Markdown report's contributing-model/observation counts.

Three defects are covered:

1. ``_screen_model_cycle`` previously lived inline behind a single
   ``except (KeyError, ValueError, GustDisqualificationError)``, so a
   model vanished from the product with no record of *why*. The cause is
   now classified and reported. Whole-cycle disqualification scope is
   deliberate (plan Section 4.1: "variable-only fallback is forbidden")
   and is asserted here so it cannot be silently narrowed later.
2. The Markdown read ``availability["available_models"]``, a key the
   availability report never publishes, so it always printed "none".
3. The Markdown counted ``row["observed_value"]``, a key matched-pairs.v2
   rows never carry, so it always printed 0 observations, and it
   unconditionally claimed every valid time was in the future.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mesoforge.application.phase2_production import Phase2ProductionScience

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run_phase2_live.py"


def _load_runner() -> Any:
    """Load the live runner module by path; it is a script, not a
    package member, so it is imported explicitly here."""
    spec = importlib.util.spec_from_file_location("run_phase2_live_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class _Station:
    station_id: str


_STATIONS = (_Station("station.kjmr"),)
_HORIZONS = (1,)


def _aligned(*, gust: float, eastward: float, northward: float) -> dict[str, Any]:
    return {
        "models": ["GFS"],
        "values": {
            "GFS": {
                "1": {
                    "station.kjmr|wind_gust_10m": gust,
                    "station.kjmr|eastward_wind_10m": eastward,
                    "station.kjmr|northward_wind_10m": northward,
                }
            }
        },
    }


def _screen(aligned: dict[str, Any]) -> dict[str, Any] | None:
    return Phase2ProductionScience._screen_model_cycle(
        Phase2ProductionScience,  # type: ignore[arg-type]
        aligned=aligned,
        model="GFS",
        stations=_STATIONS,
        horizons=_HORIZONS,
    )


class TestModelCycleScreening:
    def test_consistent_cycle_is_not_excluded(self) -> None:
        assert _screen(_aligned(gust=8.0, eastward=3.0, northward=4.0)) is None

    def test_gust_within_floor_tolerance_is_not_excluded(self) -> None:
        """A shortfall inside the 0.1 m/s tolerance is floored, not
        disqualifying."""
        assert _screen(_aligned(gust=4.95, eastward=3.0, northward=4.0)) is None

    def test_material_gust_shortfall_reports_cause_and_location(self) -> None:
        """The real live-run failure mode: a sub-0.4 m/s shortfall at a
        single point dropped GFS from all 756 rows with no explanation."""
        exclusion = _screen(_aligned(gust=2.068, eastward=2.436, northward=0.0))
        assert exclusion is not None
        assert exclusion["model"] == "GFS"
        assert exclusion["reason"] == "source_gust_inconsistency"
        assert exclusion["station"] == "station.kjmr"
        assert exclusion["target_horizon"] == 1
        assert exclusion["shortfall_m_s"] == pytest.approx(0.368, abs=1e-3)

    def test_missing_aligned_point_is_distinguished_from_gust_inconsistency(self) -> None:
        aligned = _aligned(gust=8.0, eastward=3.0, northward=4.0)
        del aligned["values"]["GFS"]["1"]["station.kjmr|wind_gust_10m"]
        exclusion = _screen(aligned)
        assert exclusion is not None
        assert exclusion["reason"] == "missing_aligned_point"
        assert "wind_gust_10m" in exclusion["detail"]

    def test_missing_horizon_is_distinguished(self) -> None:
        aligned = _aligned(gust=8.0, eastward=3.0, northward=4.0)
        aligned["values"]["GFS"] = {}
        exclusion = _screen(aligned)
        assert exclusion is not None
        assert exclusion["reason"] == "missing_aligned_horizon"

    def test_one_bad_point_disqualifies_the_whole_cycle(self) -> None:
        """Section 4.1 scope guard: a source-level inconsistency
        disqualifies the entire model cycle. This asserts the approved
        design, so narrowing it to a variable-only exclusion would be a
        deliberate, reviewed change rather than an accident."""
        aligned = {
            "models": ["GFS"],
            "values": {
                "GFS": {
                    "1": {
                        "station.kjmr|wind_gust_10m": 8.0,
                        "station.kjmr|eastward_wind_10m": 3.0,
                        "station.kjmr|northward_wind_10m": 4.0,
                    },
                    "2": {
                        "station.kjmr|wind_gust_10m": 2.068,
                        "station.kjmr|eastward_wind_10m": 2.436,
                        "station.kjmr|northward_wind_10m": 0.0,
                    },
                }
            },
        }
        exclusion = Phase2ProductionScience._screen_model_cycle(
            Phase2ProductionScience,  # type: ignore[arg-type]
            aligned=aligned,
            model="GFS",
            stations=_STATIONS,
            horizons=(1, 2),
        )
        assert exclusion is not None
        assert exclusion["target_horizon"] == 2


class TestMarkdownReporting:
    """These call the runner's real section builders, so a renamed
    availability/matched-pairs key fails a test instead of silently
    degrading the published report."""

    def test_contributing_models_uses_the_published_key(self) -> None:
        """Reproduces the live bug: the report said "none" while HRRR and
        NBM were contributing to every row."""
        runner = _load_runner()
        rendered = "\n".join(
            runner._availability_lines({"run_state": "degraded", "models": ["HRRR", "NBM"]})
        )
        assert "- contributing models: HRRR, NBM" in rendered
        assert "contributing models: none" not in rendered

    def test_absent_contributor_set_still_reports_none(self) -> None:
        rendered = "\n".join(_load_runner()._availability_lines({"run_state": "invalid"}))
        assert "- contributing models: none" in rendered

    def test_excluded_model_is_reported_with_cause(self) -> None:
        """An excluded model must never disappear silently."""
        runner = _load_runner()
        rendered = "\n".join(
            runner._availability_lines(
                {
                    "run_state": "degraded",
                    "models": ["HRRR", "NBM"],
                    "excluded_models": [
                        {
                            "model": "GFS",
                            "reason": "source_gust_inconsistency",
                            "station": "station.kjmr",
                            "target_horizon": 5,
                            "shortfall_m_s": 0.368,
                        }
                    ],
                }
            )
        )
        assert "excluded model: **GFS** (source_gust_inconsistency)" in rendered
        assert "station.kjmr horizon 5" in rendered
        assert "0.3680" in rendered

    def test_observations_are_counted_from_per_field_columns(self) -> None:
        """matched-pairs.v2 has no scalar ``observed_value``; counting it
        always reported zero observations."""
        runner = _load_runner()
        rendered = "\n".join(
            runner._verification_lines(
                {
                    "verification_cutoff": "2026-09-03T06:00:00Z",
                    "rows": [
                        {
                            "valid_time": "2026-09-03T00:00:00Z",
                            "observed_temperature_k": 295.0,
                        },
                        {
                            "valid_time": "2026-09-03T01:00:00Z",
                            "observed_wind_speed_m_s": 3.0,
                        },
                        {
                            "valid_time": "2026-09-04T00:00:00Z",
                            "observed_temperature_k": None,
                        },
                    ],
                }
            )
        )
        assert "3 rows, 2 with at least one observed field" in rendered
        assert "`observed_temperature_k` 1" in rendered
        assert "`observed_wind_speed_m_s` 1" in rendered

    def test_future_and_verifiable_rows_are_separated(self) -> None:
        """The old text unconditionally claimed every valid time was in
        the future, which was false for 36 of 108 live rows."""
        runner = _load_runner()
        rendered = "\n".join(
            runner._verification_lines(
                {
                    "verification_cutoff": "2026-09-03T06:00:00Z",
                    "rows": [
                        {"valid_time": "2026-09-03T00:00:00Z"},
                        {"valid_time": "2026-09-03T06:00:00Z"},
                        {"valid_time": "2026-09-04T00:00:00Z"},
                    ],
                }
            )
        )
        assert "(verifiable now): 2" in rendered
        assert "the remaining 1 are still in the future" in rendered
        assert "no observed field was populated in any row" in rendered

    def test_cutoff_is_read_from_rows_when_absent_at_top_level(self) -> None:
        runner = _load_runner()
        rendered = "\n".join(
            runner._verification_lines(
                {
                    "rows": [
                        {
                            "valid_time": "2026-09-03T00:00:00Z",
                            "verification_cutoff": "2026-09-03T06:00:00Z",
                        }
                    ]
                }
            )
        )
        assert "(verifiable now): 1" in rendered

    def test_parse_utc_handles_z_suffix_and_rejects_junk(self) -> None:
        runner = _load_runner()
        assert runner._parse_utc("2026-09-03T06:00:00Z") is not None
        assert runner._parse_utc("not-a-timestamp") is None
        assert runner._parse_utc(None) is None
