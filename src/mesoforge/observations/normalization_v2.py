"""Phase 2 METAR normalization extension (plan Section 6.1, Task 11):
dew point (C->K), explicit wind gust (knots->m/s), and raw routine
``Prrrr`` hourly liquid precipitation (hundredths of an inch -> kg/m2).

Pure computation -- no network/storage import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from mesoforge.common.errors import MesoForgeError

_KNOTS_TO_M_S = 0.5144444444444445
_INCH_HUNDREDTHS_TO_MM = 0.254  # 0.01 inch * 25.4 mm/inch
_PRRRR_PATTERN = re.compile(r"(?<![A-Za-z0-9])P(\d{4})(?![A-Za-z0-9])")


class PrecipitationParsingError(MesoForgeError):
    """Raised when a syntactically present ``Prrrr``-shaped token
    cannot be safely parsed (should not normally occur given the
    regex; retained for defense-in-depth)."""


def convert_dew_point_c_to_k(dew_point_degc: float) -> float:
    return dew_point_degc + 273.15


def convert_gust_knots_to_m_s(gust_knots: float) -> float:
    return gust_knots * _KNOTS_TO_M_S


@dataclass(frozen=True, slots=True)
class PrecipitationTruth:
    status: str  # "reported" | "missing" | "malformed"
    amount_kg_m2: float | None
    interval_start: datetime | None
    interval_end: datetime | None


def extract_hourly_precipitation(
    *, raw_ob: str, metar_type: str, report_time: datetime
) -> PrecipitationTruth:
    """Section 6.1: accept only a non-SPECI routine report with an
    explicit syntactically valid ``Prrrr`` group representing the hour
    ending at the report time. ``rrrr`` is hundredths of an inch;
    convert ``rrrr * 0.01 inch * 25.4 = rrrr * 0.254`` to kg/m2 (kg/m2
    numerically equals mm of liquid water depth). Missing ``Prrrr`` is
    missing truth, never zero. A SPECI report never contributes
    precipitation truth even if it happens to carry a ``Prrrr`` group.
    """
    if metar_type.upper() == "SPECI":
        return PrecipitationTruth(
            status="missing", amount_kg_m2=None, interval_start=None, interval_end=None
        )

    matches = _PRRRR_PATTERN.findall(raw_ob)
    if not matches:
        return PrecipitationTruth(
            status="missing", amount_kg_m2=None, interval_start=None, interval_end=None
        )
    if len(matches) > 1:
        return PrecipitationTruth(
            status="malformed", amount_kg_m2=None, interval_start=None, interval_end=None
        )

    hundredths = int(matches[0])
    amount_mm = hundredths * _INCH_HUNDREDTHS_TO_MM
    interval_end = report_time
    interval_start = report_time - timedelta(hours=1)
    return PrecipitationTruth(
        status="reported",
        amount_kg_m2=amount_mm,
        interval_start=interval_start,
        interval_end=interval_end,
    )
