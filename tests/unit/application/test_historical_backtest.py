"""Bounded retrospective orchestration using existing matching/comparison fixtures.

All meteorological values and provider timestamps below are invented test data.
Only guidance loading and retained artifact reads are substituted: observation
selection, QC, eligibility, recipes, and metrics execute their production paths.
"""

from __future__ import annotations

import copy
import json
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mesoforge.application import historical_backtest as application
from mesoforge.application.prepared_ifs import IFS_CONFIGURATION
from mesoforge.common.identifiers import ArtifactId
from tests.unit.observations.test_selection import (
    _POLICY,
    _SNAPSHOT,
    _TIME,
    _row,
    _station,
)
from tests.unit.verification.test_model_comparison import _hour

TARGET = datetime(2026, 9, 10, 12, tzinfo=UTC)
AS_OF = TARGET + timedelta(hours=2)
ACQUIRED = datetime(2026, 9, 12, 12, tzinfo=UTC)
EVALUATED = datetime(2026, 9, 13, 12, tzinfo=UTC)
OBSERVATIONS = ArtifactId("art_00000000-0000-0000-0000-000000000003")
LOCATIONS = [{"lat": 45.8, "lon": -93.1}, {"lat": 36.7378, "lon": -119.7871}]
# Horizon, HRRR, GFS, RAP, IFS, control, observation; no implementation computes these.
VALUES = (
    (3, 280.0, 290.0, 284.0, 281.0, 283.0, 282.0),
    (9, 284.0, 278.0, 286.0, 287.0, 282.2, 285.0),
    (21, 282.0, 280.0, 278.0, 284.0, 281.4, 280.0),
)


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def _source_evidence(model, horizon):
    return {
        "model": model,
        "cycle": _iso(TARGET),
        "source_lead_hours": horizon,
        "valid_time": _iso(TARGET + timedelta(hours=horizon)),
        "raw_sha256": "a" * 64,
        "source_url": f"synthetic://{model}/{horizon}",
        "prepared_sha256": "b" * 64,
        "source_metadata": {
            "model_definition": IFS_CONFIGURATION.model_map()[model].model_dump(mode="json"),
            "fixture": "Generated temperature values, not real model data",
        },
        "acquisition": {
            "grib_available_at": _iso(TARGET + timedelta(hours=1)),
            "index_available_at": _iso(TARGET + timedelta(hours=1, minutes=1)),
            "grib_last_modified": "Thu, 10 Sep 2026 13:00:00 GMT",
            "index_last_modified": "Thu, 10 Sep 2026 13:01:00 GMT",
            "grib_retrieved_at": _iso(ACQUIRED),
            "index_retrieved_at": _iso(ACQUIRED),
            "index_sha256": "c" * 64,
        },
    }


@pytest.fixture()
def backtest_case(tmp_path, monkeypatch):
    class FixtureDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return (EVALUATED + timedelta(hours=1)).astimezone(tz)

    monkeypatch.setattr(application, "datetime", FixtureDatetime)
    path = tmp_path / "locations.json"
    path.write_text(json.dumps({"locations": LOCATIONS}), encoding="utf-8")
    stations = tuple(
        _station(icao, lat=location["lat"], lon=location["lon"] + 0.01)
        for icao, location in zip(("KAAA", "KBBB"), LOCATIONS, strict=True)
    )
    forecasts = {}
    observations = []
    for index, (location, station) in enumerate(zip(LOCATIONS, stations, strict=True)):
        hours = []
        for horizon, *temperatures in VALUES:
            # Reflection creates opposite signed errors at the second location.
            values = temperatures if index == 0 else [560.0 - value for value in temperatures]
            hrrr, gfs, rap, ifs, control, observed = values
            hour = _hour(horizon=horizon, hrrr=hrrr, gfs=gfs, control=control)
            hour["valid_time"] = _iso(TARGET + timedelta(hours=horizon))
            for source in hour["sources"]:
                source.update(_source_evidence(source["model"], horizon))
            hour["shadow_sources"] = [
                {
                    **_source_evidence(model, horizon),
                    "weight": 0.0,
                    "temperature": {"value": value, "unit": "K"},
                    "missing_reasons": [],
                }
                for model, value in (("RAP", rap), ("IFS", ifs))
            ]
            hours.append(hour)
            seconds = int((TARGET + timedelta(hours=horizon) - _TIME).total_seconds())
            observations.append(
                _row(
                    station,
                    seconds=seconds,
                    revision=f"location-{index}-hour-{horizon}",
                    temperature_k=observed,
                    ingested_at=ACQUIRED,
                )
            )
        by_horizon = {hour["horizon_hours"]: hour for hour in hours}
        for horizon in range(3, 22):
            if horizon in by_horizon:
                continue
            hour = copy.deepcopy(hours[0])
            hour.update(horizon_hours=horizon, valid_time=_iso(TARGET + timedelta(hours=horizon)))
            for source in hour["sources"] + hour["shadow_sources"]:
                source.update(_source_evidence(source["model"], horizon))
            shadow = hour["shadow_sources"][-1]
            shadow["temperature"]["value"] = None
            shadow["missing_reasons"] = ["IFS: no retained native guidance for this valid time"]
            by_horizon[horizon] = hour
        forecasts[location["lat"], location["lon"]] = {
            "latitude": location["lat"],
            "longitude": location["lon"],
            "target_reference_time": _iso(TARGET),
            "data_kind": "real_prepared_guidance",
            "notice": "Invented values exercising the retained-input format in this test only.",
            "manifest_sha256": "d" * 64,
            "contributor_configuration": IFS_CONFIGURATION.model_dump(mode="json"),
            "hours": [by_horizon[horizon] for horizon in sorted(by_horizon)],
        }
    provenance = {
        "observations": {
            "artifact_id": str(OBSERVATIONS),
            "content_digest": "sha256:" + "e" * 64,
            "availability": {"available_at": _iso(ACQUIRED), "ingested_at": _iso(ACQUIRED)},
            "attributes": {"data_kind": "synthetic_observation_fixture"},
        },
        "station_snapshots": [
            {
                "artifact_id": str(_SNAPSHOT),
                "availability": {"available_at": _iso(ACQUIRED), "ingested_at": _iso(ACQUIRED)},
            }
        ],
        "observation_qc_policy": _POLICY.model_dump(mode="json"),
    }
    inputs = (observations, {_SNAPSHOT: stations}, _POLICY, provenance)
    read = Mock(return_value=inputs)
    point = Mock()
    point.forecast.side_effect = lambda *, latitude, longitude: forecasts[latitude, longitude]
    load = Mock(return_value=point)
    monkeypatch.setattr(application, "load_prepared", load)

    def guidance_evidence(_directories):
        # Inputs have identical source-message identity across the two spatial views.
        evidence = {}
        for forecast in forecasts.values():
            for hour in forecast["hours"]:
                for source in hour["sources"] + hour["shadow_sources"]:
                    key = (
                        source["model"],
                        source["cycle"],
                        hour["valid_time"],
                        source["raw_sha256"],
                    )
                    evidence[key] = source["acquisition"]
        return [{"fixture": "Retained provider metadata"}], evidence

    monkeypatch.setattr(application, "_guidance_evidence", guidance_evidence)
    kwargs = {
        "configuration": IFS_CONFIGURATION,
        "shadow_directories": {model: tmp_path / model for model in ("RAP", "IFS")},
        "observation_ids": [OBSERVATIONS],
        "as_of": AS_OF,
        "start_valid_time": TARGET + timedelta(hours=3),
        "end_valid_time": TARGET + timedelta(hours=21),
        "evaluated_at": EVALUATED,
        "read_observations": read,
    }
    return SimpleNamespace(
        config=path,
        data=tmp_path / "guidance",
        kwargs=kwargs,
        forecasts=forecasts,
        inputs=inputs,
        read=read,
        load=load,
        point=point,
    )


def _run(case, **overrides):
    return application.run_backtest(case.config, case.data, **{**case.kwargs, **overrides})


def _assert_metrics(metrics, expected, count):
    for name, (mae, bias, rmse) in expected.items():
        result = metrics["predictions"][name]
        assert result["sample_count"] == count
        assert result["unit"] == "K"
        assert (result["mae"], result["mean_bias"], result["rmse"]) == pytest.approx(
            (mae, bias, rmse)
        )


def test_four_model_pairs_use_the_same_samples_and_as_of_lead_buckets(backtest_case):
    case = backtest_case
    result = _run(case)
    assert len(result["rows"]) == 38
    assert result["summary"]["all"]["paired_sample_count"] == 6
    expected = {
        "HRRR": (5 / 3, 0, math.sqrt(3)),
        "GFS": (5, 0, math.sqrt(113 / 3)),
        "RAP": (5 / 3, 0, math.sqrt(3)),
        "IFS": (7 / 3, 0, math.sqrt(7)),
        "blend_70_30": (26 / 15, 0, math.sqrt(3.6)),
        "blend_50_50": (8 / 3, 0, math.sqrt(26 / 3)),
    }
    _assert_metrics(result["summary"]["all"], expected, 6)
    for bucket in ("1-6", "7-18", "19-36"):
        assert result["summary"][bucket]["paired_sample_count"] == 2
        assert all(
            item["sample_count"] == 2 for item in result["summary"][bucket]["predictions"].values()
        )
    local = result["locations"]
    assert len(local) == 2
    assert (local[0]["latitude"], local[0]["longitude"]) == tuple(LOCATIONS[0].values())
    _assert_metrics(
        local[0]["summary"]["all"],
        {
            **expected,
            "HRRR": (5 / 3, -1 / 3, math.sqrt(3)),
            "GFS": (5, 1 / 3, math.sqrt(113 / 3)),
            "RAP": (5 / 3, 1 / 3, math.sqrt(3)),
            "IFS": (7 / 3, 5 / 3, math.sqrt(7)),
            "blend_70_30": (26 / 15, -2 / 15, math.sqrt(3.6)),
        },
        3,
    )
    for name, first in local[0]["summary"]["all"]["predictions"].items():
        second = local[1]["summary"]["all"]["predictions"][name]
        assert second["mae"] == pytest.approx(first["mae"])
        assert second["mean_bias"] == pytest.approx(-first["mean_bias"])
    pairs = [row for row in result["rows"] if row["paired_sample"]]
    assert [(row["replay_lead_hours"], row["prepared_horizon_hours"]) for row in pairs] == [
        (1, 3),
        (7, 9),
        (19, 21),
        (1, 3),
        (7, 9),
        (19, 21),
    ]
    first = pairs[0]
    assert first["errors"] == {
        "HRRR": -2,
        "GFS": 8,
        "RAP": 2,
        "IFS": -1,
        "blend_70_30": 1,
        "blend_50_50": 3,
    }
    assert first["observation_match"]["selected"]["provenance"]["revision_digest"]
    assert first["forecast"]["shadow_sources"][-1]["source_lead_hours"] == 3


def test_missing_ifs_hour_never_enters_other_models_paired_metrics(backtest_case):
    case = backtest_case
    observations, stations, _policy, _provenance = case.inputs
    station = stations[_SNAPSHOT][0]
    observations.append(_row(station, seconds=3 * 3600, temperature_k=250.0))  # Valid 16Z.
    result = _run(case)
    missing = next(
        row
        for row in result["rows"]
        if row["latitude"] == LOCATIONS[0]["lat"] and row["prepared_horizon_hours"] == 4
    )
    assert missing["observation"]["value"] == 250.0
    assert missing["predictions"]["IFS"]["value"] is None
    assert missing["predictions"]["IFS"]["missing_reasons"]
    assert missing["errors"]["HRRR"] == 30.0
    assert missing["paired_sample"] is False
    assert "IFS_missing" in missing["exclusion_reasons"]
    for metrics in result["summary"]["all"]["predictions"].values():
        assert metrics["sample_count"] == 6
    assert result["summary"]["all"]["predictions"]["HRRR"]["mae"] == pytest.approx(5 / 3)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("grib_last_modified", None, "grib_historical_publication_evidence_unavailable"),
        (
            "index_last_modified",
            "Thu, 10 Sep 2026 13:02:00 GMT",
            "index_historical_publication_evidence_unavailable",
        ),
        (
            "grib_retrieved_at",
            _iso(EVALUATED + timedelta(hours=1)),
            "grib_acquisition_time_missing_or_after_evaluation",
        ),
    ],
)
def test_unknown_or_inconsistent_source_evidence_excludes_the_same_pair(
    backtest_case, field, value, reason
):
    case = backtest_case
    # Both spatial views retain the same acquired message, with the same missing evidence.
    for forecast in case.forecasts.values():
        forecast["hours"][0]["shadow_sources"][-1]["acquisition"][field] = value
    result = _run(case)
    for row in (result["rows"][0], result["rows"][19]):
        assert reason in row["ineligible_reasons"]["IFS"]
        assert row["errors"]["IFS"] is None
        assert row["paired_sample"] is False
    assert result["summary"]["all"]["paired_sample_count"] == 4
    assert all(
        item["sample_count"] == 4 for item in result["summary"]["all"]["predictions"].values()
    )


def test_source_nominal_cycle_does_not_override_actual_publication(backtest_case):
    case = backtest_case
    for forecast in case.forecasts.values():
        source = forecast["hours"][0]["shadow_sources"][-1]
        source["acquisition"].update(
            grib_available_at=_iso(AS_OF + timedelta(minutes=1)),
            grib_last_modified="Thu, 10 Sep 2026 14:01:00 GMT",
        )
    result = _run(case)
    assert result["rows"][0]["forecast"]["shadow_sources"][-1]["cycle"] == _iso(TARGET)
    assert result["rows"][0]["ineligible_reasons"]["IFS"] == ["grib_published_after_replay_as_of"]
    assert result["summary"]["all"]["paired_sample_count"] == 4


def test_repeat_reuses_read_inputs_without_historical_issuance_or_mutations(backtest_case):
    case = backtest_case
    before = copy.deepcopy((case.forecasts, case.inputs))
    first, repeated = _run(case), _run(case)
    assert first["run_id"] != repeated["run_id"]
    assert first["input_digest"] == repeated["input_digest"]
    assert {key: value for key, value in first.items() if key != "run_id"} == {
        key: value for key, value in repeated.items() if key != "run_id"
    }
    assert (case.forecasts, case.inputs) == before
    assert case.load.call_count == 2  # Once per run, reused for both locations.
    assert case.read.call_count == 2  # Once per artifact/run, not per location/hour.
    assert case.point.forecast.call_count == 4
    assert "issued_forecast_id" not in first and "issued_at" not in first
    for row in first["rows"]:
        for source in row["source_evidence"].values():
            assert source["acquisition"]["grib_retrieved_at"] == _iso(ACQUIRED)
            assert source["operational_input_cutoff_pass"] is False
    assert first["inputs"]["as_of"] == _iso(AS_OF)


def test_fixed_observation_cutoff_is_part_of_reproducible_input_identity(backtest_case):
    first = _run(backtest_case)
    later = _run(backtest_case, evaluated_at=EVALUATED + timedelta(minutes=1))
    assert first["inputs"]["evaluation_cutoff"] == _iso(EVALUATED)
    assert later["inputs"]["evaluation_cutoff"] == _iso(EVALUATED + timedelta(minutes=1))
    assert first["input_digest"] != later["input_digest"]
    # No new eligible observations are present, so changing cutoff alone does not change values.
    assert first["rows"] == later["rows"]
    assert first["summary"] == later["summary"]


@pytest.mark.parametrize("kind", ["observations", "station_snapshots"])
@pytest.mark.parametrize("field", ["available_at", "ingested_at"])
@pytest.mark.parametrize("state", ["missing", "later"])
def test_observation_artifacts_require_publication_and_ingestion_by_evaluation_cutoff(
    backtest_case, kind, field, state
):
    case = backtest_case
    provenance = case.inputs[-1]
    manifest = provenance[kind] if kind == "observations" else provenance["station_snapshots"][0]
    if state == "missing":
        del manifest["availability"][field]
    else:
        manifest["availability"][field] = _iso(EVALUATED + timedelta(seconds=1))
    with pytest.raises(ValueError, match="artifact unavailable at evaluation time"):
        _run(case)
    case.point.forecast.assert_not_called()


def test_observation_artifact_cutoff_includes_exact_publication_and_ingestion_time(backtest_case):
    provenance = backtest_case.inputs[-1]
    for manifest in [provenance["observations"], *provenance["station_snapshots"]]:
        manifest["availability"] = {"available_at": _iso(EVALUATED), "ingested_at": _iso(EVALUATED)}
    assert _run(backtest_case)["summary"]["all"]["paired_sample_count"] == 6


def test_derived_artifacts_use_recorded_availability_without_invented_ingestion(backtest_case):
    provenance = backtest_case.inputs[-1]
    for manifest in [provenance["observations"], *provenance["station_snapshots"]]:
        manifest["availability"] = {
            "available_at": _iso(EVALUATED),
            "ingested_at": None,
            "authority": "mesoforge.derived",
        }
    result = _run(backtest_case)
    assert result["summary"]["all"]["paired_sample_count"] == 6
    retained = result["inputs"]["observations"][0]
    assert retained["observations"]["availability"]["ingested_at"] is None
    assert retained["station_snapshots"][0]["availability"]["ingested_at"] is None
    assert result["rows"][0]["observation_match"]["selected"]["provenance"]["ingested_at"] == _iso(
        ACQUIRED
    )


def test_derived_station_manifest_does_not_ignore_supplied_future_ingestion(backtest_case):
    station = backtest_case.inputs[-1]["station_snapshots"][0]
    station["availability"].update(
        authority="mesoforge.derived", ingested_at=_iso(EVALUATED + timedelta(seconds=1))
    )
    with pytest.raises(ValueError, match="artifact unavailable at evaluation time"):
        _run(backtest_case)
    backtest_case.point.forecast.assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    [
        {"as_of": AS_OF.replace(tzinfo=None)},
        {"as_of": AS_OF + timedelta(minutes=1)},
        {"start_valid_time": AS_OF},
        {"end_valid_time": AS_OF + timedelta(hours=37)},
        {"start_valid_time": TARGET + timedelta(hours=22)},
        {"evaluated_at": TARGET + timedelta(hours=21, minutes=14)},
    ],
)
def test_invalid_time_bounds_stop_before_reading_guidance_or_observations(backtest_case, overrides):
    case = backtest_case
    with pytest.raises(ValueError):
        _run(case, **overrides)
    case.load.assert_not_called()
    case.read.assert_not_called()


def test_duplicate_coordinates_are_not_silently_counted_as_independent_samples(backtest_case):
    case = backtest_case
    case.config.write_text(
        json.dumps({"locations": [LOCATIONS[0], LOCATIONS[0]]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unique"):
        _run(case)
    case.load.assert_not_called()


def test_cli_forwards_fixed_reproduction_inputs_without_running_acquisition(
    backtest_case, tmp_path, monkeypatch, capsys
):
    case = backtest_case
    configuration = tmp_path / "contributors.json"
    configuration.write_text(IFS_CONFIGURATION.model_dump_json(), encoding="utf-8")
    identifiers = tmp_path / "observations.json"
    identifiers.write_text(json.dumps([str(OBSERVATIONS)]), encoding="utf-8")
    run = Mock(return_value={"kind": "retrospective_temperature_backtest", "rows": []})
    monkeypatch.setattr(application, "run_backtest", run)
    application.main(
        [
            "--config",
            str(case.config),
            "--data-dir",
            str(case.data),
            "--contributors-config",
            str(configuration),
            "--observation-ids-file",
            str(identifiers),
            "--shadow-data",
            f"RAP={case.kwargs['shadow_directories']['RAP']}",
            "--shadow-data",
            f"IFS={case.kwargs['shadow_directories']['IFS']}",
            "--as-of",
            _iso(AS_OF),
            "--start-valid-time",
            _iso(case.kwargs["start_valid_time"]),
            "--end-valid-time",
            _iso(case.kwargs["end_valid_time"]),
            "--evaluation-cutoff",
            _iso(EVALUATED),
        ]
    )
    actual = run.call_args
    assert actual.args == (case.config, case.data)
    assert actual.kwargs == {
        key: value for key, value in case.kwargs.items() if key != "read_observations"
    }
    assert json.loads(capsys.readouterr().out)["kind"] == "retrospective_temperature_backtest"
    case.load.assert_not_called()
    case.read.assert_not_called()
