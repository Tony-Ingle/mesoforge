"""Regression probes for Phase 2 exclusion granularity and for the live
Minnesota Markdown report's contributing-model/observation counts.

The exclusion scope is the smallest scientifically coupled unit (Codex
review ``t_1564b30c``):

* a source gust below its own sustained speed rejects that model's
  ``eastward_wind_10m``/``northward_wind_10m``/``wind_gust_10m`` tuple
  **atomically at that station/horizon**, and nothing else -- the model's
  temperature, dew point, PoP and QPF at that point, and its wind/gust at
  every other point, keep contributing;
* coverage failures (a missing horizon, a missing aligned field) and a
  non-finite/invalid source value still reject the whole model cycle,
  because they are evidence the cycle is not trustworthy as a unit.

Nothing is ever clamped or repaired: the source values are recorded
verbatim and the affected point simply loses that contributor.

The Markdown probes cover two published numbers that were false because
they read keys that never exist (``availability["available_models"]`` and
``row["observed_value"]``), plus the unconditional "every valid time is
in the future" claim.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mesoforge.application.phase2_production import Phase2ProductionScience
from mesoforge.common.identifiers import StationId
from mesoforge.forecasting.contributions import BlendContributionRow, ContributorRecord
from mesoforge.forecasting.gust_blend import SOURCE_GUST_SHORTFALL_FLOOR_TOLERANCE_M_S

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run_phase2_live.py"

# The three real GFS wind/gust inconsistencies the live Minnesota run
# exposed, measured from that run's own aligned values. Sustained speed
# is carried as a pure eastward component so ``hypot`` reproduces it
# exactly; the shortfalls are therefore the live shortfalls.
_LIVE_GFS_VIOLATIONS = (
    ("station.kcbg", 1, 2.3049771406631456, 2.6665220845441224),
    ("station.kjmr", 5, 2.0684211424509824, 2.4359712013825794),
    ("station.kros", 1, 2.2508749928250427, 2.452005985597849),
)


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


def _screen(
    aligned: dict[str, Any],
    *,
    stations: tuple[_Station, ...] = _STATIONS,
    horizons: tuple[int, ...] = _HORIZONS,
) -> tuple[dict[str, Any] | None, dict[tuple[str, int], dict[str, Any]]]:
    return Phase2ProductionScience._screen_model_guidance(
        Phase2ProductionScience,  # type: ignore[arg-type]
        aligned=aligned,
        model="GFS",
        stations=stations,
        horizons=horizons,
    )


class TestGuidanceScreening:
    def test_consistent_guidance_is_not_excluded(self) -> None:
        assert _screen(_aligned(gust=8.0, eastward=3.0, northward=4.0)) == (None, {})

    def test_gust_within_floor_tolerance_is_not_excluded(self) -> None:
        """A shortfall inside the 0.1 m/s tolerance is floored by the
        operator, not disqualifying."""
        assert _screen(_aligned(gust=4.95, eastward=3.0, northward=4.0)) == (None, {})

    def test_material_gust_shortfall_excludes_only_that_point(self) -> None:
        cycle, points = _screen(_aligned(gust=2.068, eastward=2.436, northward=0.0))
        assert cycle is None
        assert list(points) == [("station.kjmr", 1)]
        record = points[("station.kjmr", 1)]
        assert record["model"] == "GFS"
        assert record["reason"] == "source_gust_inconsistency"
        assert record["scope"] == "coupled-wind-gust-point"
        assert record["shortfall_m_s"] == pytest.approx(0.368, abs=1e-3)

    def test_point_exclusion_records_the_coupled_tuple_and_source_values(self) -> None:
        """Wind U/V and gust are rejected atomically, with the source
        values and the tolerance they were judged against retained."""
        _cycle, points = _screen(_aligned(gust=2.068, eastward=2.436, northward=0.0))
        record = points[("station.kjmr", 1)]
        assert record["affected_variable_ids"] == [
            "eastward_wind_10m",
            "northward_wind_10m",
            "wind_gust_10m",
        ]
        assert record["source_gust_m_s"] == 2.068
        assert record["source_sustained_speed_m_s"] == pytest.approx(2.436)
        assert record["shortfall_floor_tolerance_m_s"] == (
            SOURCE_GUST_SHORTFALL_FLOOR_TOLERANCE_M_S
        )

    def test_one_bad_point_does_not_disqualify_the_other_points(self) -> None:
        """The core remediation: a localized source-product inconsistency
        must not discard the model's guidance everywhere else."""
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
        cycle, points = _screen(aligned, horizons=(1, 2))
        assert cycle is None
        assert list(points) == [("station.kjmr", 2)]

    def test_every_offending_point_is_reported_not_just_the_first(self) -> None:
        aligned = {
            "models": ["GFS"],
            "values": {
                "GFS": {
                    str(horizon): {
                        "station.kjmr|wind_gust_10m": 2.068,
                        "station.kjmr|eastward_wind_10m": 2.436,
                        "station.kjmr|northward_wind_10m": 0.0,
                    }
                    for horizon in (1, 2, 3)
                }
            },
        }
        cycle, points = _screen(aligned, horizons=(1, 2, 3))
        assert cycle is None
        assert sorted(points) == [("station.kjmr", 1), ("station.kjmr", 2), ("station.kjmr", 3)]

    def test_source_values_are_never_clamped_or_repaired(self) -> None:
        """Screening is read-only: the aligned payload it inspects must
        come back byte-identical."""
        aligned = _aligned(gust=2.068, eastward=2.436, northward=0.0)
        before = {k: dict(v) for k, v in aligned["values"]["GFS"].items()}
        _screen(aligned)
        assert {k: dict(v) for k, v in aligned["values"]["GFS"].items()} == before

    def test_missing_aligned_point_still_rejects_the_whole_cycle(self) -> None:
        aligned = _aligned(gust=8.0, eastward=3.0, northward=4.0)
        del aligned["values"]["GFS"]["1"]["station.kjmr|wind_gust_10m"]
        cycle, points = _screen(aligned)
        assert cycle is not None
        assert cycle["reason"] == "missing_aligned_point"
        assert cycle["scope"] == "model-cycle"
        assert "wind_gust_10m" in cycle["detail"]
        assert points == {}

    def test_missing_horizon_still_rejects_the_whole_cycle(self) -> None:
        aligned = _aligned(gust=8.0, eastward=3.0, northward=4.0)
        aligned["values"]["GFS"] = {}
        cycle, points = _screen(aligned)
        assert cycle is not None
        assert cycle["reason"] == "missing_aligned_horizon"
        assert cycle["scope"] == "model-cycle"
        assert points == {}

    def test_invalid_source_value_still_rejects_the_whole_cycle(self) -> None:
        cycle, points = _screen(_aligned(gust=float("nan"), eastward=3.0, northward=4.0))
        assert cycle is not None
        assert cycle["reason"] == "invalid_source_value"
        assert cycle["scope"] == "model-cycle"
        assert points == {}


class TestLiveMinnesotaViolations:
    """The three known GFS tuples from the live Minnesota run, screened
    together exactly as the production stage screens them."""

    @staticmethod
    def _live_aligned() -> tuple[dict[str, Any], tuple[_Station, ...], tuple[int, ...]]:
        stations = tuple(
            _Station(station) for station in ("station.kcbg", "station.kjmr", "station.kros")
        )
        horizons = (1, 5)
        values: dict[str, dict[str, float]] = {
            str(horizon): {
                f"{station.station_id}|{name}": value
                for station in stations
                # A consistent baseline everywhere: gust 8 vs sustained 5.
                for name, value in (
                    ("wind_gust_10m", 8.0),
                    ("eastward_wind_10m", 3.0),
                    ("northward_wind_10m", 4.0),
                )
            }
            for horizon in horizons
        }
        for station, horizon, gust, sustained in _LIVE_GFS_VIOLATIONS:
            values[str(horizon)][f"{station}|wind_gust_10m"] = gust
            values[str(horizon)][f"{station}|eastward_wind_10m"] = sustained
            values[str(horizon)][f"{station}|northward_wind_10m"] = 0.0
        return {"models": ["GFS"], "values": {"GFS": values}}, stations, horizons

    def test_exactly_the_three_known_tuples_are_excluded(self) -> None:
        aligned, stations, horizons = self._live_aligned()
        cycle, points = _screen(aligned, stations=stations, horizons=horizons)
        assert cycle is None, "a localized inconsistency must not reject the GFS cycle"
        assert sorted(points) == sorted(
            (station, horizon) for station, horizon, _gust, _sustained in _LIVE_GFS_VIOLATIONS
        )

    def test_each_excluded_tuple_carries_its_own_source_evidence(self) -> None:
        aligned, stations, horizons = self._live_aligned()
        _cycle, points = _screen(aligned, stations=stations, horizons=horizons)
        for station, horizon, gust, sustained in _LIVE_GFS_VIOLATIONS:
            record = points[(station, horizon)]
            assert record["source_gust_m_s"] == gust
            assert record["source_sustained_speed_m_s"] == pytest.approx(sustained)
            assert record["shortfall_m_s"] == pytest.approx(sustained - gust)
            assert record["affected_variable_ids"] == [
                "eastward_wind_10m",
                "northward_wind_10m",
                "wind_gust_10m",
            ]

    def test_unaffected_points_of_the_same_stations_are_retained(self) -> None:
        """KCBG h5, KJMR h1 and KROS h5 are valid and must keep GFS."""
        aligned, stations, horizons = self._live_aligned()
        _cycle, points = _screen(aligned, stations=stations, horizons=horizons)
        for station in ("station.kcbg", "station.kjmr", "station.kros"):
            for horizon in horizons:
                if (station, horizon) in points:
                    continue
                assert (station, horizon) not in points


class TestGustFloorReconstruction:
    """A sub-tolerance gust floor is an approved, recorded step -- but the
    contributor records must carry the value the operator actually
    consumed, or the manifest cannot reconstruct the output it claims to
    explain.

    This is the defect the first instrumented run exposed once GFS was
    retained instead of discarded: at KJMR h4 a floored GFS gust made the
    weighted contributions sum to 2.3573 while ``unrounded_sum`` was
    2.3707, and `blend-contribution-manifest.v1` correctly refused it.
    """

    @staticmethod
    def _row(*, aligned_value: float, unrounded_sum: float) -> BlendContributionRow:
        return BlendContributionRow(
            variable_id="wind_gust_10m",
            location=StationId("station.kjmr"),
            target_horizon=4,
            target_valid_time="2026-09-02T22:00:00+00:00",
            operator_id="phase2.wind_gust_10m.v1",
            availability_state="complete",
            fallback_row_id=None,
            contributors=(
                ContributorRecord(
                    model="GFS",
                    source_cycle_reference_time="2026-09-02T12:00:00+00:00",
                    source_forecast_hour=10,
                    artifact_id="art_gfs",
                    aligned_value=aligned_value,
                    configured_weight=1.0,
                    weighted_contribution=aligned_value,
                ),
            ),
            unrounded_sum=unrounded_sum,
            serialized_output=unrounded_sum,
            gust_floor_applied=True,
        )

    def test_unfloored_contributor_value_fails_reconstruction(self) -> None:
        with pytest.raises(ValueError, match="does not reconstruct"):
            self._row(aligned_value=2.3573256835967356, unrounded_sum=2.370659362805913)

    def test_floored_contributor_value_reconstructs(self) -> None:
        row = self._row(aligned_value=2.370659362805913, unrounded_sum=2.370659362805913)
        assert row.gust_floor_applied is True
        assert row.contributors[0].aligned_value == 2.370659362805913

    def test_comparison_row_publishes_the_floor_flags(self) -> None:
        """A floored gust must be visible in the export, not silent."""
        runner = _load_runner()
        rows = runner._comparison_rows(
            contribution_manifest={
                "rows": [
                    {
                        "variable_id": "wind_gust_10m",
                        "location": "station.kjmr",
                        "target_horizon": 4,
                        "target_valid_time": "2026-09-02T22:00:00+00:00",
                        "availability_state": "complete",
                        "operator_id": "phase2.wind_gust_10m.v1",
                        "fallback_row_id": None,
                        "serialized_output": 2.370659362805913,
                        "unrounded_sum": 2.370659362805913,
                        "contributors": [],
                        "gust_floor_applied": True,
                        "final_gust_epsilon_floor_applied": False,
                    }
                ]
            },
            aligned={"values": {}},
            target_reference_time=None,
        )
        assert rows[0]["source_gust_floor_applied"] is True
        assert rows[0]["final_gust_epsilon_floor_applied"] is False


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
        assert "models contributing to at least one output: HRRR, NBM" in rendered
        assert "at least one output: none" not in rendered

    def test_absent_contributor_set_still_reports_none(self) -> None:
        rendered = "\n".join(_load_runner()._availability_lines({"run_state": "invalid"}))
        assert "models contributing to at least one output: none" in rendered

    def test_eligible_and_contributing_sets_are_reported_separately(self) -> None:
        """A model can be eligible for the run and still be excluded at
        some points; conflating the two is what made the old report
        untrue."""
        runner = _load_runner()
        rendered = "\n".join(
            runner._availability_lines(
                {
                    "run_state": "degraded",
                    "models": ["HRRR", "NBM", "GFS"],
                    "eligible_models": ["HRRR", "NBM", "GFS"],
                }
            )
        )
        assert "models contributing to at least one output: HRRR, NBM, GFS" in rendered
        assert "models eligible after whole-cycle screening: HRRR, NBM, GFS" in rendered

    def test_excluded_model_cycle_is_reported_with_cause(self) -> None:
        """A whole-cycle exclusion must never disappear silently."""
        runner = _load_runner()
        rendered = "\n".join(
            runner._availability_lines(
                {
                    "run_state": "degraded",
                    "models": ["HRRR", "NBM"],
                    "eligible_models": ["HRRR", "NBM"],
                    "excluded_models": [
                        {
                            "model": "GFS",
                            "scope": "model-cycle",
                            "reason": "missing_aligned_horizon",
                            "detail": "no aligned values for horizon 5",
                            "station": None,
                            "target_horizon": 5,
                        }
                    ],
                }
            )
        )
        assert "excluded model cycle: **GFS** (missing_aligned_horizon), whole cycle" in rendered
        assert "horizon 5" in rendered

    def test_point_exclusions_are_reported_with_full_evidence(self) -> None:
        runner = _load_runner()
        rendered = "\n".join(
            runner._availability_lines(
                {
                    "run_state": "degraded",
                    "models": ["HRRR", "NBM", "GFS"],
                    "eligible_models": ["HRRR", "NBM", "GFS"],
                    "point_exclusions": [
                        {
                            "model": "GFS",
                            "scope": "coupled-wind-gust-point",
                            "reason": "source_gust_inconsistency",
                            "detail": "source gust below sustained speed",
                            "station": "station.kjmr",
                            "target_horizon": 5,
                            "affected_variable_ids": [
                                "eastward_wind_10m",
                                "northward_wind_10m",
                                "wind_gust_10m",
                            ],
                            "source_gust_m_s": 2.0684211424509824,
                            "source_sustained_speed_m_s": 2.4359712013825794,
                            "shortfall_m_s": 0.367550058931597,
                            "shortfall_floor_tolerance_m_s": 0.1,
                        }
                    ],
                }
            )
        )
        assert "point-level exclusions: 1" in rendered
        assert "| GFS | station.kjmr | 5 | source_gust_inconsistency |" in rendered
        assert "coupled-wind-gust-point" in rendered
        assert "eastward_wind_10m, northward_wind_10m, wind_gust_10m" in rendered
        assert "2.0684" in rendered
        assert "2.4360" in rendered
        assert "0.3676" in rendered
        assert "0.1000" in rendered
        assert "Nothing is clamped, repaired, or synthesized" in rendered

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

    def test_comparison_rows_carry_per_row_exclusion_provenance(self) -> None:
        """A row whose contributor was rejected must publish who was
        excluded and why, not merely omit the value."""
        runner = _load_runner()
        rows = runner._comparison_rows(
            contribution_manifest={
                "rows": [
                    {
                        "variable_id": "wind_gust_10m",
                        "location": "station.kjmr",
                        "target_horizon": 5,
                        "target_valid_time": "2026-09-02T23:00:00+00:00",
                        "availability_state": "fallback",
                        "operator_id": "phase2.wind_gust_10m.v1",
                        "fallback_row_id": "fallback.hn.h01-h18",
                        "serialized_output": 6.0,
                        "unrounded_sum": 6.0,
                        "contributors": [],
                        "excluded_contributors": [
                            {
                                "model": "GFS",
                                "reason": "source_gust_inconsistency",
                                "scope": "coupled-wind-gust-point",
                                "detail": "source gust 2.068 below sustained 2.436",
                                "affected_variable_ids": [
                                    "eastward_wind_10m",
                                    "northward_wind_10m",
                                    "wind_gust_10m",
                                ],
                                "source_gust_m_s": 2.0684211424509824,
                                "source_sustained_speed_m_s": 2.4359712013825794,
                                "shortfall_m_s": 0.367550058931597,
                                "shortfall_floor_tolerance_m_s": 0.1,
                            }
                        ],
                    }
                ]
            },
            aligned={"values": {}},
            target_reference_time=None,
        )
        assert len(rows) == 1
        assert rows[0]["excluded_models"] == "GFS"
        assert rows[0]["exclusion_causes"] == (
            "GFS:source_gust_inconsistency:coupled-wind-gust-point"
        )
        assert "2.068" in rows[0]["exclusion_detail"]

    def test_comparison_rows_without_exclusions_report_empty_provenance(self) -> None:
        runner = _load_runner()
        rows = runner._comparison_rows(
            contribution_manifest={
                "rows": [
                    {
                        "variable_id": "air_temperature_2m",
                        "location": "station.kjmr",
                        "target_horizon": 5,
                        "target_valid_time": "2026-09-02T23:00:00+00:00",
                        "availability_state": "complete",
                        "operator_id": "phase2.air_temperature_2m.v1",
                        "fallback_row_id": "fallback.hng.h01-h18",
                        "serialized_output": 290.0,
                        "unrounded_sum": 290.0,
                        "contributors": [],
                    }
                ]
            },
            aligned={"values": {}},
            target_reference_time=None,
        )
        assert rows[0]["excluded_models"] == ""
        assert rows[0]["exclusion_causes"] == ""
        assert rows[0]["exclusion_detail"] == ""
