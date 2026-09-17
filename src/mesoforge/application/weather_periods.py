"""Read-only local-period summary of one exact saved issuance's evolution."""

from __future__ import annotations

import argparse
import hashlib
import sys
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from mesoforge.application.weather_transitions import preview_weather_transitions
from mesoforge.common.errors import NotFound
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.forecasting import periods
from mesoforge.forecasting.conditions import ConditionsPreviewUnavailableError
from mesoforge.forecasting.periods import PERIOD_POLICY, TEMPLATE_VERSION, build_period_summary


@lru_cache(maxsize=1)
def _derivation_identity() -> dict[str, Any]:
    sources = {
        "forecasting/periods.py": Path(periods.__file__),
        "application/weather_periods.py": Path(__file__),
    }
    return {
        "period_policy_id": PERIOD_POLICY["id"],
        "template_version": TEMPLATE_VERSION,
        "source_sha256": {
            name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()
        },
    }


def preview_weather_periods(
    issued_forecast_id: UUID, *, display_timezone: str | None = None
) -> dict[str, Any]:
    """Reuse the transition preview (one saved read) and group its facts into periods."""
    transitions = preview_weather_transitions(issued_forecast_id, display_timezone=display_timezone)
    result = build_period_summary(transitions)
    result["derivation"] = {
        **deepcopy(_derivation_identity()),
        "transitions": deepcopy(transitions["derivation"]),
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issued-forecast-id", type=UUID, required=True)
    parser.add_argument(
        "--display-timezone",
        help="IANA zone for periods and rendered times; default is the saved report zone or UTC",
    )
    args = parser.parse_args(argv)
    try:
        payload = canonical_json_bytes(
            preview_weather_periods(args.issued_forecast_id, display_timezone=args.display_timezone)
        )
    except NotFound:
        error = {
            "code": "issued_forecast_not_found",
            "message": "No saved issued forecast exists for this ID.",
        }
    except ConditionsPreviewUnavailableError as exc:
        error = {"code": "periods_preview_unavailable", "message": str(exc)}
    except ValueError as exc:
        error = {"code": "invalid_display_timezone", "message": str(exc)}
    except Exception:
        error = {
            "code": "periods_preview_failed",
            "message": "Could not read and verify the saved period-summary inputs.",
        }
    else:
        sys.stdout.buffer.write(payload + b"\n")
        return 0
    print(canonical_json_bytes({"error": error}).decode("utf-8"), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
