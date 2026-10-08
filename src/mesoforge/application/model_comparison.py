"""Read-only comparison of exact saved temperature verifications and contributors."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Sequence
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from mesoforge.application.issuance import issued_forecast_context, read_issued_forecast
from mesoforge.application.issued_temperature_verification import configured_service
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.application.prepared_temperature import _code_identity
from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION, ContributorConfiguration
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.verification.issued_temperature import forecast_eligibility_reasons
from mesoforge.verification.model_comparison import (
    compare_hour,
    require_raw_temperature_control,
    summarize,
    temperature_control_stage,
)

_JSON = CanonicalJsonSerializer()
_PACKAGE = Path(__file__).resolve().parents[1]
_EXTRACTION_SOURCES = (
    "alignment/station_frame.py",
    "alignment/spatial.py",
    "forecasting/scalar_blend.py",
)
_EXTRACTION_DEPENDENCIES = ("numpy", "xarray", "pyproj", "h5netcdf")


def _configuration(forecast: dict[str, Any]) -> ContributorConfiguration:
    """Read the issued configuration, with the original recipes for pre-registry history."""
    snapshot = forecast.get("contributor_configuration")
    return (
        ContributorConfiguration.model_validate_json(json.dumps(snapshot))
        if snapshot is not None
        else DEFAULT_CONFIGURATION
    )


def comparison_identity() -> dict[str, Any]:
    identity = _code_identity()
    for name in (
        "application/model_comparison.py",
        "verification/model_comparison.py",
        "verification/metrics.py",
        "alignment/temporal.py",
    ):
        identity["source_sha256"][name] = hashlib.sha256((_PACKAGE / name).read_bytes()).hexdigest()
    return identity


class RetainedContributors:
    """Optional recovery for old records; never rewrite history or prepare/download data."""

    def __init__(self, roots: Sequence[Path], identity: dict[str, Any]) -> None:
        self._roots = tuple(roots)
        self._identity = identity
        self._index: dict[Digest, Path] | None = None
        self._loaded: dict[str, PreparedPointForecast] = {}
        self._points: dict[tuple[str, float, float], dict[str, Any]] = {}

    def _find(self, digest: Digest) -> Path | None:
        digest = Digest(digest)
        if self._index is None:
            self._index = {}
            for root in self._roots:
                if not root.is_dir():
                    raise ValueError(f"Retained guidance root is not a directory: {root}")
                for path in sorted(root.rglob("manifest.json")):
                    actual = Digest.of_bytes(path.read_bytes())
                    self._index.setdefault(actual, path.parent)
        return self._index.get(digest)

    def resolve(
        self, saved: dict[str, Any], hour: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if all("temperature" in source for source in hour["sources"]):
            return hour, {"origin": "issued_payload", "reasons": []}
        evidence: dict[str, Any] = {
            "origin": "unavailable",
            "reasons": ["Individual contributor values were not saved in this issued version."],
        }
        forecast = saved["forecast"]
        digest = forecast.get("manifest_sha256")
        if not digest or not self._roots:
            evidence["reasons"].append("No matching retained prepared guidance was supplied.")
            return hour, evidence
        # Historical payloads retain bare SHA256 hex; the lookup uses the typed
        # digest contract without rewriting the saved representation.
        directory = self._find(Digest(f"sha256:{digest}"))
        if directory is None:
            evidence["reasons"].append("The exact issued prepared-manifest checksum was not found.")
            return hour, evidence
        original_identity = saved["code_identity"]
        for group, names in (
            ("source_sha256", _EXTRACTION_SOURCES),
            ("dependency_versions", _EXTRACTION_DEPENDENCIES),
        ):
            if any(
                original_identity.get(group, {}).get(name) != self._identity[group][name]
                for name in names
            ):
                evidence["reasons"].append(
                    "Retained extraction code/dependencies differ or are not recorded; "
                    "contributors cannot be reconstructed by this command."
                )
                return hour, evidence
        if digest not in self._loaded:
            # Existing reader verifies manifest, prepared files and every retained raw/index file.
            prepared = PreparedPointForecast.from_directory(directory)
            if prepared._manifest_sha256 != digest:
                raise IntegrityError("Prepared manifest changed during contributor recovery")
            self._loaded[digest] = prepared
        key = digest, forecast["latitude"], forecast["longitude"]
        if key not in self._points:
            self._points[key] = self._loaded[digest].forecast(
                latitude=forecast["latitude"], longitude=forecast["longitude"]
            )
        reconstructed = self._points[key]
        if any(
            reconstructed[name] != forecast[name]
            for name in ("latitude", "longitude", "target_reference_time", "data_kind")
        ):
            raise IntegrityError("Retained guidance does not describe the issued coordinate/time")
        recovered = next(
            (row for row in reconstructed["hours"] if row["valid_time"] == hour["valid_time"]),
            None,
        )
        if recovered is None:
            raise IntegrityError("Retained guidance does not contain the issued valid time")
        if any(recovered[name] != value for name, value in hour.items() if name != "sources"):
            raise IntegrityError("Recovered guidance disagrees with the exact issued control")
        by_model = {source["model"]: source for source in recovered["sources"]}
        for source in hour["sources"]:
            candidate = by_model.get(source["model"], {})
            if any(candidate.get(name) != value for name, value in source.items()):
                raise IntegrityError("Recovered contributor provenance disagrees with issuance")
        return recovered, {
            "origin": "reconstructed_from_retained_prepared_guidance",
            "manifest_sha256": digest,
            "reasons": [],
            "notice": "Re-extracted from exact retained inputs; original issued bytes unchanged.",
            "identity_check": {
                "matched_original_sources": list(_EXTRACTION_SOURCES),
                "matched_original_dependencies": list(_EXTRACTION_DEPENDENCIES),
                "limitation": (
                    "Older issuances did not record alignment/temporal.py identity. "
                    "Current extraction identity is reported; the original control, "
                    "valid times and source provenance were reproduced exactly."
                ),
            },
        }


def compare_verified(
    verification_ids: Sequence[ArtifactId],
    *,
    guidance_roots: Sequence[Path] = (),
    read_verification: Callable[[ArtifactId], dict[str, Any]] | None = None,
    read_forecast: Callable[[UUID], dict[str, Any]] = read_issued_forecast,
) -> dict[str, Any]:
    """One observation revision per issued hour, explicitly selected by verification ID."""
    identifiers = list(
        dict.fromkeys(ArtifactId(str(identifier)) for identifier in verification_ids)
    )
    identity = comparison_identity()
    resolver = RetainedContributors(guidance_roots, identity)
    reader = read_verification
    if identifiers and reader is None:
        reader = configured_service().read
    forecasts: dict[UUID, dict[str, Any]] = {}
    seen: set[tuple[UUID, datetime]] = set()
    rows = []
    for identifier in identifiers:
        assert reader is not None
        verification = reader(identifier)
        fact = verification["result"]
        match = fact["match"]
        issued_id = UUID(match["issued_forecast_id"])
        valid_time = datetime.fromisoformat(match["forecast"]["valid_time"])
        if (issued_id, valid_time) in seen:
            raise ValueError(
                "Select only one verification result per issued-forecast ID and valid time; "
                "different observation revisions must be compared in separate requests."
            )
        seen.add((issued_id, valid_time))
        if issued_id not in forecasts:
            forecasts[issued_id] = read_forecast(issued_id)
        saved = forecasts[issued_id]
        forecast = saved["forecast"]
        hour = next(
            (
                row
                for row in forecast["hours"]
                if row["valid_time"] == match["forecast"]["valid_time"]
            ),
            None,
        )
        if (
            fact["status"] != "verified"
            or str(identifier) != verification["verification_id"]
            or str(issued_id) != saved["issued_forecast_id"]
            or fact["issued_forecast_digest"] != str(Digest.of_bytes(_JSON.serialize(saved)))
            or saved["issued_at"] != match["issued_at"]
            or hour is None
            or match["forecast"]
            != {"latitude": forecast["latitude"], "longitude": forecast["longitude"], **hour}
            or match["forecast_context"] != issued_forecast_context(forecast)
            or match["forecast_code_identity"] != saved["code_identity"]
        ):
            raise IntegrityError("Verification does not describe the exact saved issued forecast")
        require_raw_temperature_control(temperature_control_stage(forecast))
        resolved, contributor_evidence = resolver.resolve(saved, hour)
        configuration = _configuration(forecast)
        ineligible_models = {
            source["model"]: forecast_eligibility_reasons(
                {
                    **resolved,
                    "sources": [source],
                    "temperature": source.get("temperature", {}),
                    "missing_reasons": source.get("missing_reasons", []),
                },
                saved["issued_at"],
                cutoff=datetime.fromisoformat(fact["verification_cutoff"]),
            )
            for source in resolved.get("shadow_sources", [])
        }
        comparison = compare_hour(
            resolved,
            match["selected"]["temperature"],
            configuration=configuration,
            ineligible_models=ineligible_models,
        )
        if (
            comparison["errors"][configuration.control_recipe.result_key]
            != fact["temperature_error"]["value"]
        ):
            raise IntegrityError("Saved verification error disagrees with the retained control")
        rows.append(
            {
                "verification_id": str(identifier),
                "issued_forecast_id": str(issued_id),
                "issued_at": saved["issued_at"],
                "batch_run_id": saved["batch_run_id"],
                "location_index": saved["location_index"],
                "latitude": forecast["latitude"],
                "longitude": forecast["longitude"],
                "target_reference_time": saved["target_reference_time"],
                "valid_time": hour["valid_time"],
                "horizon_hours": hour["horizon_hours"],
                "hours_after_issuance": (
                    valid_time - datetime.fromisoformat(saved["issued_at"])
                ).total_seconds()
                / 3600,
                **comparison,
                "sources": deepcopy(resolved["sources"]),
                "shadow_sources": deepcopy(resolved.get("shadow_sources", [])),
                "contributor_evidence": contributor_evidence,
                "selected_observation": deepcopy(match["selected"]),
                "provenance": {
                    "issued_forecast_digest": fact["issued_forecast_digest"],
                    "issued_code_identity": saved["code_identity"],
                    "forecast_context": match["forecast_context"],
                    "verification_artifact": verification["artifact"],
                    "verification_policy": fact["verification_policy"],
                    "verification_cutoff": fact["verification_cutoff"],
                    "input_provenance": match["input_provenance"],
                },
            }
        )
    return {
        "comparison": "hrrr-gfs-temperature.v1",
        "notice": (
            "Descriptive comparison only; these samples do not establish forecast skill. "
            "Production weights remain HRRR 70% / GFS 30%."
        ),
        "lead_bucket_basis": (
            "Saved horizon_hours since target_reference_time, not native model leads "
            "or elapsed time since issuance; both are retained separately."
        ),
        "verification_ids": [str(identifier) for identifier in identifiers],
        "comparison_code_identity": identity,
        "results": rows,
        "summary": summarize(rows),
    }


def compare_issued(
    issued_forecast_id: UUID,
    *,
    guidance_roots: Sequence[Path] = (),
    read_forecast: Callable[[UUID], dict[str, Any]] = read_issued_forecast,
) -> dict[str, Any]:
    """Expose every saved hour's predictions before an observation is selected/verified."""
    saved = read_forecast(issued_forecast_id)
    if saved["issued_forecast_id"] != str(issued_forecast_id):
        raise IntegrityError("Readback returned a different issued forecast")
    require_raw_temperature_control(temperature_control_stage(saved["forecast"]))
    identity = comparison_identity()
    resolver = RetainedContributors(guidance_roots, identity)
    configuration = _configuration(saved["forecast"])
    hours = []
    for hour in saved["forecast"]["hours"]:
        resolved, evidence = resolver.resolve(saved, hour)
        hours.append(
            {
                "valid_time": hour["valid_time"],
                "horizon_hours": hour["horizon_hours"],
                **compare_hour(resolved, None, configuration=configuration),
                "sources": deepcopy(resolved["sources"]),
                "shadow_sources": deepcopy(resolved.get("shadow_sources", [])),
                "contributor_evidence": evidence,
            }
        )
    return {
        "comparison": "hrrr-gfs-temperature.v1",
        "notice": (
            "Read-only prediction comparison; no observation was selected. "
            "Use explicit verification IDs for saved observations, errors and metrics. "
            "Production weights remain HRRR 70% / GFS 30%."
        ),
        "issued": {key: value for key, value in saved.items() if key != "forecast"},
        "issued_forecast_digest": str(Digest.of_bytes(_JSON.serialize(saved))),
        "forecast_context": issued_forecast_context(saved["forecast"]),
        "comparison_code_identity": identity,
        "hours": hours,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verification-id", type=ArtifactId, action="append", default=[])
    parser.add_argument("--verification-ids-file", type=Path, help="JSON list of verification IDs.")
    parser.add_argument(
        "--issued-forecast-id", type=UUID, help="Compare saved hours without scoring."
    )
    parser.add_argument(
        "--guidance-root",
        type=Path,
        action="append",
        default=[],
        help="Optional retained-guidance directory to recover contributors in older issuances.",
    )
    args = parser.parse_args(argv)
    verifying = bool(args.verification_id) or args.verification_ids_file is not None
    if verifying == (args.issued_forecast_id is not None):
        parser.error("Supply verification IDs, or one --issued-forecast-id")
    try:
        identifiers = args.verification_id
        if args.verification_ids_file is not None:
            values = json.loads(args.verification_ids_file.read_text(encoding="utf-8-sig"))
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError("Verification IDs file must contain a JSON list of strings")
            identifiers.extend(ArtifactId(value) for value in values)
        result = (
            compare_verified(identifiers, guidance_roots=args.guidance_root)
            if verifying
            else compare_issued(args.issued_forecast_id, guidance_roots=args.guidance_root)
        )
    except (ValueError, IntegrityError, NotFound) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"error": "Could not read retained comparison inputs."}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
