"""``aligned-station-guidance.v1`` assembly (plan Section 3.1-3.3, Task
6): combine bilinear native-grid extraction (``alignment.spatial``)
with exact temporal matching (``alignment.temporal``) to produce one
hourly per-station frame per model.

Pure NumPy/xarray computation over already-normalized
``canonical-guidance.v2`` datasets (``guidance.canonical_v2``) -- no
storage/network import.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyproj
import xarray as xr

from mesoforge.alignment.spatial import (
    PointExtractionError,
    bilinear_interpolate,
    project_station_point,
)
from mesoforge.alignment.temporal import (
    TemporalAlignmentError,
    find_exact_interval_index,
    find_exact_valid_time_index,
)
from mesoforge.common.errors import MesoForgeError

_INTERVAL_VARIABLE_IDS = frozenset(
    {"liquid_equivalent_precipitation_amount_1h", "probability_of_precipitation_1h"}
)


class StationAlignmentError(MesoForgeError):
    """Raised when a station cannot be aligned to a model's native grid
    (no coverage, non-finite corner) or temporally matched (missing
    source hour) -- Section 3.3: 'a missing source hour makes that
    model unavailable for the entire run'."""


@dataclass(frozen=True, slots=True)
class AlignedValue:
    value: float
    source_y0: int
    source_y1: int
    source_x0: int
    source_x1: int
    station_projected_x: float
    station_projected_y: float
    weights: tuple[float, float, float, float]
    source_lead_hour: int
    quality_bits: int


def align_station_to_model(
    dataset: xr.Dataset,
    *,
    crs: pyproj.CRS,
    station_latitude: float,
    station_longitude: float,
    canonical_variable_id: str,
    target_horizon_hours: tuple[int, ...],
    target_reference_time: np.datetime64,
) -> dict[int, AlignedValue]:
    """Section 3.1-3.3: for one station and one model's one canonical
    variable, extract every requested target horizon's value via
    native-grid bilinear interpolation at the exact matching source
    valid time (instantaneous) or interval (accumulation/probability).

    Returns ``{target_horizon: AlignedValue}``. A target horizon with
    no exact matching source hour is simply absent from the result
    (the caller's availability evaluation treats a missing key as
    unavailable for that horizon, per Section 3.3's 'model unavailable
    for the entire run' completeness rule enforced upstream at
    cycle-selection time -- this function only reports what it could
    align).
    """
    station_x, station_y = project_station_point(
        crs, latitude=station_latitude, longitude=station_longitude
    )
    x = dataset["x"].values
    y = dataset["y"].values
    lead_hours = np.asarray(
        dataset["source_lead_time"].values / np.timedelta64(1, "h"), dtype=np.int64
    )

    is_interval = canonical_variable_id in _INTERVAL_VARIABLE_IDS
    field = dataset[canonical_variable_id].values

    results: dict[int, AlignedValue] = {}
    for horizon in target_horizon_hours:
        target_valid_time = target_reference_time + np.timedelta64(horizon, "h")
        try:
            if is_interval:
                bounds = dataset[f"{canonical_variable_id}_interval_bounds"].values
                lead_index = find_exact_interval_index(
                    bounds[:, 0],
                    bounds[:, 1],
                    target_start=target_valid_time - np.timedelta64(1, "h"),
                    target_end=target_valid_time,
                )
            else:
                source_valid_times = dataset["source_valid_time"].values
                lead_index = find_exact_valid_time_index(
                    source_valid_times, target_valid_time=target_valid_time
                )
        except TemporalAlignmentError:
            continue

        try:
            extracted = bilinear_interpolate(
                field=field[lead_index],
                x=x,
                y=y,
                station_x=station_x,
                station_y=station_y,
            )
        except PointExtractionError as exc:
            raise StationAlignmentError(
                f"station extraction failed for {canonical_variable_id!r} at horizon "
                f"{horizon!r}: {exc}"
            ) from exc

        results[horizon] = AlignedValue(
            value=extracted.value,
            source_y0=extracted.cell.y0,
            source_y1=extracted.cell.y1,
            source_x0=extracted.cell.x0,
            source_x1=extracted.cell.x1,
            station_projected_x=station_x,
            station_projected_y=station_y,
            weights=(
                extracted.weights.w00,
                extracted.weights.w01,
                extracted.weights.w10,
                extracted.weights.w11,
            ),
            source_lead_hour=int(lead_hours[lead_index]),
            quality_bits=0,
        )

    return results
