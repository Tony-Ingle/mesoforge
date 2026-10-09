"""The existing configured-location registry, readable by stdlib host orchestration."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from mesoforge.common.email_address import address


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not supported: {value}")


def _json_float(value: str) -> float | str:
    number = float(value)
    # Preserve an overflowing numeral as text in its location error, not JSON Infinity.
    return number if math.isfinite(number) else value


def load_locations(config_path: Path) -> list[Any]:
    """Read the canonical registry; per-location validation stays failure-isolated."""
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
    return config["locations"]


def location_email_recipients(location: dict[str, Any]) -> list[str]:
    """Zero or more recipients, normalized/deduplicated in first-appearance order.

    Missing is the historical no-delivery default. Recipient metadata never selects
    forecast guidance or changes meteorological values. No global recipient fallback.
    """
    values = location.get("email_recipients", [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("email_recipients must be a list of plain email addresses")
    recipients = []
    for value in values:
        normalized = address(value)
        if normalized not in recipients:
            recipients.append(normalized)
    return recipients
