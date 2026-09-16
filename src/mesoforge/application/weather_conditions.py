"""Read-only condition preview from one exact saved numerical forecast grid."""

from __future__ import annotations

import argparse
import hashlib
import sys
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from mesoforge.application.issuance import read_issued_forecast
from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.forecasting import cloud_cover, condition_wording, conditions
from mesoforge.forecasting.conditions import (
    RULESET_ID,
    TEMPLATE_VERSION,
    ConditionsPreviewUnavailableError,
    build_conditions_preview,
)


@lru_cache(maxsize=1)
def _derivation_identity() -> dict[str, Any]:
    """Identify this preview and renderer independently of the saved issuance code."""
    sources = {
        "forecasting/conditions.py": Path(conditions.__file__),
        "forecasting/condition_wording.py": Path(condition_wording.__file__),
        "forecasting/cloud_cover.py": Path(cloud_cover.__file__),
        "application/weather_conditions.py": Path(__file__),
    }
    return {
        "ruleset_id": RULESET_ID,
        "template_version": TEMPLATE_VERSION,
        "source_sha256": {
            name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()
        },
    }


def preview_weather_conditions(issued_forecast_id: UUID) -> dict[str, Any]:
    """Read verified saved bytes once; derive a preview without generation or persistence."""
    saved = read_issued_forecast(issued_forecast_id)
    if saved.get("issued_forecast_id") != str(issued_forecast_id):
        raise IntegrityError("Readback returned a different issued forecast")
    result = build_conditions_preview(saved)
    result["derivation"] = deepcopy(_derivation_identity())
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issued-forecast-id", type=UUID, required=True)
    args = parser.parse_args(argv)
    try:
        payload = canonical_json_bytes(preview_weather_conditions(args.issued_forecast_id))
    except NotFound:
        error = {
            "code": "issued_forecast_not_found",
            "message": "No saved issued forecast exists for this ID.",
        }
    except ConditionsPreviewUnavailableError as exc:
        error = {"code": "conditions_preview_unavailable", "message": str(exc)}
    except Exception:
        error = {
            "code": "conditions_preview_failed",
            "message": "Could not read and verify the saved condition-preview inputs.",
        }
    else:
        sys.stdout.buffer.write(payload + b"\n")
        return 0
    print(canonical_json_bytes({"error": error}).decode("utf-8"), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
