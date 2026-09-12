"""Build and retain small surface grids offline from an existing selected preparation."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

from mesoforge.application.batch_forecast import (
    _coordinates,
    load_locations,
    validate_current_control,
)
from mesoforge.application.local_surface_grid import extract_grid_point
from mesoforge.application.spatial_coverage import CoverageRequiredError, validate_coordinate
from mesoforge.application.spatial_preparation import PreparedRegions, load_prepared
from mesoforge.common.identifiers import Digest
from mesoforge.contracts.serialization import canonical_json_bytes, parse_canonical_json
from mesoforge.forecasting.recipes import ContributorConfiguration


def _retain(path: Path, payload: bytes) -> bool:
    """Never overwrite a retained baseline; repeat builds must reproduce its bytes."""
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"Retained local-grid artifact differs: {path}")
        return True
    with path.open("xb") as output:
        output.write(payload)
    return False


def prepare_local_grids(
    config_path: Path, prepared_run: Path, output_directory: Path
) -> dict[str, Any]:
    """Reuse one existing selected model set; no acquisition, decoding or database writes.

    The selected preparation report already identifies model registration and shadow
    views. The only geographic input is the existing locations JSON. This command
    cannot expand missing native coverage: run guidance preparation first if needed.
    """
    output_directory = output_directory.resolve()
    if output_directory.is_relative_to(Path(__file__).resolve().parents[3]):
        raise ValueError("Retained local forecast grids must remain outside Git")
    locations = load_locations(config_path)
    preparation = json.loads((prepared_run / "preparation.json").read_text(encoding="utf-8"))
    selection = preparation["current_model_set"]["selection"]
    if not selection.get("surface_fields"):
        raise ValueError("Local surface grids require a surface-field selected preparation")
    # The exact registration used by discovery is retained in the selection artifact.
    configuration = ContributorConfiguration.model_validate_json(
        json.dumps(selection["contributor_configuration"])
    )
    validate_current_control(configuration)
    start = perf_counter()
    prepared = load_prepared(
        Path(preparation["directory"]),
        configuration=configuration,
        shadow_directories={
            model: Path(path) for model, path in preparation["shadow_directories"].items()
        },
        **({"pop_guidance": preparation["pop_guidance"]} if "pop_guidance" in preparation else {}),
        **(
            {"probability_sources": preparation["probability_sources"]}
            if "probability_sources" in preparation
            else {}
        ),
        **(
            {"ptype_guidance": preparation["ptype_guidance"]}
            if "ptype_guidance" in preparation
            else {}
        ),
        **(
            {"snowfall_guidance": preparation["snowfall_guidance"]}
            if "snowfall_guidance" in preparation
            else {}
        ),
        **(
            {"snowfall_amount_guidance": preparation["snowfall_amount_guidance"]}
            if "snowfall_amount_guidance" in preparation
            else {}
        ),
        **(
            {"cloud_guidance": preparation["cloud_guidance"]}
            if "cloud_guidance" in preparation
            else {}
        ),
    )
    if prepared.horizon_hours != tuple(range(1, 37)):
        raise ValueError("Local surface grids require prepared hours 1..36")
    regions = prepared.regions if isinstance(prepared, PreparedRegions) else [prepared]
    if any(
        region._surface_configuration is None
        or (region._manifest or {}).get("current_model_set") != preparation["current_model_set"]
        for region in regions
    ):
        raise ValueError("Prepared surface regions differ from the selected model-set evidence")
    loaded_seconds = perf_counter() - start
    output_directory.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    seen: dict[tuple[float, float], dict[str, Any]] = {}
    for index, location in enumerate(locations):
        result: dict[str, Any] = {"index": index, "location": location}
        try:
            latitude, longitude = _coordinates(location)
            validate_coordinate(latitude, longitude)
            coordinate = latitude, longitude
            if coordinate in seen:
                result.update(seen[coordinate], duplicate_coordinate_reused=True)
            else:
                start = perf_counter()
                forecast = prepared.forecast(latitude=latitude, longitude=longitude)
                grid = forecast["local_grid_baseline"]
                payload = canonical_json_bytes(grid)
                digest = Digest.of_bytes(payload)
                compressed = gzip.compress(payload, mtime=0)
                filename = f"{str(digest).removeprefix('sha256:')}.json.gz"
                reused = _retain(output_directory / filename, compressed)
                entry = {
                    "latitude": latitude,
                    "longitude": longitude,
                    "file": filename,
                    "sha256": str(digest),
                    "compressed_sha256": str(Digest.of_bytes(compressed)),
                }
                entries.append(entry)
                result.update(
                    status="ok",
                    grid=forecast["local_grid"],
                    artifact=entry,
                    retained_artifact_reused=reused,
                    grid_bytes=len(payload),
                    compressed_grid_bytes=len(compressed),
                    build_and_retain_seconds=perf_counter() - start,
                    forecast_hours=len(forecast["hours"]),
                )
                seen[coordinate] = {
                    key: value for key, value in result.items() if key not in ("index", "location")
                }
        except (ValueError, KeyError, OSError) as exc:
            result.update(status="error", error={"code": "local_grid_failed", "message": str(exc)})
        results.append(result)
    index_payload = {"version": "local-surface-grid-index.v1", "grids": entries}
    _retain(output_directory / "local-grids.json", canonical_json_bytes(index_payload))
    return {
        "directory": str(output_directory.resolve()),
        "guidance_load_seconds": loaded_seconds,
        "guidance_loads": 1,
        "downloaded_bytes": 0,
        "selected_cycles": selection["selected_cycles"],
        "results": results,
    }


@dataclass(frozen=True)
class PreparedLocalGrids:
    """Verified local baselines loaded once at HTTP startup; no calculation or writes."""

    grids: dict[tuple[float, float], dict[str, Any]]

    @classmethod
    def from_directory(cls, directory: Path) -> PreparedLocalGrids:
        index = json.loads((directory / "local-grids.json").read_text())
        if index.get("version") != "local-surface-grid-index.v1":
            raise ValueError("Unsupported local surface-grid index")
        grids: dict[tuple[float, float], dict[str, Any]] = {}
        for row in index["grids"]:
            name = row["file"]
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError("Local-grid index must name an artifact in its directory")
            compressed = (directory / name).read_bytes()
            if str(Digest.of_bytes(compressed)) != row["compressed_sha256"]:
                raise ValueError("Retained local-grid compressed checksum differs")
            payload = gzip.decompress(compressed)
            if str(Digest.of_bytes(payload)) != row["sha256"]:
                raise ValueError("Retained local-grid checksum differs")
            grid = parse_canonical_json(payload)
            latitude, longitude = row["latitude"], row["longitude"]
            extract_grid_point(grid, latitude=latitude, longitude=longitude)
            if (latitude, longitude) in grids:
                raise ValueError("Duplicate configured coordinate in local-grid index")
            grids[latitude, longitude] = grid
        if not grids:
            raise ValueError("No successful local surface grids in this preparation")
        return cls(grids)

    @property
    def data_kind(self) -> str:
        return str(next(iter(self.grids.values()))["forecast_context"]["data_kind"])

    @property
    def notice(self) -> str:
        return str(next(iter(self.grids.values()))["forecast_context"]["notice"])

    def forecast(self, *, latitude: float, longitude: float) -> dict[str, Any]:
        validate_coordinate(latitude, longitude)
        grid = self.grids.get((latitude, longitude))
        if grid is None:
            raise CoverageRequiredError(
                "Prepare a local surface grid for this exact coordinate before HTTP; "
                "this milestone extracts configured center nodes only"
            )
        return extract_grid_point(grid, latitude=latitude, longitude=longitude)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Existing locations JSON")
    parser.add_argument(
        "--prepared-run",
        required=True,
        type=Path,
        help="Existing selected preparation directory containing preparation.json",
    )
    parser.add_argument("--output-dir", required=True, type=Path, help="Retain grids outside Git")
    args = parser.parse_args(argv)
    try:
        result = prepare_local_grids(args.config, args.prepared_run, args.output_dir)
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return int(any(row["status"] != "ok" for row in result["results"]))


if __name__ == "__main__":
    raise SystemExit(main())
