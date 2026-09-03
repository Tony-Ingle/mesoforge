#!/usr/bin/env python3
"""Run the Phase 2 deterministic multi-model baseline end to end against
live public NOAA/NCEP guidance, then export human- and machine-readable
comparison artifacts.

This is *run-enablement glue only*. It contains no science, no selectors,
no blending, and no hand calculations: it resolves runtime configuration,
composes the existing production adapters
(:func:`mesoforge.application.phase2_production.build_phase2_production_adapters`)
behind the existing :class:`mesoforge.application.phase2.Phase2Coordinator`,
calls ``coordinator.run``, and then *reads back* repository-authoritative
artifacts through ``ArtifactService`` verified reads to render Markdown,
CSV, and JSON.

Every reported number is loaded from an artifact the production pipeline
itself wrote. Source-model values come from ``aligned-station-guidance.v1``
and the contributor records of ``blend-contribution-manifest.v1``; blended
values come from ``baseline-forecast.v2`` and the manifest's own
``serialized_output``. Nothing is recomputed here.

Usage::

    uv run python scripts/run_phase2_live.py \
        --target-reference-time 2026-09-02T18:00:00Z \
        --forecast-issue-time 2026-09-02T18:00:00Z \
        --information-cutoff 2026-09-02T18:00:00Z \
        --verification-cutoff 2026-09-02T18:00:00Z \
        --output-dir /absolute/output/dir

Infrastructure is resolved from the environment (see ``_env``):
``MESOFORGE_DATABASE_DSN``, ``MESOFORGE_S3_ENDPOINT``,
``MESOFORGE_S3_BUCKET``, ``MESOFORGE_S3_ACCESS_KEY``,
``MESOFORGE_S3_SECRET_KEY``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # pragma: no cover - operational entrypoint
    sys.path.insert(0, str(ROOT / "src"))

from mesoforge.application.artifacts import ArtifactService  # noqa: E402
from mesoforge.application.configuration import ConfigurationService  # noqa: E402
from mesoforge.application.phase2 import (  # noqa: E402
    Phase2Coordinator,
    Phase2Request,
    Phase2Result,
)
from mesoforge.application.phase2_production import (  # noqa: E402
    build_phase2_production_adapters,
    build_phase2_replay_adapters,
)
from mesoforge.catalog.configuration import load_configuration_source  # noqa: E402
from mesoforge.common.identifiers import Digest, RunId  # noqa: E402
from mesoforge.guidance.runtime import SystemClock, SystemSleeper  # noqa: E402
from mesoforge.guidance.sources.hrrr_transport import RequestsHrrrHttpTransport  # noqa: E402
from mesoforge.storage.json import CanonicalJsonSerializer  # noqa: E402
from mesoforge.storage.netcdf import H5NetcdfDatasetSerializer  # noqa: E402
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock  # noqa: E402
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork  # noqa: E402
from mesoforge.storage.s3 import S3ArtifactObjectStore  # noqa: E402

JSON = CanonicalJsonSerializer()
NETCDF = H5NetcdfDatasetSerializer()

MODELS = ("HRRR", "NBM", "GFS")

# The demo variables named by the task, in report order. Dew point is
# carried too because it is part of the same blended scalar family and
# costs nothing extra to report.
DEMO_VARIABLES = (
    "air_temperature_2m",
    "dew_point_temperature_2m",
    "eastward_wind_10m",
    "northward_wind_10m",
    "wind_gust_10m",
    "probability_of_precipitation_1h",
    "liquid_equivalent_precipitation_amount_1h",
)

CANONICAL_UNITS = {
    "air_temperature_2m": "K",
    "dew_point_temperature_2m": "K",
    "eastward_wind_10m": "m s-1",
    "northward_wind_10m": "m s-1",
    "wind_gust_10m": "m s-1",
    "probability_of_precipitation_1h": "1",
    "liquid_equivalent_precipitation_amount_1h": "kg m-2",
}

# A compact, useful subset for the human-readable Markdown table. The
# JSON and CSV exports always carry every target horizon.
COMPACT_HORIZONS = (6, 12, 18, 24, 30, 36)


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"required environment variable {name} is not set")
    return value


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value!r} must carry an explicit UTC offset")
    return parsed.astimezone(UTC)


def _code_revision() -> str:
    """The exact Git commit this run executed, required by the request
    contract to be a 40-character hex SHA."""
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _lockfile_digest() -> Digest:
    return Digest.of_bytes((ROOT / "uv.lock").read_bytes())


def _environment_digest() -> Digest:
    """A documented, deterministic digest of the execution environment.

    Deliberately built from a small, stable, explicitly-listed set of
    facts (interpreter version, platform, and the resolved dependency
    lock) rather than a full environment dump, so the same machine and
    lockfile reproduce the same digest.
    """
    facts = {
        "python_version": platform.python_version(),
        "platform": platform.platform(terse=True),
        "machine": platform.machine(),
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
    }
    return Digest.of_bytes(JSON.serialize(facts))


def _artifact_service(dsn: str) -> tuple[ArtifactService, S3ArtifactObjectStore]:
    store = S3ArtifactObjectStore(
        bucket=_env("MESOFORGE_S3_BUCKET"),
        endpoint_url=_env("MESOFORGE_S3_ENDPOINT"),
        access_key=_env("MESOFORGE_S3_ACCESS_KEY"),
        secret_key=_env("MESOFORGE_S3_SECRET_KEY"),
    )
    # ``ArtifactService`` declares its collaborators as structural
    # Protocols whose *attributes* are invariant, so a concrete
    # PostgreSQL/S3 implementation returning its own repository/stored
    # object types is rejected statically even though it satisfies every
    # method contract at runtime (this is exactly the wiring the
    # integration and acceptance suites exercise). The cast is confined
    # to this composition root; no behaviour is changed.
    service = ArtifactService(
        unit_of_work_factory=cast(Any, lambda: PostgresUnitOfWork(dsn)),
        object_store=cast(Any, store),
        idempotency_lock=PostgresIdempotencyLock(dsn),
    )
    return service, store


def _load_configuration(dsn: str) -> tuple[Any, Any]:
    """Load the authoritative operational configuration overlays.

    Deliberately excludes ``tests/fixtures/phase2-fixture-grid-overlay.yaml``:
    a live run must pin the real operational NBM grid profile, not the
    reduced acceptance fixture grid.
    """
    configuration, _sources = load_configuration_source(
        base_path=ROOT / "configs/base.yaml",
        environment_path=ROOT / "configs/phase1-grasston.yaml",
        additional_overlay_paths=(ROOT / "configs/phase2-grasston.yaml",),
    )
    if configuration.phase2 is None:
        raise SystemExit("loaded configuration has no phase2 section")
    profile = configuration.phase2.nbm.grid_profile.profile_id
    if profile != "nbm-core-conus-operational.v1":
        raise SystemExit(
            f"a live run requires the operational NBM grid profile, got {profile!r}; "
            "the fixture-grid overlay must never be loaded here"
        )
    snapshot = ConfigurationService(
        unit_of_work_factory=cast(Any, lambda: PostgresUnitOfWork(dsn))
    ).register(configuration)
    return configuration.phase2, snapshot


def _coordinator(service: ArtifactService, configuration: Any) -> Phase2Coordinator:
    """Compose the production adapters behind the production coordinator.

    A single real ``requests`` transport instance is shared by all four
    providers, exactly as an operational deployment would.
    """
    transport = RequestsHrrrHttpTransport()
    adapters = build_phase2_production_adapters(
        artifact_service=service,
        configuration=configuration,
        hrrr_transport=transport,
        nbm_transport=transport,
        gfs_transport=transport,
        aviationweather_transport=transport,
        clock=SystemClock(),
        sleeper=SystemSleeper(),
    )
    return Phase2Coordinator(
        artifact_service=service,
        discovery=adapters,
        acquisition=adapters,
        normalization=adapters,
        alignment=adapters,
        availability=adapters,
        forecast=adapters,
        correction=adapters,
        observations=adapters,
        matching=adapters,
        verification=adapters,
    )


def _replay_coordinator(service: ArtifactService) -> Phase2Coordinator:
    """A coordinator that can only replay: no provider, no live
    configuration, and a transport that raises on every request."""
    adapters = build_phase2_replay_adapters(
        artifact_service=service, clock=SystemClock(), sleeper=SystemSleeper()
    )
    return Phase2Coordinator(
        artifact_service=service,
        discovery=adapters,
        acquisition=adapters,
        normalization=adapters,
        alignment=adapters,
        availability=adapters,
        forecast=adapters,
        correction=adapters,
        observations=adapters,
        matching=adapters,
        verification=adapters,
    )


def _json_artifact(service: ArtifactService, manifest: Any) -> Any:
    _found, payload = service.load_verified_payload(manifest.artifact_id)
    return json.loads(payload)


def _dataset_artifact(service: ArtifactService, manifest: Any) -> Any:
    _found, payload = service.load_verified_payload(manifest.artifact_id)
    return NETCDF.deserialize(payload)


def _identity(manifest: Any) -> dict[str, str]:
    return {
        "artifact_id": str(manifest.artifact_id),
        "artifact_type": manifest.artifact_type,
        "schema_version": manifest.artifact_schema_version,
        "content_digest": str(manifest.content_digest),
        "available_at": manifest.availability.available_at.isoformat(),
    }


def _source_inventory(run_spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the run spec's per-lead acquisition evidence.

    Everything here was written by production acquisition: exact URLs,
    endpoints, byte ranges, inventory rows, artifact digests, and both
    kinds of timestamp (provider publication vs local retrieval).
    """
    rows: list[dict[str, Any]] = []
    for group in run_spec["required_groups"]:
        if not group["selected"]:
            continue
        for lead in group["leads"]:
            for message in lead["selected_messages"]:
                rows.append(
                    {
                        "model": group["model"],
                        "source_cycle_reference_time": group["source_cycle_reference_time"],
                        "source_lead_hours": lead["source_lead_hours"],
                        "canonical_variable_id": message["canonical_variable_id"],
                        "endpoint": lead["endpoint"],
                        "grib_url": lead["resolved_grib_url"],
                        "index_url": lead["resolved_index_url"],
                        "inventory_row": message["inventory_row"],
                        "message_number": message["message_number"],
                        "byte_start": message["byte_start"],
                        "byte_end": message["byte_end"],
                        "byte_length": message["byte_end"] - message["byte_start"],
                        "message_artifact_id": message["artifact"]["artifact_id"],
                        "message_content_digest": message["artifact"]["content_digest"],
                        "index_artifact_id": lead["index_artifact"]["artifact_id"],
                        "index_content_digest": lead["index_artifact"]["content_digest"],
                        "grib_available_at": lead["grib_available_at"],
                        "index_available_at": lead["index_available_at"],
                        "grib_last_modified": lead.get("grib_last_modified"),
                        "index_last_modified": lead.get("index_last_modified"),
                        "grib_retrieved_at": lead["grib_completed_at"],
                        "index_retrieved_at": lead["index_completed_at"],
                    }
                )
    return rows


def _comparison_rows(
    *,
    contribution_manifest: dict[str, Any],
    aligned: dict[str, Any],
    target_reference_time: datetime,
) -> list[dict[str, Any]]:
    """Join each blended value to its per-model contributors.

    Both sides are read from production artifacts: the contributor
    records (model, source cycle/lead, aligned value, configured weight,
    weighted contribution) come from ``blend-contribution-manifest.v1``,
    and the per-model aligned value is cross-checked against
    ``aligned-station-guidance.v1``.
    """
    aligned_values = aligned["values"]
    rows: list[dict[str, Any]] = []
    for row in contribution_manifest["rows"]:
        variable_id = row["variable_id"]
        if variable_id not in DEMO_VARIABLES:
            continue
        horizon = row["target_horizon"]
        station = row["location"]
        record: dict[str, Any] = {
            "variable_id": variable_id,
            "units": CANONICAL_UNITS[variable_id],
            "station_id": station,
            "target_horizon_hours": horizon,
            "target_valid_time": row["target_valid_time"],
            "availability_state": row["availability_state"],
            "operator_id": row["operator_id"],
            "fallback_row_id": row["fallback_row_id"],
            "blended_value": row["serialized_output"],
            "blend_unrounded_sum": row["unrounded_sum"],
            # Section 4.3's two explicitly approved, recorded floors. When
            # ``source_gust_floor_applied`` is true, the contributor value
            # in this row is the floored gust the operator actually
            # consumed; the raw source value is still published in the
            # ``{model}_aligned_guidance_value`` column below and in
            # aligned-station-guidance.v1.
            "source_gust_floor_applied": row.get("gust_floor_applied", False),
            "final_gust_epsilon_floor_applied": row.get("final_gust_epsilon_floor_applied", False),
            # Per-row exclusion provenance, written by the pipeline. A
            # model that was eligible for the run but rejected at this
            # exact point appears here with its cause and its verbatim
            # source values, so a missing contributor is never silent.
            "excluded_models": ";".join(
                sorted(item["model"] for item in row.get("excluded_contributors", ()))
            ),
            "exclusion_causes": ";".join(
                f"{item['model']}:{item['reason']}:{item['scope']}"
                for item in sorted(
                    row.get("excluded_contributors", ()), key=lambda item: item["model"]
                )
            ),
            "exclusion_detail": " | ".join(
                item["detail"]
                for item in sorted(
                    row.get("excluded_contributors", ()), key=lambda item: item["model"]
                )
            ),
        }
        for model in MODELS:
            record[f"{model}_value"] = None
            record[f"{model}_weight"] = None
            record[f"{model}_weighted_contribution"] = None
            record[f"{model}_source_cycle"] = None
            record[f"{model}_source_lead_hours"] = None
            model_values = aligned_values.get(model, {}).get(str(horizon), {})
            key = f"{station}|{variable_id}"
            if key in model_values:
                record[f"{model}_aligned_guidance_value"] = model_values[key]
            else:
                record[f"{model}_aligned_guidance_value"] = None
        for contributor in row["contributors"]:
            model = contributor["model"]
            record[f"{model}_value"] = contributor["aligned_value"]
            record[f"{model}_weight"] = contributor["configured_weight"]
            record[f"{model}_weighted_contribution"] = contributor["weighted_contribution"]
            record[f"{model}_source_cycle"] = contributor["source_cycle_reference_time"]
            record[f"{model}_source_forecast_hour"] = contributor["source_forecast_hour"]
            record[f"{model}_source_artifact_id"] = contributor["artifact_id"]
        rows.append(record)
    rows.sort(key=lambda r: (r["variable_id"], r["station_id"], r["target_horizon_hours"]))
    return rows


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    import csv

    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _fmt(value: Any) -> str:
    if value is None:
        return "--"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _parse_utc(value: Any) -> datetime | None:
    """Parse an exported ISO-8601 UTC timestamp, tolerating the
    trailing ``Z`` spelling used by the serialized artifacts."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _availability_lines(availability: dict[str, Any]) -> list[str]:
    """Build the availability/blend-identity section.

    ``models`` is the run-wide *contributing* set: every model that
    contributes to at least one output. ``eligible_models`` is the set
    that survived whole-cycle screening; a model can be eligible and
    still be excluded at individual points, so both are reported and
    neither is presented as the other.
    """
    lines = [f"- run state: **{availability.get('run_state', 'unknown')}**"]
    contributing = availability.get("models", [])
    lines.append(
        f"- models contributing to at least one output: {', '.join(contributing) or 'none'}"
    )
    eligible = availability.get("eligible_models")
    if eligible is not None:
        lines.append(
            f"- models eligible after whole-cycle screening: {', '.join(eligible) or 'none'}"
        )
    for excluded in availability.get("excluded_models", []):
        location = excluded.get("station") or "--"
        horizon = excluded.get("target_horizon")
        detail = (
            f"- excluded model cycle: **{excluded['model']}** ({excluded['reason']}), whole cycle"
        )
        if horizon is not None:
            detail += f", first seen at {location} horizon {horizon}"
        lines.append(detail)
    point_exclusions = availability.get("point_exclusions", [])
    if point_exclusions:
        lines.append(
            f"- point-level exclusions: {len(point_exclusions)}; each rejects only the "
            "named coupled fields at that station/horizon, leaving that model's other "
            "variables and its unaffected points contributing"
        )
        lines.append("")
        lines.append(
            "| model | station | horizon | cause | scope | affected fields | "
            "source gust (m s-1) | source sustained (m s-1) | shortfall (m s-1) | "
            "tolerance (m s-1) |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for record in point_exclusions:
            lines.append(
                f"| {record['model']} | {record.get('station', '--')} | "
                f"{record.get('target_horizon', '--')} | {record['reason']} | "
                f"{record['scope']} | "
                f"{', '.join(record.get('affected_variable_ids', ()))} | "
                f"{_fmt(record.get('source_gust_m_s'))} | "
                f"{_fmt(record.get('source_sustained_speed_m_s'))} | "
                f"{_fmt(record.get('shortfall_m_s'))} | "
                f"{_fmt(record.get('shortfall_floor_tolerance_m_s'))} |"
            )
        lines.append("")
        lines.append(
            "Source values are reported verbatim. Nothing is clamped, repaired, or "
            "synthesized; the affected point simply loses that contributor and falls "
            "back to the approved row for the models that remain."
        )
    return lines


_OBSERVED_FIELDS = (
    "observed_temperature_k",
    "observed_dew_point_k",
    "observed_eastward_wind_m_s",
    "observed_northward_wind_m_s",
    "observed_wind_speed_m_s",
    "observed_wind_from_direction_degrees",
    "observed_wind_gust_m_s",
    "observed_qpf_kg_m2",
)


def _verification_lines(matched_pairs: dict[str, Any]) -> list[str]:
    """Build the verification section.

    matched-pairs.v2 rows carry no scalar ``observed_value`` key; each
    observed field is its own column. Counting ``observed_value``
    therefore always yielded 0 and understated the observations actually
    matched. The prior text also unconditionally asserted that every
    target valid time was in the future, which is false whenever the
    verification cutoff is later than some target valid times.
    """
    pair_rows = matched_pairs.get("rows", [])
    rows_with_observation = sum(
        1 for row in pair_rows if any(row.get(field) is not None for field in _OBSERVED_FIELDS)
    )
    field_counts = {
        field: sum(1 for row in pair_rows if row.get(field) is not None)
        for field in _OBSERVED_FIELDS
    }
    cutoff = _parse_utc(matched_pairs.get("verification_cutoff")) or _parse_utc(
        pair_rows[0].get("verification_cutoff") if pair_rows else None
    )
    verifiable = [
        row
        for row in pair_rows
        if cutoff is not None
        and (valid := _parse_utc(row.get("valid_time"))) is not None
        and valid <= cutoff
    ]
    lines = [
        f"- matched pairs: {len(pair_rows)} rows, {rows_with_observation} with at least one "
        f"observed field",
        f"- rows whose target valid time is at or before the verification cutoff "
        f"(verifiable now): {len(verifiable)}; the remaining "
        f"{len(pair_rows) - len(verifiable)} are still in the future and are forecasts, "
        f"not verified outcomes",
    ]
    populated = {field: count for field, count in field_counts.items() if count}
    if populated:
        lines.append(
            "- observed field coverage: "
            + ", ".join(f"`{field}` {count}" for field, count in sorted(populated.items()))
        )
    else:
        lines.append("- observed field coverage: no observed field was populated in any row")
    return lines


def _markdown(
    *,
    result: Phase2Result,
    run_spec: dict[str, Any],
    comparison: Sequence[dict[str, Any]],
    availability: dict[str, Any],
    verification: dict[str, Any],
    matched_pairs: dict[str, Any],
    inventory: Sequence[dict[str, Any]],
) -> str:
    lines: list[str] = []
    lines.append("# MesoForge Phase 2 live Minnesota baseline")
    lines.append("")
    lines.append(
        "Deterministic multi-model baseline for the Grasston, Minnesota domain "
        "(`45.80265, -93.07956`), produced end to end by the existing Phase 2 "
        "production pipeline against real public NOAA/NCEP guidance."
    )
    lines.append("")
    lines.append("## Run identity")
    lines.append("")
    lines.append("| field | value |")
    lines.append("| --- | --- |")
    lines.append(f"| run_id | `{run_spec['run_id']}` |")
    lines.append(f"| target_reference_time | {run_spec['target_reference_time']} |")
    lines.append(f"| forecast_issue_time | {run_spec['forecast_issue_time']} |")
    lines.append(f"| information_cutoff | {run_spec['information_cutoff']} |")
    lines.append(f"| verification_cutoff | {run_spec['verification_cutoff']} |")
    lines.append(f"| code_revision | `{run_spec['request_digests']['code_revision']}` |")
    lines.append(
        f"| configuration_snapshot_id | "
        f"`{run_spec['request_digests']['configuration_snapshot_id']}` |"
    )
    lines.append(
        f"| configuration_digest | `{run_spec['request_digests']['configuration_digest']}` |"
    )
    lines.append(f"| lockfile_digest | `{run_spec['request_digests']['lockfile_digest']}` |")
    lines.append(f"| environment_digest | `{run_spec['request_digests']['environment_digest']}` |")
    lines.append(f"| random_seed | {run_spec['random_seed']} |")
    lines.append("")

    lines.append("## Selected model cycles")
    lines.append("")
    lines.append(
        "| model | selected | source cycle | source leads | endpoint | "
        "grid profile | provider published (earliest..latest) |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for group in run_spec["required_groups"]:
        leads = group["source_lead_hours"]
        span = f"{min(leads)}..{max(leads)}" if leads else "--"
        published = [lead["grib_available_at"] for lead in group["leads"]]
        window = f"{min(published)} .. {max(published)}" if published else "--"
        lines.append(
            f"| {group['model']} | {group['selected']} | "
            f"{group['source_cycle_reference_time'] or '--'} | {span} | "
            f"{', '.join(group['endpoints']) or '--'} | "
            f"`{group['source_grid_profile_id']}` | {window} |"
        )
    lines.append("")

    lines.append("## Availability and blend identity")
    lines.append("")
    lines.extend(_availability_lines(availability))
    lines.append("")

    lines.append("## Model versus blend comparison")
    lines.append("")
    lines.append(
        "Source-model values are the production-aligned values recorded as blend "
        "contributors; the blended value is the production `serialized_output`. "
        f"Compact horizons shown: {', '.join(str(h) for h in COMPACT_HORIZONS)}. "
        "Every target horizon 1..36 is in the CSV and JSON exports."
    )
    lines.append("")
    for variable_id in DEMO_VARIABLES:
        subset = [
            row
            for row in comparison
            if row["variable_id"] == variable_id and row["target_horizon_hours"] in COMPACT_HORIZONS
        ]
        if not subset:
            continue
        lines.append(f"### {variable_id} ({CANONICAL_UNITS[variable_id]})")
        lines.append("")
        if variable_id == "probability_of_precipitation_1h":
            lines.append(
                "NBM PoP01 is `P(1h accumulation > 0.254 kg m-2)` over the one-hour "
                "window ending exactly at the valid time, decoded from GRIB2 PDT 4.9 "
                "and converted once from percent. It is an exceedance probability, "
                "not an expected amount, so it is not directly comparable to the "
                'deterministic QPF below. See "NBM 1h PoP versus deterministic QPF" '
                "in `docs/data-contracts/phase-2.md`."
            )
            lines.append("")
        if variable_id == "liquid_equivalent_precipitation_amount_1h":
            lines.append(
                "Deterministic 1h QPF is reported verbatim in `kg m-2`, drawn from the "
                "same one-hour interval and lead as PoP01 above. MesoForge makes no "
                "claim about which statistic of the NBM distribution this value "
                "represents: the captured GRIB2 metadata does not state one. Hours "
                "with a QPF above the 0.254 kg m-2 event threshold but a low PoP01 "
                "(for example KJMR horizon 24) are an advisory source-product tension "
                "only; both values are reported verbatim and nothing is altered, "
                "disqualified, or reconciled."
            )
            lines.append("")
        lines.append(
            "| station | horizon | valid time | HRRR | NBM | GFS | "
            "blend | weights (HRRR/NBM/GFS) | state |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in subset:
            weights = "/".join(_fmt(row.get(f"{model}_weight")) for model in MODELS)
            lines.append(
                f"| {row['station_id']} | {row['target_horizon_hours']} | "
                f"{row['target_valid_time']} | "
                f"{_fmt(row.get('HRRR_value'))} | {_fmt(row.get('NBM_value'))} | "
                f"{_fmt(row.get('GFS_value'))} | {_fmt(row['blended_value'])} | "
                f"{weights} | {row['availability_state']} |"
            )
        lines.append("")

    lines.append("## Verification")
    lines.append("")
    lines.extend(_verification_lines(matched_pairs))
    lines.append("")

    lines.append("## Artifact identities")
    lines.append("")
    lines.append("| artifact | id | content digest |")
    lines.append("| --- | --- | --- |")
    for label, manifest in (
        ("phase2-run-spec.v1", result.selected_inputs.run_spec),
        ("station-catalog-snapshot.v1", result.selected_inputs.station_snapshot),
        ("aligned-station-guidance.v1", result.alignment.aligned_guidance),
        ("model-cycle-selection.v1", result.availability.cycle_selection),
        ("model-availability-report.v1", result.availability.report),
        ("uncorrected-blend-forecast.v1", result.atomic_forecast.uncorrected_blend),
        ("blend-contribution-manifest.v1", result.atomic_forecast.contribution_manifest),
        ("identity-bias-correction.v1", result.corrected_forecast.correction),
        ("baseline-forecast.v2", result.corrected_forecast.baseline),
        ("normalized-metar-observations", result.observations.normalized),
        ("matched-pairs.v2", result.matching.matched_pairs),
        ("verification-report.v2", result.verification.report),
    ):
        lines.append(f"| {label} | `{manifest.artifact_id}` | `{manifest.content_digest}` |")
    lines.append("")

    lines.append("## Source inventory")
    lines.append("")
    lines.append(
        f"{len(inventory)} selected GRIB messages were retained by exact byte range. "
        "The complete inventory (URLs, byte ranges, inventory rows, digests, and both "
        "provider-publication and local-retrieval timestamps) is in the JSON and CSV "
        "exports."
    )
    lines.append("")
    lines.append(f"- verification report digest: `{verification.get('schema_version', '')}`")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-reference-time", type=_utc, required=True)
    parser.add_argument("--forecast-issue-time", type=_utc, required=True)
    parser.add_argument("--information-cutoff", type=_utc, required=True)
    parser.add_argument("--verification-cutoff", type=_utc, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--replay",
        action="store_true",
        help="after the live run, replay it from persisted roots with no network access",
    )
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    dsn = _env("MESOFORGE_DATABASE_DSN")
    service, _store = _artifact_service(dsn)
    configuration, snapshot = _load_configuration(dsn)

    request = Phase2Request(
        run_id=RunId(args.run_id) if args.run_id else RunId(f"run_{uuid.uuid4()}"),
        configuration_snapshot_id=snapshot.configuration_snapshot_id,
        configuration_digest=snapshot.configuration_digest,
        code_revision=_code_revision(),
        environment_digest=_environment_digest(),
        lockfile_digest=_lockfile_digest(),
        target_reference_time=args.target_reference_time,
        forecast_issue_time=args.forecast_issue_time,
        information_cutoff=args.information_cutoff,
        verification_cutoff=args.verification_cutoff,
    )

    print(f"[phase2] running {request.run_id} target={request.target_reference_time}", flush=True)
    result = _coordinator(service, configuration).run(request)
    print("[phase2] run complete; exporting artifacts", flush=True)

    run_spec = _json_artifact(service, result.selected_inputs.run_spec)
    aligned = _json_artifact(service, result.alignment.aligned_guidance)
    availability = _json_artifact(service, result.availability.report)
    cycle_selection = _json_artifact(service, result.availability.cycle_selection)
    contribution_manifest = _json_artifact(service, result.atomic_forecast.contribution_manifest)
    matched_pairs = _json_artifact(service, result.matching.matched_pairs)
    verification = _json_artifact(service, result.verification.report)
    station_snapshot = _json_artifact(service, result.selected_inputs.station_snapshot)

    # Prove the baseline dataset itself is readable and carries the blend.
    baseline = _dataset_artifact(service, result.corrected_forecast.baseline)

    comparison = _comparison_rows(
        contribution_manifest=contribution_manifest,
        aligned=aligned,
        target_reference_time=request.target_reference_time,
    )
    inventory = _source_inventory(run_spec)

    replay_report: dict[str, Any] | None = None
    if args.replay:
        print("[phase2] replaying from persisted roots (no network)", flush=True)
        replayed = _replay_coordinator(service).replay(
            request, result.selected_inputs, result.observations.responses
        )
        replay_report = {
            "identical_artifact_ids": {
                "baseline_forecast": (
                    str(replayed.corrected_forecast.baseline.artifact_id)
                    == str(result.corrected_forecast.baseline.artifact_id)
                ),
                "contribution_manifest": (
                    str(replayed.atomic_forecast.contribution_manifest.artifact_id)
                    == str(result.atomic_forecast.contribution_manifest.artifact_id)
                ),
                "verification_report": (
                    str(replayed.verification.report.artifact_id)
                    == str(result.verification.report.artifact_id)
                ),
                "matched_pairs": (
                    str(replayed.matching.matched_pairs.artifact_id)
                    == str(result.matching.matched_pairs.artifact_id)
                ),
                "aligned_guidance": (
                    str(replayed.alignment.aligned_guidance.artifact_id)
                    == str(result.alignment.aligned_guidance.artifact_id)
                ),
            },
            "identical_content_digests": {
                "baseline_forecast": (
                    str(replayed.corrected_forecast.baseline.content_digest)
                    == str(result.corrected_forecast.baseline.content_digest)
                ),
                "contribution_manifest": (
                    str(replayed.atomic_forecast.contribution_manifest.content_digest)
                    == str(result.atomic_forecast.contribution_manifest.content_digest)
                ),
                "verification_report": (
                    str(replayed.verification.report.content_digest)
                    == str(result.verification.report.content_digest)
                ),
            },
        }
        replay_report["all_identical"] = all(
            all(section.values())
            for section in (
                replay_report["identical_artifact_ids"],
                replay_report["identical_content_digests"],
            )
        )
        print(f"[phase2] replay identical: {replay_report['all_identical']}", flush=True)

    bundle = {
        "schema_version": "phase2-live-demo-export.v1",
        "run_id": str(result.run.run_id),
        "generated_at": datetime.now(UTC).isoformat(),
        "domain": {
            "name": "Grasston, Minnesota",
            "center_latitude": 45.80265,
            "center_longitude": -93.07956,
            "note": (
                "Grasston is the named domain center. baseline-forecast.v2 is a frozen "
                "Phase 2 contract whose output locations are exactly the three preserved "
                "METAR stations; the center point itself is not an output location."
            ),
        },
        "canonical_units": CANONICAL_UNITS,
        "run_spec": run_spec,
        "station_snapshot": station_snapshot,
        "cycle_selection": cycle_selection,
        "availability_report": availability,
        "comparison_rows": comparison,
        "source_inventory": inventory,
        "matched_pairs": matched_pairs,
        "verification_report": verification,
        "artifact_identities": {
            "phase2_run_spec": _identity(result.selected_inputs.run_spec),
            "station_catalog_snapshot": _identity(result.selected_inputs.station_snapshot),
            "aligned_station_guidance": _identity(result.alignment.aligned_guidance),
            "model_cycle_selection": _identity(result.availability.cycle_selection),
            "model_availability_report": _identity(result.availability.report),
            "uncorrected_blend_forecast": _identity(result.atomic_forecast.uncorrected_blend),
            "blend_contribution_manifest": _identity(result.atomic_forecast.contribution_manifest),
            "identity_bias_correction": _identity(result.corrected_forecast.correction),
            "baseline_forecast": _identity(result.corrected_forecast.baseline),
            "normalized_metar_observations": _identity(result.observations.normalized),
            "matched_pairs": _identity(result.matching.matched_pairs),
            "verification_report": _identity(result.verification.report),
            "metar_responses": [_identity(m) for m in result.observations.responses],
            "canonical_guidance": [_identity(m) for m in result.normalized.artifacts],
        },
        "baseline_variables": sorted(str(name) for name in baseline.data_vars),
        "replay": replay_report,
    }

    json_path = output_dir / "phase2-live-minnesota.json"
    json_path.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")

    comparison_csv = output_dir / "phase2-live-minnesota-comparison.csv"
    _write_csv(comparison_csv, comparison)

    inventory_csv = output_dir / "phase2-live-minnesota-source-inventory.csv"
    _write_csv(inventory_csv, inventory)

    markdown_path = output_dir / "phase2-live-minnesota.md"
    markdown_path.write_text(
        _markdown(
            result=result,
            run_spec=run_spec,
            comparison=comparison,
            availability=availability,
            verification=verification,
            matched_pairs=matched_pairs,
            inventory=inventory,
        ),
        encoding="utf-8",
    )

    print(f"[phase2] wrote {markdown_path}", flush=True)
    print(f"[phase2] wrote {json_path}", flush=True)
    print(f"[phase2] wrote {comparison_csv}", flush=True)
    print(f"[phase2] wrote {inventory_csv}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - operational entrypoint
    raise SystemExit(main())
