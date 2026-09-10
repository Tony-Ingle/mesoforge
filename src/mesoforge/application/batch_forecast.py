"""Forecast a JSON coordinate list from one existing 36-hour prepared dataset."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from mesoforge.application.point_forecast import PreparedPointForecast, UnsupportedCoordinateError


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not supported: {value}")


def _json_float(value: str) -> float | str:
    number = float(value)
    # Preserve an overflowing numeral as text in its location error, not JSON Infinity.
    return number if math.isfinite(number) else value


def _coordinates(location: object) -> tuple[float, float]:
    message = "Each location must contain only finite numeric lat and lon."
    if not isinstance(location, dict) or set(location) != {"lat", "lon"}:
        raise ValueError(message)
    if any(type(location[key]) not in (int, float) for key in ("lat", "lon")):
        raise ValueError(message)
    try:
        latitude, longitude = float(location["lat"]), float(location["lon"])
    except OverflowError as exc:
        raise ValueError(message) from exc
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise ValueError(message)
    return latitude, longitude


def run_batch(config_path: Path, data_dir: Path) -> dict[str, Any]:
    """Load guidance once and preserve ordered successes/errors without writing data."""
    config = json.loads(
        config_path.read_text(encoding="utf-8-sig"),
        parse_constant=_reject_constant,
        parse_float=_json_float,
    )
    if (
        not isinstance(config, dict)
        or set(config) != {"locations"}
        or not isinstance(config["locations"], list)
    ):
        raise ValueError("Config must be a JSON object containing a locations list.")

    prepared = PreparedPointForecast.from_directory(data_dir)
    if prepared.horizon_hours != tuple(range(1, 37)):
        raise ValueError("Batch forecasts require an existing dataset for hours 1..36.")

    results: list[dict[str, Any]] = []
    for index, location in enumerate(config["locations"]):
        result: dict[str, Any] = {"index": index, "location": location}
        try:
            latitude, longitude = _coordinates(location)
        except ValueError as exc:
            result.update(status="error", error={"code": "invalid_location", "message": str(exc)})
        else:
            try:
                forecast = prepared.forecast(latitude=latitude, longitude=longitude)
            except UnsupportedCoordinateError as exc:
                result.update(
                    status="error", error={"code": "unsupported_coordinate", "message": str(exc)}
                )
            except Exception as exc:
                # Isolate a calculation failure to this location; interrupts still propagate.
                result.update(
                    status="error",
                    error={"code": "forecast_failed", "message": f"{type(exc).__name__}: {exc}"},
                )
            else:
                result.update(status="ok", forecast=forecast)
        results.append(result)
    return {"results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="JSON locations list.")
    parser.add_argument("--data-dir", type=Path, required=True, help="Existing 36-hour snapshot.")
    args = parser.parse_args(argv)
    try:
        payload = run_batch(args.config, args.data_dir)
        output = json.dumps(payload, indent=2, allow_nan=False)
    except Exception as exc:
        print(
            json.dumps({"error": {"code": "batch_failed", "message": str(exc)}}),
            file=sys.stderr,
        )
        return 2
    print(output)
    return 1 if any(result["status"] == "error" for result in payload["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
