"""Bounded retrospective temperature evaluation through the existing forecast path.

Archive acquisition is allowed after the historical decision time. This is therefore
a provider-available hindcast, never a claim of historical local issuance/ingestion.
Reads retained guidance and observation artifacts; writes no forecast history.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mesoforge.application.batch_forecast import _coordinates, load_locations
from mesoforge.application.observation_preview import _retained_inputs
from mesoforge.application.prepared_temperature import _code_identity, _hour, _iso
from mesoforge.application.spatial_preparation import load_prepared
from mesoforge.catalog.stations import StationDefinition
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION, ContributorConfiguration
from mesoforge.observations.selection import select_temperature_observation
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.verification.issued_temperature import _instant, forecast_eligibility_reasons
from mesoforge.verification.model_comparison import compare_hour, summarize

_JSON = CanonicalJsonSerializer()


def _guidance_evidence(directories: Sequence[Path]) -> tuple[list[dict[str, Any]], dict[Any, Any]]:
    manifests: dict[str, dict[str, Any]] = {}
    inputs: dict[Any, Any] = {}
    for directory in directories:
        index = directory / "coverage.json"
        paths = (
            [Path(row["directory"]) for row in json.loads(index.read_text())["regions"]]
            if index.is_file()
            else [directory]
        )
        for path in paths:
            payload = (path / "manifest.json").read_bytes()
            digest = str(Digest.of_bytes(payload))
            manifest = json.loads(payload)
            manifests[digest] = {"digest": digest, "manifest": manifest}
            for row in manifest["inputs"]:
                key = (row["model"], row["cycle"], row["valid_time"], row["raw_sha256"])
                if key in inputs and inputs[key] != row:
                    raise ValueError("Conflicting acquisition evidence for the same model message")
                inputs[key] = row
    return list(manifests.values()), inputs


def _source_cutoff(
    source: dict[str, Any], evidence: dict[str, Any], *, as_of: datetime, evaluated_at: datetime
) -> tuple[list[str], bool]:
    """Require recorded publication evidence; retain honest archive ingestion times."""
    reasons = []
    operational = True
    for prefix in ("grib", "index"):
        available = _instant(evidence.get(f"{prefix}_available_at"))
        acquired = _instant(evidence.get(f"{prefix}_retrieved_at"))
        try:
            published = parsedate_to_datetime(evidence[f"{prefix}_last_modified"])
        except (KeyError, TypeError, ValueError):
            published = None
        if published is None or published.tzinfo is None or published != available:
            reasons.append(f"{prefix}_historical_publication_evidence_unavailable")
        elif available is not None and available > as_of:
            reasons.append(f"{prefix}_published_after_replay_as_of")
        if acquired is None or acquired > evaluated_at:
            reasons.append(f"{prefix}_acquisition_time_missing_or_after_evaluation")
        if acquired is None or acquired > as_of:
            operational = False
        if available is not None and acquired is not None and available > acquired:
            reasons.append(f"{prefix}_publication_after_recorded_acquisition")
    if not source.get("raw_sha256") or not source.get("prepared_sha256"):
        reasons.append("model_checksums_unavailable")
    return reasons, operational and not reasons


def run_backtest(
    config_path: Path,
    data_dir: Path,
    *,
    configuration: ContributorConfiguration,
    shadow_directories: Mapping[str, Path],
    observation_ids: Sequence[ArtifactId],
    as_of: datetime,
    start_valid_time: datetime,
    end_valid_time: datetime,
    evaluated_at: datetime | None = None,
    read_observations: Callable[..., Any] = _retained_inputs,
) -> dict[str, Any]:
    """Evaluate one fixed window without acquiring data or impersonating issued forecasts."""
    as_of, start, end = map(_hour, (as_of, start_valid_time, end_valid_time))
    executed_at = datetime.now(UTC)
    evaluated_at = _instant(evaluated_at or executed_at)
    if evaluated_at is None:
        raise ValueError("Evaluation time must include a timezone")
    if evaluated_at > executed_at:
        raise ValueError("Evaluation cutoff cannot be in the future")
    if not as_of < start <= end <= as_of + timedelta(hours=36):
        raise ValueError("Window must be within hours 1..36 after the replay as-of time")
    if end + timedelta(minutes=15) > evaluated_at:
        raise ValueError("The complete observation matching window must be in the past")
    if configuration.control_recipe != DEFAULT_CONFIGURATION.control_recipe:
        raise ValueError("Backtesting cannot change the approved active control")
    locations = [_coordinates(row) for row in load_locations(config_path)]
    if not locations or len(set(locations)) != len(locations):
        raise ValueError("Backtest locations must be nonempty and unique")
    identifiers = sorted(set(observation_ids))
    if not identifiers:
        raise ValueError("At least one retained observation artifact is required")
    forecaster = load_prepared(
        data_dir, configuration=configuration, shadow_directories=shadow_directories
    )
    manifests, evidence = _guidance_evidence([data_dir, *shadow_directories.values()])
    observations, observation_evidence = [], []
    station_snapshots: dict[ArtifactId, tuple[StationDefinition, ...]] = {}
    policy = None
    for identifier in identifiers:
        retained, stations, candidate_policy, provenance = read_observations(identifier)
        if policy is not None and candidate_policy != policy:
            raise ValueError("Observation artifacts use different matching/QC policies")
        policy = candidate_policy
        for manifest in [provenance["observations"], *provenance["station_snapshots"]]:
            availability = manifest.get("availability", {})
            for name in ("available_at", "ingested_at"):
                # Derived artifacts use publication after completion/registration/parents;
                # they have no independent source-ingestion timestamp in this storage contract.
                if (
                    name == "ingested_at"
                    and availability.get(name) is None
                    and availability.get("authority") == "mesoforge.derived"
                ):
                    continue
                instant = _instant(availability.get(name))
                if instant is None or instant > evaluated_at:
                    raise ValueError(
                        "Observation/station artifact unavailable at evaluation time: " + name
                    )
        for key, value in stations.items():
            if key in station_snapshots and station_snapshots[key] != value:
                raise ValueError("Conflicting immutable station snapshots")
            station_snapshots[key] = value
        observations.extend(retained)
        observation_evidence.append(provenance)
    assert policy is not None
    # Do not let a later correction leak into a replay of an earlier evaluation cutoff.
    eligible_observations = [
        row
        for row in observations
        if max(row.provider_available_at, row.ingested_at, row.event_time) <= evaluated_at
    ]
    identity = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for name in (
        "application/historical_backtest.py",
        "application/observation_preview.py",
        "observations/selection.py",
        "verification/issued_temperature.py",
        "verification/model_comparison.py",
        "verification/metrics.py",
        "forecasting/recipes.py",
    ):
        identity["source_sha256"][name] = hashlib.sha256((package / name).read_bytes()).hexdigest()
    inputs = {
        "locations": [{"lat": lat, "lon": lon} for lat, lon in locations],
        "as_of": _iso(as_of),
        "start_valid_time": _iso(start),
        "end_valid_time": _iso(end),
        "evaluation_cutoff": _iso(evaluated_at),
        "configuration": configuration.model_dump(mode="json"),
        "guidance": manifests,
        "observations": observation_evidence,
        "code_identity": identity,
    }
    rows, results = [], []
    for latitude, longitude in locations:
        forecast = forecaster.forecast(latitude=latitude, longitude=longitude)
        if forecast["data_kind"] != "real_prepared_guidance":
            raise ValueError("Historical backtest requires real prepared model data")
        selected_hours = [
            hour
            for hour in forecast["hours"]
            if start <= _hour(datetime.fromisoformat(hour["valid_time"])) <= end
        ]
        expected_times = {
            start + timedelta(hours=n) for n in range(int((end - start).total_seconds() / 3600) + 1)
        }
        if {
            datetime.fromisoformat(hour["valid_time"]) for hour in selected_hours
        } != expected_times:
            raise ValueError("Prepared forecast does not contain the entire requested window")
        location_rows = []
        for hour in selected_hours:
            valid = datetime.fromisoformat(hour["valid_time"])
            ineligible, source_evidence = {}, {}
            for source in hour["sources"] + hour.get("shadow_sources", []):
                key = (
                    source["model"],
                    source["cycle"],
                    hour["valid_time"],
                    source.get("raw_sha256"),
                )
                acquired = evidence.get(key, {})
                reasons = forecast_eligibility_reasons(
                    {
                        **hour,
                        "sources": [source],
                        "temperature": source["temperature"],
                        "missing_reasons": source["missing_reasons"],
                    },
                    as_of,
                    cutoff=evaluated_at,
                )
                cutoff_reasons, operational = _source_cutoff(
                    source, acquired, as_of=as_of, evaluated_at=evaluated_at
                )
                ineligible[source["model"]] = [
                    reason.replace("issuance", "replay_as_of").replace("issued", "available")
                    for reason in reasons
                ] + cutoff_reasons
                source_evidence[source["model"]] = {
                    "acquisition": acquired,
                    "operational_input_cutoff_pass": operational,
                }
            match = select_temperature_observation(
                latitude=latitude,
                longitude=longitude,
                valid_time=valid,
                observations=eligible_observations,
                station_snapshots=station_snapshots,
                policy=policy,
            )
            selected = match["selected"]
            observation = selected["temperature"] if selected else None
            if selected and datetime.fromisoformat(selected["observation_time"]) <= as_of:
                observation = None
                match = {
                    **match,
                    "status": "ineligible",
                    "unavailable_reason": (
                        "Observation does not follow the retrospective decision time."
                    ),
                }
            lead = int((valid - as_of).total_seconds() / 3600)
            comparison = compare_hour(
                {**hour, "horizon_hours": lead},
                observation,
                configuration=configuration,
                ineligible_models=ineligible,
            )
            row = {
                "latitude": latitude,
                "longitude": longitude,
                "valid_time": hour["valid_time"],
                "replay_lead_hours": lead,
                "prepared_horizon_hours": hour["horizon_hours"],
                "forecast": hour,
                "source_evidence": source_evidence,
                "observation_match": match,
                **comparison,
            }
            location_rows.append(row)
        rows.extend(location_rows)
        results.append(
            {
                "latitude": latitude,
                "longitude": longitude,
                "forecast_context": {k: v for k, v in forecast.items() if k != "hours"},
                "summary": summarize(location_rows),
            }
        )
    return {
        "kind": "retrospective_temperature_backtest",
        "run_id": str(uuid4()),
        "executed_at": _iso(executed_at),
        "notice": (
            "Provider-available historical hindcast; not a forecast issued by MesoForge at "
            "the historical decision time. Archive acquisition and station metadata may be "
            "later. No forecast history is created or changed."
        ),
        "cutoff_policy": (
            "Model cycles and recorded GRIB/index publication must precede or equal as_of. "
            "Actual acquisition times are preserved and may be later; "
            "operational_input_cutoff_pass reports this distinction. Observations use "
            "retained revisions available by evaluation_cutoff, selected by existing "
            "50 km/QC/+/-15 minute rules. Buckets use valid time minus as_of; "
            "original prepared horizons remain separate. Valid-time window endpoints "
            "are both inclusive."
        ),
        "input_digest": str(Digest.of_bytes(_JSON.serialize(inputs))),
        "inputs": inputs,
        "rows": rows,
        "summary": summarize(rows),
        "locations": results,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--contributors-config", type=Path, required=True)
    parser.add_argument("--shadow-data", action="append", default=[], metavar="MODEL=PATH")
    parser.add_argument("--observation-ids-file", type=Path, required=True)
    parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    parser.add_argument("--start-valid-time", type=datetime.fromisoformat, required=True)
    parser.add_argument("--end-valid-time", type=datetime.fromisoformat, required=True)
    parser.add_argument("--evaluation-cutoff", type=datetime.fromisoformat)
    args = parser.parse_args(argv)
    shadows = {}
    for value in args.shadow_data:
        model, path = value.split("=", 1)
        if model in shadows:
            parser.error("Repeated shadow model")
        shadows[model] = Path(path)
    result = run_backtest(
        args.config,
        args.data_dir,
        configuration=ContributorConfiguration.model_validate_json(
            args.contributors_config.read_bytes()
        ),
        shadow_directories=shadows,
        observation_ids=[
            ArtifactId(value) for value in json.loads(args.observation_ids_file.read_text())
        ],
        as_of=args.as_of,
        start_valid_time=args.start_valid_time,
        end_valid_time=args.end_valid_time,
        evaluated_at=args.evaluation_cutoff,
    )
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
