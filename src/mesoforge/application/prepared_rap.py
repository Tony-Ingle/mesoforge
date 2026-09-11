"""Prepare a separate RAP temperature shadow snapshot for an existing control window.

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
from mesoforge.application.shadow_preparation import (
    ShadowAdapter,
    prepare_shadow,
    retained_surface_mode,
)
from mesoforge.catalog.contributors import RAP_MODEL_DEFINITION
from mesoforge.forecasting.recipes import (
    DEFAULT_CONFIGURATION,
    ContributorConfiguration,
    with_surface_fields,
)
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.sources.rap import (
    RAP_CAPABILITIES,
    SURFACE_FIELD_CONTRACTS,
    acquire_rap_lead,
    decode_surface_message,
    decode_temperature_message,
    discover_rap_cycle,
)

RAP_CONFIGURATION = ContributorConfiguration(
    models=(*DEFAULT_CONFIGURATION.models, RAP_MODEL_DEFINITION),
    control_recipe=DEFAULT_CONFIGURATION.control_recipe,
    comparison_recipes=DEFAULT_CONFIGURATION.comparison_recipes,
)
_SOURCE_METADATA = {
    "model_definition": RAP_MODEL_DEFINITION.model_dump(mode="json"),
    "capabilities": json.loads(json.dumps(RAP_CAPABILITIES)),
    "adapter_version": "rap_temperature_v1",
    "product": "awp130pgrb",
    "scientific_version": "RAPv5",
    "documented_production_package": "rap.v5.1.24",
    "version_note": "Documented NOAA package, not a patch version asserted by each GRIB message.",
    "metadata_sources": [
        "https://www.nco.ncep.noaa.gov/pmb/products/rap/",
        "https://www.nco.ncep.noaa.gov/pmb/codes/nwprod/",
        "https://www.weather.gov/media/notification/pdf2/scn20-46rap_v5_hrrr_v4_aab.pdf",
    ],
}


def _identity() -> dict[str, Any]:
    identity = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for name in (
        "application/prepared_rap.py",
        "application/shadow_preparation.py",
        "application/prepared_shadow.py",
        "guidance/sources/rap.py",
    ):
        identity["source_sha256"][name] = hashlib.sha256((package / name).read_bytes()).hexdigest()
    return identity


def prepare_rap(
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
    surface_fields: bool = False,
    canonical_variables_by_lead: dict[int, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """Acquire once, or rebuild/reuse retained RAP independently of the control files."""
    surface_fields = surface_fields or retained_surface_mode(from_raw or output_directory)
    configuration = with_surface_fields(RAP_CONFIGURATION) if surface_fields else RAP_CONFIGURATION
    metadata = (
        {
            **_SOURCE_METADATA,
            "model_definition": configuration.model_map()["RAP"].model_dump(mode="json"),
            "adapter_version": "rap_surface_v1",
        }
        if surface_fields
        else _SOURCE_METADATA
    )
    return prepare_shadow(
        locations,
        control_directory,
        output_directory,
        adapter=ShadowAdapter(
            model="RAP",
            source_metadata=metadata,
            configuration=configuration,
            discover_cycle=discover_rap_cycle,
            acquire_lead=acquire_rap_lead,
            decode_message=decode_temperature_message,
            decode_surface_message=decode_surface_message,
            surface_variables=tuple(SURFACE_FIELD_CONTRACTS),
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
        surface_fields=surface_fields,
        canonical_variables_by_lead=canonical_variables_by_lead,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Existing locations JSON")
    parser.add_argument(
        "--data-dir", type=Path, required=True, help="Existing real control snapshot"
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="Shadow data outside Git")
    parser.add_argument("--from-raw", type=Path, help="Rebuild offline from retained RAP evidence")
    parser.add_argument("--rap-cycle", type=datetime.fromisoformat, help="Optional fixed UTC cycle")
    parser.add_argument(
        "--hours",
        nargs="+",
        type=int,
        default=list(range(1, 37)),
        help="Bounded target hours to acquire; all other shadow hours remain explicitly missing",
    )
    args = parser.parse_args(argv)
    report = prepare_rap(
        load_locations(args.config),
        args.data_dir,
        args.output_dir,
        from_raw=args.from_raw,
        cycle_override=args.rap_cycle,
        target_horizons=tuple(args.hours),
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
