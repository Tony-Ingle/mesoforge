"""Model metadata for configured forecast contributors.

Capabilities describe the supported adapter envelope, not a promise that a
provider has every nominal cycle available. Status changes are explicit config
changes; acquiring or evaluating a model never promotes it automatically.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]*$")
    provider: str = Field(min_length=1)
    family: str = Field(min_length=1)
    lineage: tuple[str, ...] = ()
    domain: str = Field(min_length=1)
    supported_fields: tuple[str, ...]
    cycle_hours: tuple[int, ...]
    supported_leads: tuple[int, ...]
    status: Literal["shadow", "evaluated", "active", "deprecated", "retired"] = "shadow"
    grid_type: Literal["projected", "geographic", "either"] = "either"

    @field_validator("provider", "family", "domain")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("metadata must be nonblank without surrounding whitespace")
        return value

    @model_validator(mode="after")
    def _capabilities(self) -> ModelDefinition:
        for label, entries in (
            ("supported_fields", self.supported_fields),
            ("lineage", self.lineage),
        ):
            if len(entries) != len(set(entries)) or any(
                not entry.strip() or entry != entry.strip() for entry in entries
            ):
                raise ValueError(f"{label} must contain unique nonblank entries")
        if not self.supported_fields:
            raise ValueError("supported_fields must not be empty")
        for label, values in (
            ("cycle_hours", self.cycle_hours),
            ("supported_leads", self.supported_leads),
        ):
            if not values or tuple(sorted(set(values))) != values:
                raise ValueError(f"{label} must contain sorted unique values")
            if any(value < 0 for value in values):
                raise ValueError(f"{label} cannot contain negative values")
        if any(hour > 23 for hour in self.cycle_hours):
            raise ValueError("cycle_hours must be UTC hours from 0 through 23")
        return self


# These are the current local temperature adapter capabilities. The existing
# HrrrPhase2SourceSettings/GfsSourceSettings restrict their extended-cycle
# envelope to 00/06/12/18 UTC and source leads <=48; their other fields are not
# enabled by this local temperature configuration. No ancestry is inferred.
DEFAULT_MODEL_DEFINITIONS = (
    ModelDefinition(
        model_id="HRRR",
        provider="NOAA/NCEP",
        family="HRRR",
        domain="CONUS",
        supported_fields=("air_temperature_2m",),
        cycle_hours=(0, 6, 12, 18),
        supported_leads=tuple(range(49)),
        status="active",
        grid_type="projected",
    ),
    ModelDefinition(
        model_id="GFS",
        provider="NOAA/NCEP",
        family="GFS",
        domain="global",
        supported_fields=("air_temperature_2m",),
        cycle_hours=(0, 6, 12, 18),
        supported_leads=tuple(range(49)),
        status="active",
        grid_type="geographic",
    ),
)


# Optional temperature adapter; never automatically joins the active defaults.
# RAPv5 is the scientific system. The provider's deployment package version is
# separate acquisition metadata, not a guarantee encoded by every GRIB message.
RAP_MODEL_DEFINITION = ModelDefinition(
    model_id="RAP",
    provider="NOAA/NCEP",
    family="RAP/WRF-ARW",
    lineage=("Shared WRF-ARW/GSI lineage with HRRR",),
    domain="CONUS",
    supported_fields=("air_temperature_2m",),
    cycle_hours=tuple(range(24)),
    supported_leads=tuple(range(52)),
    status="shadow",
    grid_type="projected",
)

# Deterministic public IFS subset, preserving native three-hourly temperature.
# This registration describes the adapter envelope, not every ECMWF product.
IFS_MODEL_DEFINITION = ModelDefinition(
    model_id="IFS",
    provider="ECMWF Open Data",
    family="ECMWF IFS",
    lineage=("ECMWF Integrated Forecasting System deterministic forecast",),
    domain="global",
    supported_fields=("air_temperature_2m",),
    cycle_hours=(0, 6, 12, 18),
    supported_leads=tuple(range(0, 91, 3)),
    status="shadow",
    grid_type="geographic",
)

# Surface adapters are opt-in so retained temperature snapshots keep their exact
# original capability metadata. IFS's three-hour maximum gust is not the
# instantaneous surface-gust contract used by the other adapters.
_SURFACE_FIELDS = (
    "air_temperature_2m",
    "dew_point_temperature_2m",
    "eastward_wind_10m",
    "northward_wind_10m",
    "wind_gust_10m",
)
SURFACE_MODEL_FIELDS = {
    "HRRR": _SURFACE_FIELDS,
    "GFS": _SURFACE_FIELDS,
    "RAP": _SURFACE_FIELDS,
    "IFS": _SURFACE_FIELDS[:-1],
}
