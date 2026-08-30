"""HRRR normalization completeness/scientific validation (plan Section
3.5/3.6, Task 5).

Phase 1 tightening on top of the existing generic
``contracts.datasets.validate_canonical_dataset``: exact lead axis,
no missing lead/field/coordinate/halo, and that every quality mask is
entirely zero (Phase 1's canonical guidance never accepts a partial
artifact -- missingness fails normalization closed before
registration, per plan Section 3.5's "no missing lead, field,
coordinate, or interpolation halo is accepted for a valid Phase 1
artifact").
"""

from __future__ import annotations

import numpy as np
import xarray as xr

from mesoforge.common.errors import MesoForgeError

_EXPECTED_LEAD_HOURS = tuple(range(7))
_EXPECTED_VARIABLES = ("air_temperature_2m", "eastward_wind_10m", "northward_wind_10m")


class Phase1GuidanceValidationError(MesoForgeError):
    """Raised when a canonical HRRR guidance dataset fails a Phase 1
    completeness/scientific check."""


def validate_phase1_hrrr_guidance(dataset: xr.Dataset) -> None:
    errors: list[str] = []

    lead_time = dataset.get("lead_time")
    if lead_time is None:
        errors.append("missing lead_time coordinate")
    else:
        actual_hours = tuple(int(v / np.timedelta64(1, "h")) for v in lead_time.values)
        if actual_hours != _EXPECTED_LEAD_HOURS:
            errors.append(
                f"lead_time must be exactly {_EXPECTED_LEAD_HOURS!r} hours, got {actual_hours!r}"
            )

    for variable_id in _EXPECTED_VARIABLES:
        if variable_id not in dataset.data_vars:
            errors.append(f"missing required data variable {variable_id!r}")
            continue
        data_array = dataset[variable_id]
        values = data_array.values
        if not np.all(np.isfinite(values.astype(np.float64))):
            errors.append(
                f"data variable {variable_id!r} contains non-finite values; Phase 1 requires "
                "every field present and valid (no missing lead/field/coordinate/halo)"
            )
        mask_name = f"{variable_id}_quality_mask"
        if mask_name in dataset.data_vars:
            mask_values = dataset[mask_name].values
            if np.any(mask_values != 0):
                errors.append(
                    f"quality mask {mask_name!r} has nonzero bits; Phase 1 canonical guidance "
                    "never accepts a partial/masked artifact"
                )

    if "latitude" not in dataset.coords or "longitude" not in dataset.coords:
        errors.append("missing latitude/longitude 2-D coordinates")

    if errors:
        raise Phase1GuidanceValidationError(
            f"Phase 1 HRRR guidance validation failed with {len(errors)} problem(s): "
            + "; ".join(errors)
        )
