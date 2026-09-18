"""Prepared contributor snapshots: manifest, absolute-time coverage and the latest pointer.

A snapshot is the prepared contributor/evidence data one refresh produced for a
coordinate collection. It is not any model's forecast; MesoForge constructs its own
baseline from it. The manifest references the existing preparation artifacts by path
and digest instead of copying their provenance. Publication is a small pointer file
replaced atomically only after validation, so a failed refresh never moves it.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from mesoforge.application.batch_forecast import validate_current_control
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.application.precipitation_type import POLICY as PTYPE_POLICY
from mesoforge.application.prepared_temperature import _code_identity, _iso
from mesoforge.application.selected_forecast import selection_contributors
from mesoforge.application.spatial_preparation import PreparedRegions, load_prepared
from mesoforge.forecasting.cloud_cover import CLOUD_ACTIVE_POLICY
from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION
from mesoforge.forecasting.thunder import ACTIVE_POLICY as THUNDER_ACTIVE_POLICY
from mesoforge.guidance.coverage import COVERAGE_POLICY, REQUIRED_HOURS, window_hours

SNAPSHOT_SCHEMA = "mesoforge.prepared-snapshot.v1"
POINTER_SCHEMA = "mesoforge.latest-complete-pointer.v1"
POINTER_FILE = "latest_complete.json"
SNAPSHOTS_DIRECTORY = "snapshots"
MANIFEST_FILE = "snapshot.json"

# How each source is used by the CURRENT temporary policies. These are the roles the
# later blend engine must be able to change without redesigning the refresh: a
# contributor's kind is a fact about the product; its usage is today's policy.
CONTRIBUTOR_KINDS = {
    "HRRR": "native_deterministic",
    "GFS": "native_deterministic",
    "RAP": "native_deterministic",
    "IFS": "native_deterministic",
    "NBM": "blended_meta_model",
}
ACTIVE_DETERMINISTIC = ("HRRR", "GFS")
SHADOW_DETERMINISTIC = ("RAP", "IFS")
# Products the current active policies read from the blended NBM guidance.
NBM_ACTIVE_PRODUCTS = {
    "probability_of_precipitation_1h": "pop_guidance",
    "cloud_area_fraction": "cloud_guidance",
    "probability_of_thunder_1h": "thunder_guidance",
}


class SnapshotError(ValueError):
    """The snapshot, its pointer or its retained artifacts do not validate."""


def derive_reference_time(request_time: datetime) -> datetime:
    """An ad-hoc request uses the current UTC hour; it never waits for the next one."""
    if request_time.tzinfo is None or request_time.utcoffset() is None:
        raise ValueError("Request time must include a timezone")
    return request_time.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def current_field_policies(selection: dict[str, Any]) -> dict[str, Any]:
    """Identities of the temporary policies the snapshot serves; not a blend design."""
    blend = selection["source_configuration"]["blend_configuration"]
    control = DEFAULT_CONFIGURATION.control_recipe
    return {
        "air_temperature_2m": {
            "policy": f"{control.name}/{control.version}",
            "contributors": [row.model for row in control.contributors],
            "status": "temporary_fixed_weights",
        },
        "surface_scalar_vector": {
            "policy": blend["scalar_vector_table"]["table_id"],
            "contributors": ["HRRR", "GFS"],
            "status": "temporary_phase2_rows",
        },
        "liquid_equivalent_precipitation_amount_1h": {
            "policy": blend["qpf_table"]["table_id"],
            "contributors": ["HRRR", "GFS"],
            "status": "temporary_phase2_rows",
        },
        "probability_of_precipitation_1h": {
            "policy": blend["pop_policy"]["schema_version"],
            "contributors": ["NBM"],
            "status": "temporary_single_source",
        },
        "cloud_area_fraction": {
            "policy": CLOUD_ACTIVE_POLICY["policy_id"],
            "contributors": ["NBM"],
            "status": "temporary_single_source",
        },
        "probability_of_thunder_1h": {
            "policy": THUNDER_ACTIVE_POLICY["policy_id"],
            "contributors": ["NBM"],
            "status": "temporary_single_source",
        },
        "precipitation_type": {
            "policy": PTYPE_POLICY["id"],
            "contributors": ["HRRR", "GFS"],
            "status": "temporary_agreement_rule",
        },
    }


def _sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _event_coverage(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Valid times an attachment source actually holds, from its retained manifest."""
    directory = Path(descriptor["directory"])
    payload = (directory / "manifest.json").read_bytes()
    if hashlib.sha256(payload).hexdigest() != descriptor["manifest_sha256"]:
        raise SnapshotError(f"Attachment manifest digest differs: {directory}")
    manifest = json.loads(payload)
    available = [e["valid_time"] for e in manifest["events"] if not e["missing_reasons"]]
    missing = {
        e["valid_time"]: e["missing_reasons"][0] for e in manifest["events"] if e["missing_reasons"]
    }
    return {
        "cycle": manifest.get("source_cycle")
        or next((e.get("source_cycle") for e in manifest["events"] if e.get("source_cycle")), None),
        "valid_times": available,
        "missing_valid_times": missing,
        "manifest_sha256": descriptor["manifest_sha256"],
        "directory": str(directory),
    }


def _pop_coverage(
    preparation: dict[str, Any], hours: tuple[int, ...], target: datetime
) -> dict[str, Any]:
    guidance = preparation.get("pop_guidance") or {
        "status": "unavailable",
        "reason": "not prepared",
    }
    if guidance.get("status") != "prepared":
        return {
            "status": "unavailable",
            "reason": guidance.get("reason"),
            "cycle": None,
            "valid_times": [],
            "missing_valid_times": {},
        }
    missing = {row["valid_time"]: row["reason"] for row in guidance.get("missing_hours", [])}
    directory = Path(guidance["directory"])
    payload = (directory / "manifest.json").read_bytes()
    if hashlib.sha256(payload).hexdigest() != guidance["manifest_sha256"]:
        raise SnapshotError(f"PoP manifest digest differs: {directory}")
    # The retained acquisition evidence carries the discovery report it was pinned to.
    evidence = (json.loads(payload).get("selection_evidence") or {}).get("selection") or {}
    return {
        "status": "complete" if not missing else "partial",
        "cycle": guidance["selected_cycle"],
        "valid_times": [
            _iso(target + timedelta(hours=hour))
            for hour in hours
            if _iso(target + timedelta(hours=hour)) not in missing
        ],
        "missing_valid_times": missing,
        "manifest_sha256": guidance["manifest_sha256"],
        "directory": str(directory),
        "selection": {
            "status": evidence.get("status"),
            "reason": evidence.get("reason"),
            "rejected_candidates": evidence.get("rejected_candidates", []),
            "candidates": [
                {key: row.get(key) for key in ("cycle", "status", "reason")}
                for row in evidence.get("candidates", [])
            ],
        },
    }


def _attachment_coverage(
    preparation: dict[str, Any], key: str, *, by: str
) -> dict[str, dict[str, Any]]:
    guidance = preparation.get(key)
    if not guidance:
        return {}
    return {
        descriptor[by]: _event_coverage(descriptor) for descriptor in guidance.get("sources", [])
    }


def build_snapshot_manifest(
    *,
    snapshot_id: str,
    root: Path,
    preparation_path: Path,
    selection_path: Path,
    steps: list[dict[str, Any]],
    downloaded_bytes: int,
    clock: datetime,
    completed_at: datetime,
) -> dict[str, Any]:
    """Describe one finished refresh by reference to its retained preparation artifacts."""
    preparation_bytes = preparation_path.read_bytes()
    preparation = json.loads(preparation_bytes)
    selection = preparation["current_model_set"]["selection"]
    hours = window_hours(selection)
    target = datetime.fromisoformat(selection["target_reference_time"]).astimezone(UTC)
    supported = [_iso(target + timedelta(hours=hour)) for hour in hours]
    control = Path(preparation["directory"])
    contributors: dict[str, Any] = {}
    for model in ACTIVE_DETERMINISTIC:
        contributors[model] = {
            "kind": CONTRIBUTOR_KINDS[model],
            "usage": "active_current_policy",
            "cycle": selection["selected_cycles"][model],
            "valid_times": list(selection["models"][model]["valid_times"]),
            "products": _control_products(preparation),
        }
    shortfalls = preparation.get("shadow_shortfalls", {})
    for model in SHADOW_DETERMINISTIC:
        shadow = preparation["shadows"][model]
        contributors[model] = {
            "kind": CONTRIBUTOR_KINDS[model],
            "usage": "shadow_evidence",
            "cycle": shadow["selected_cycle"],
            "valid_times": [
                _iso(target + timedelta(hours=hour)) for hour in shadow["supported_hours"]
            ],
            "products": ["air_temperature_2m", "surface_fields"],
            "directory": preparation["shadow_directories"][model],
            "status": "partial"
            if model in shortfalls and shadow["supported_hours"]
            else "unavailable"
            if model in shortfalls
            else "complete",
            **(
                {
                    "missing_valid_times": {
                        _iso(target + timedelta(hours=int(hour))): reason
                        for hour, reason in shortfalls[model]["missing_hours"].items()
                    }
                }
                if model in shortfalls
                else {}
            ),
        }
    ptype = _attachment_coverage(preparation, "ptype_guidance", by="model")
    cloud = _attachment_coverage(preparation, "cloud_guidance", by="model")
    thunder = _attachment_coverage(preparation, "thunder_guidance", by="source_id")
    visibility = _attachment_coverage(preparation, "visibility_guidance", by="model")
    absent = {"status": "unavailable", "valid_times": [], "missing_valid_times": {}}
    nbm_products = {
        "probability_of_precipitation_1h": _pop_coverage(preparation, hours, target),
        "cloud_area_fraction": cloud.get("NBM", dict(absent)),
        "probability_of_thunder_1h": thunder.get("NBM_1H", dict(absent)),
    }
    for product in nbm_products.values():
        product.setdefault(
            "status",
            "complete"
            if product["valid_times"] and not product["missing_valid_times"]
            else "partial"
            if product["valid_times"]
            else "unavailable",
        )
        product["usage"] = "active_current_policy"
    contributors["NBM"] = {
        "kind": CONTRIBUTOR_KINDS["NBM"],
        "usage": "active_current_policy_for_listed_products",
        "products": nbm_products,
        "evidence_products": {
            "precipitation_type_conditional_probabilities": ptype.get("NBM"),
        },
    }
    evidence = {
        "precipitation_type_flags": {m: row for m, row in ptype.items() if m != "NBM"},
        "total_cloud_cover": {m: row for m, row in cloud.items() if m != "NBM"},
        "thunder_longer_periods": {s: row for s, row in thunder.items() if s != "NBM_1H"},
        "visibility": visibility,
    }
    manifest = {
        "schema_version": SNAPSHOT_SCHEMA,
        "snapshot_id": snapshot_id,
        "kind": "prepared_contributor_snapshot",
        "description": (
            "Prepared contributor and evidence data for constructing the current MesoForge "
            "baseline; not any model's forecast."
        ),
        "created_at": _iso(clock),
        "completed_at": _iso(completed_at),
        "root": str(root),
        "coverage": {
            "policy": COVERAGE_POLICY,
            "reference_time": _iso(target),
            "decision_time": selection["decision_time"],
            "requested_hours": selection.get("coverage", {}).get("requested_hours", len(hours)),
            "prepared_hours": len(hours),
            "first_valid_time": supported[0],
            "last_valid_time": supported[-1],
            "supported_valid_times": supported,
            "models": selection.get("coverage", {}).get("models", {}),
            "usability_rule": {
                "required_complete": list(ACTIVE_DETERMINISTIC),
                "reported": list(NBM_ACTIVE_PRODUCTS),
                "rule": (
                    "A snapshot serves reference time R only if every required contributor "
                    "holds all valid times R+1..R+36; NBM-based active products report their "
                    "own covered and missing valid times and never shorten the forecast."
                ),
            },
        },
        "contributors": contributors,
        "evidence": evidence,
        "field_policies": current_field_policies(selection),
        "attachments": {
            key: {
                "present": key in preparation,
                "status": preparation.get(key, {}).get("status")
                if isinstance(preparation.get(key), dict)
                else None,
            }
            for key in (
                "pop_guidance",
                "ptype_guidance",
                "cloud_guidance",
                "thunder_guidance",
                "visibility_guidance",
            )
        },
        "completeness": {
            "required_deterministic": all(
                contributors[model]["valid_times"] == supported for model in ACTIVE_DETERMINISTIC
            ),
            "shadows": {model: contributors[model]["status"] for model in SHADOW_DETERMINISTIC},
            "nbm_active_products": {name: row["status"] for name, row in nbm_products.items()},
        },
        "prepared_run": {
            "preparation_file": str(preparation_path),
            "preparation_sha256": hashlib.sha256(preparation_bytes).hexdigest(),
            "final_directory": str(preparation_path.parent),
            "control_directory": str(control),
            "control_manifest_sha256": _sha256_file(_control_manifest(control)),
            "selection_file": str(selection_path),
            "selection_sha256": _sha256_file(selection_path),
            "shadow_directories": dict(preparation["shadow_directories"]),
        },
        "code_identity": _identity(),
        "refresh": {"steps": steps, "downloaded_bytes": downloaded_bytes},
    }
    return manifest


def _control_products(preparation: dict[str, Any]) -> list[str]:
    selection = preparation["current_model_set"]["selection"]
    products = ["air_temperature_2m"]
    if selection.get("surface_fields"):
        products += [
            "dew_point_temperature_2m",
            "eastward_wind_10m",
            "northward_wind_10m",
            "wind_gust_10m",
        ]
    if selection.get("qpf_fields"):
        products.append("liquid_equivalent_precipitation_amount_1h")
    return products


def _control_manifest(control: Path) -> Path:
    index = control / "coverage.json"
    if index.is_file():
        return (
            Path(json.loads(index.read_text(encoding="utf-8"))["source_directory"])
            / "manifest.json"
        )
    return control / "manifest.json"


def _identity() -> dict[str, Any]:
    identity = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for name in (
        "application/prepared_snapshot.py",
        "application/refresh_guidance.py",
        "application/forecast_from_snapshot.py",
        "application/point_forecast.py",
        "guidance/coverage.py",
    ):
        path = package / name
        if path.is_file():
            identity["source_sha256"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return identity


def coverage_for(manifest: dict[str, Any], reference_time: datetime) -> dict[str, Any]:
    """Judge one reference time against absolute prepared valid times."""
    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise ValueError("Reference time must include a timezone")
    reference = reference_time.astimezone(UTC)
    if reference.minute or reference.second or reference.microsecond:
        raise ValueError("Reference time must be an exact UTC hour")
    required = [_iso(reference + timedelta(hours=hour)) for hour in range(1, REQUIRED_HOURS + 1)]
    prepared_reference = datetime.fromisoformat(manifest["coverage"]["reference_time"])
    contributors = manifest["contributors"]
    result: dict[str, Any] = {
        "reference_time": _iso(reference),
        "prepared_reference_time": manifest["coverage"]["reference_time"],
        "reference_offset_hours": int((reference - prepared_reference).total_seconds() // 3600),
        "required_valid_times": [required[0], required[-1]],
        "required_complete": {},
        "missing": {},
        "shadows": {},
        "nbm_active_products": {},
        "usable": False,
        "reason": None,
    }
    if reference < prepared_reference:
        result["reason"] = "Reference time precedes the prepared window"
        return result
    for model in manifest["coverage"]["usability_rule"]["required_complete"]:
        held = set(contributors[model]["valid_times"])
        missing = [valid for valid in required if valid not in held]
        result["required_complete"][model] = not missing
        if missing:
            result["missing"][model] = missing
    for model in SHADOW_DETERMINISTIC:
        held = set(contributors.get(model, {}).get("valid_times", []))
        result["shadows"][model] = {
            "covered_hours": sum(valid in held for valid in required),
            "native_gap_hours": sum(valid not in held for valid in required),
        }
    for name, product in contributors["NBM"]["products"].items():
        held = set(product["valid_times"])
        missing = [valid for valid in required if valid not in held]
        result["nbm_active_products"][name] = {
            "status": product["status"],
            "covered_hours": REQUIRED_HOURS - len(missing),
            "missing_valid_times": missing,
        }
    result["usable"] = all(result["required_complete"].values())
    if not result["usable"]:
        first = min(next(iter(rows)) for rows in result["missing"].values())
        result["reason"] = (
            f"Required contributor coverage ends before {required[-1]}; first missing {first}"
        )
    return result


def snapshot_directory(root: Path, snapshot_id: str) -> Path:
    return root / SNAPSHOTS_DIRECTORY / snapshot_id


def write_manifest(directory: Path, manifest: dict[str, Any]) -> tuple[Path, str]:
    path = directory / MANIFEST_FILE
    payload = json.dumps(manifest, indent=2, allow_nan=False).encode()
    with path.open("xb") as stream:
        stream.write(payload)
    return path, hashlib.sha256(payload).hexdigest()


def read_pointer(root: Path) -> dict[str, Any] | None:
    path = root / POINTER_FILE
    if not path.is_file():
        return None
    pointer: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if pointer.get("schema_version") != POINTER_SCHEMA:
        raise SnapshotError("Unsupported latest-complete pointer schema")
    return pointer


def publish_latest_complete(
    root: Path, manifest: dict[str, Any], manifest_sha256: str, *, published_at: datetime
) -> dict[str, Any]:
    """Atomically replace the pointer; never point at partial work or move backwards."""
    if manifest.get("schema_version") != SNAPSHOT_SCHEMA:
        raise SnapshotError("Only validated prepared-snapshot manifests can be published")
    if not manifest["completeness"]["required_deterministic"]:
        raise SnapshotError(
            "Refusing to publish a snapshot whose required contributors are incomplete"
        )
    current = read_pointer(root)
    reference = manifest["coverage"]["reference_time"]
    if current is not None and current["reference_time"] > reference:
        raise SnapshotError(
            f"Refusing to publish reference {reference} over newer {current['reference_time']}"
        )
    pointer = {
        "schema_version": POINTER_SCHEMA,
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_directory": f"{SNAPSHOTS_DIRECTORY}/{manifest['snapshot_id']}",
        "manifest_file": MANIFEST_FILE,
        "manifest_sha256": manifest_sha256,
        "reference_time": reference,
        "first_valid_time": manifest["coverage"]["first_valid_time"],
        "last_valid_time": manifest["coverage"]["last_valid_time"],
        "published_at": _iso(published_at),
        "previous_snapshot_id": current["snapshot_id"] if current else None,
    }
    temporary = root / f".{POINTER_FILE}.{uuid4().hex}.tmp"
    payload = json.dumps(pointer, indent=2).encode()
    with temporary.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, root / POINTER_FILE)
    return pointer


def resolve_latest_complete(root: Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """Read the pointer, then the manifest it names, verifying the manifest digest."""
    pointer = read_pointer(root)
    if pointer is None:
        raise SnapshotError("No latest-complete prepared snapshot has been published")
    directory = (root / pointer["snapshot_directory"]).resolve()
    if not directory.is_relative_to(root.resolve()):
        raise SnapshotError("Pointer escapes the guidance root")
    path = directory / pointer["manifest_file"]
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != pointer["manifest_sha256"]:
        raise SnapshotError("Snapshot manifest digest differs from the published pointer")
    manifest = json.loads(payload)
    if (
        manifest.get("schema_version") != SNAPSHOT_SCHEMA
        or manifest["snapshot_id"] != pointer["snapshot_id"]
    ):
        raise SnapshotError("Snapshot manifest identity differs from the published pointer")
    return pointer, manifest, directory


GUIDANCE_KEYS = (
    "pop_guidance",
    "probability_sources",
    "ptype_guidance",
    "snowfall_guidance",
    "snowfall_amount_guidance",
    "cloud_guidance",
    "visibility_guidance",
    "thunder_guidance",
    "ice_guidance",
)


def load_preparation(preparation: dict[str, Any]) -> PreparedPointForecast | PreparedRegions:
    """Load a finished preparation and every attachment it lists; no network, no writes."""
    selection = preparation["current_model_set"]["selection"]
    configuration = selection_contributors(selection)
    validate_current_control(configuration)
    prepared = load_prepared(
        Path(preparation["directory"]),
        configuration=configuration,
        shadow_directories={
            model: Path(path) for model, path in preparation["shadow_directories"].items()
        },
        **{key: preparation[key] for key in GUIDANCE_KEYS if key in preparation},
    )
    regions = prepared.regions if isinstance(prepared, PreparedRegions) else [prepared]
    if any(
        region._surface_configuration is None
        or (region._manifest or {}).get("current_model_set") != preparation["current_model_set"]
        for region in regions
    ):
        raise SnapshotError("Prepared surface regions differ from the selected model-set evidence")
    return prepared


def verify_prepared_run(manifest: dict[str, Any]) -> dict[str, Any]:
    """Re-check the retained preparation artifacts the manifest names; no provider I/O."""
    run = manifest["prepared_run"]
    preparation_path = Path(run["preparation_file"])
    if _sha256_file(preparation_path) != run["preparation_sha256"]:
        raise SnapshotError("Retained preparation.json differs from the snapshot manifest")
    control = Path(run["control_directory"])
    if _sha256_file(_control_manifest(control)) != run["control_manifest_sha256"]:
        raise SnapshotError("Retained control manifest differs from the snapshot manifest")
    if _sha256_file(Path(run["selection_file"])) != run["selection_sha256"]:
        raise SnapshotError("Retained selection differs from the snapshot manifest")
    preparation: dict[str, Any] = json.loads(preparation_path.read_bytes())
    return preparation
