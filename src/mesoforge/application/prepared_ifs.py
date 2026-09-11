"""Prepare a separate IFS temperature shadow snapshot for an existing control window.

This is an explicit pre-HTTP operation. Coordinates use the existing spatial planner;
one set of temperature messages supplies all regions. The active source snapshot is
never edited. Repeats and --from-raw do not construct a network transport.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from mesoforge.application.batch_forecast import load_locations
from mesoforge.application.prepared_shadow import normalize_shadow_temperature
from mesoforge.application.prepared_temperature import BoundedHttpTransport, _code_identity
from mesoforge.application.shadow_preparation import ShadowAdapter, prepare_shadow
from mesoforge.catalog.contributors import IFS_MODEL_DEFINITION, RAP_MODEL_DEFINITION
from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION, ContributorConfiguration
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.sources.ifs import (
    IFS_CAPABILITIES,
    acquire_ifs_lead,
    decode_temperature_message,
    discover_ifs_cycle,
)

IFS_CONFIGURATION = ContributorConfiguration(
    models=(*DEFAULT_CONFIGURATION.models, RAP_MODEL_DEFINITION, IFS_MODEL_DEFINITION),
    control_recipe=DEFAULT_CONFIGURATION.control_recipe,
    comparison_recipes=DEFAULT_CONFIGURATION.comparison_recipes,
)
_SOURCE_METADATA = {
    "model_definition": IFS_MODEL_DEFINITION.model_dump(mode="json"),
    "capabilities": json.loads(json.dumps(IFS_CAPABILITIES)),
    "adapter_version": "ifs_temperature_v1",
    "product": "IFS Open Data deterministic oper/fc, 0.25 degree, 2t",
    "temporal_policy": "Native three-hourly valid times only; no temporal interpolation",
}


def _identity() -> dict[str, Any]:
    identity = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for name in (
        "application/prepared_ifs.py",
        "application/shadow_preparation.py",
        "application/prepared_shadow.py",
        "guidance/sources/ifs.py",
    ):
        identity["source_sha256"][name] = hashlib.sha256((package / name).read_bytes()).hexdigest()
    return identity


def prepare_ifs(
    locations: list[Any],
    control_directory: Path,
    output_directory: Path,
    *,
    target_horizons: tuple[int, ...] = tuple(range(1, 37)),
    from_raw: Path | None = None,
    cycle_override: datetime | None = None,
    transport: HttpTransport | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
) -> dict[str, Any]:
    """Retain native IFS values separately; both IFS and RAP have zero active weight."""
    return prepare_shadow(
        locations,
        control_directory,
        output_directory,
        adapter=ShadowAdapter(
            model="IFS",
            source_metadata=_SOURCE_METADATA,
            configuration=IFS_CONFIGURATION,
            discover_cycle=discover_ifs_cycle,
            acquire_lead=acquire_ifs_lead,
            decode_message=decode_temperature_message,
            normalize=normalize_shadow_temperature,
            code_identity=_identity,
            transport_factory=BoundedHttpTransport,
        ),
        from_raw=from_raw,
        target_horizons=target_horizons,
        cycle_override=cycle_override,
        transport=transport,
        clock=clock,
        sleeper=sleeper,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Existing locations JSON")
    parser.add_argument(
        "--data-dir", type=Path, required=True, help="Existing real control snapshot"
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="Shadow data outside Git")
    parser.add_argument("--from-raw", type=Path, help="Rebuild offline from retained IFS evidence")
    parser.add_argument("--ifs-cycle", type=datetime.fromisoformat, help="Optional fixed UTC cycle")
    parser.add_argument(
        "--hours",
        nargs="+",
        type=int,
        default=list(range(1, 37)),
        help="Bounded target hours to acquire; only native IFS valid times are available",
    )
    args = parser.parse_args(argv)
    report = prepare_ifs(
        load_locations(args.config),
        args.data_dir,
        args.output_dir,
        from_raw=args.from_raw,
        cycle_override=args.ifs_cycle,
        target_horizons=tuple(args.hours),
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
