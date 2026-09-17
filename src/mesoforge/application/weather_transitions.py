"""Read-only weather evolution over the forecast point of one exact saved issuance."""

from __future__ import annotations

import argparse
import hashlib
import sys
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from mesoforge.application.issuance import read_issued_forecast
from mesoforge.application.weather_conditions import _derivation_identity as _conditions_identity
from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.forecasting import transitions
from mesoforge.forecasting.conditions import (
    ConditionsPreviewUnavailableError,
    build_conditions_preview,
)
from mesoforge.forecasting.transitions import TEMPLATE_VERSION, TRANSITION_POLICY, build_transitions


def validate_display_timezone(name: str) -> str:
    """Accept only a resolvable IANA zone; presentation never changes stored UTC times."""
    try:
        ZoneInfo(name)
    except (KeyError, ValueError, OSError) as exc:
        raise ValueError(f"Unknown display timezone {name!r}") from exc
    return name


@lru_cache(maxsize=1)
def _derivation_identity() -> dict[str, Any]:
    sources = {
        "forecasting/transitions.py": Path(transitions.__file__),
        "application/weather_transitions.py": Path(__file__),
    }
    return {
        "transition_policy_id": TRANSITION_POLICY["id"],
        "template_version": TEMPLATE_VERSION,
        "conditions": deepcopy(_conditions_identity()),
        "source_sha256": {
            name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()
        },
    }


def resolve_display_timezone(saved: dict[str, Any], requested: str | None) -> tuple[str, str]:
    """A request wins; otherwise the issuance's saved report zone; otherwise UTC."""
    if requested is not None:
        return validate_display_timezone(requested), "request"
    report = saved.get("forecast", {}).get("hourly_report")
    zone = report.get("display_timezone") if isinstance(report, dict) else None
    if isinstance(zone, str) and zone:
        return validate_display_timezone(zone), "issuance_hourly_report"
    return "UTC", "default_utc"


def preview_weather_transitions(
    issued_forecast_id: UUID, *, display_timezone: str | None = None
) -> dict[str, Any]:
    """Read one saved version once, describe its point hours, then detect evolution."""
    if display_timezone is not None:
        validate_display_timezone(display_timezone)
    saved = read_issued_forecast(issued_forecast_id)
    if saved.get("issued_forecast_id") != str(issued_forecast_id):
        raise IntegrityError("Readback returned a different issued forecast")
    preview = build_conditions_preview(saved, scope="point")
    zone, source = resolve_display_timezone(saved, display_timezone)
    result = build_transitions(preview, display_timezone=zone, timezone_source=source)
    result["derivation"] = deepcopy(_derivation_identity())
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issued-forecast-id", type=UUID, required=True)
    parser.add_argument(
        "--display-timezone",
        help="IANA zone for rendered times; default is the issuance's saved report zone or UTC",
    )
    args = parser.parse_args(argv)
    try:
        payload = canonical_json_bytes(
            preview_weather_transitions(
                args.issued_forecast_id, display_timezone=args.display_timezone
            )
        )
    except NotFound:
        error = {
            "code": "issued_forecast_not_found",
            "message": "No saved issued forecast exists for this ID.",
        }
    except ConditionsPreviewUnavailableError as exc:
        error = {"code": "transitions_preview_unavailable", "message": str(exc)}
    except ValueError as exc:
        error = {"code": "invalid_display_timezone", "message": str(exc)}
    except Exception:
        error = {
            "code": "transitions_preview_failed",
            "message": "Could not read and verify the saved transition-preview inputs.",
        }
    else:
        sys.stdout.buffer.write(payload + b"\n")
        return 0
    print(canonical_json_bytes({"error": error}).decode("utf-8"), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
