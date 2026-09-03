"""Canonical Phase 2 multi-model guidance assembly (plan Section 5.1,
Task 3/4/5): ``canonical-guidance.v2`` -- one provider/cycle native grid
per model with scalar ``forecast_reference_time``, one-dimensional
``source_lead_time``/``source_valid_time``, native ``y``/``x``, model
ID, exact interval bounds, and model-specific variables.

Shared by HRRR/NBM/GFS normalization so all three models assemble an
identically-shaped dataset differing only in native grid and which
variables/units are populated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import numpy as np
import xarray as xr

from mesoforge.common.errors import MesoForgeError

ModelId = Literal["hrrr", "nbm", "gfs"]

# Phase 2 bounded canonical retention (owner architecture decision).
# ``canonical-guidance.v2`` no longer retains a model's full native grid:
# it retains the configured domain bbox plus the point-extraction policy
# halo, computed on the native grid with the same Phase 1 rule
# (``guidance.normalization.compute_bbox_halo_subset_indices``). The
# native grid identity, shape, and the exact retained index window are
# recorded as artifact attributes so the full source grid remains
# reconstructible and auditable from the retained evidence.
SUBSET_POLICY_ID = "bbox-halo-subset.v1"

_INSTANTANEOUS_VARIABLE_UNITS: dict[str, str] = {
    "air_temperature_2m": "K",
    "dew_point_temperature_2m": "K",
    "eastward_wind_10m": "m/s",
    "northward_wind_10m": "m/s",
    "wind_gust_10m": "m/s",
}
_INTERVAL_VARIABLE_UNITS: dict[str, str] = {
    "liquid_equivalent_precipitation_amount_1h": "kg/m^2",
    "probability_of_precipitation_1h": "1",
}


class CanonicalGuidanceV2Error(MesoForgeError):
    """Raised when a ``canonical-guidance.v2`` dataset fails assembly-
    time or validation-time invariants."""


@dataclass(frozen=True, slots=True)
class RetainedGridSubset:
    """The exact native-grid window a ``canonical-guidance.v2`` artifact
    retains, plus the full source grid it was cut from.

    Phase 2 produces values at three station points by native-grid
    bilinear interpolation, which reads a single 2x2 neighbourhood. The
    retained window is therefore the configured inclusive domain bbox
    plus ``halo_cells`` complete source cells on every side -- every
    source cell any approved station extraction can read -- and nothing
    else. ``source_ny``/``source_nx`` and the half-open
    ``[y_start, y_end) x [x_start, x_end)`` index bounds are retained so
    the artifact's grid can be placed back onto the provider's native
    grid exactly, without re-fetching it.
    """

    source_ny: int
    source_nx: int
    y_start: int
    y_end: int
    x_start: int
    x_end: int
    halo_cells: int
    bbox_south: float
    bbox_north: float
    bbox_west: float
    bbox_east: float
    policy_id: str = SUBSET_POLICY_ID

    @property
    def retained_shape(self) -> tuple[int, int]:
        return (self.y_end - self.y_start, self.x_end - self.x_start)

    def as_attrs(self) -> dict[str, object]:
        """Flat, netCDF-serializable provenance attributes."""
        return {
            "subset_policy_id": self.policy_id,
            "source_grid_ny": int(self.source_ny),
            "source_grid_nx": int(self.source_nx),
            "subset_y_start": int(self.y_start),
            "subset_y_end": int(self.y_end),
            "subset_x_start": int(self.x_start),
            "subset_x_end": int(self.x_end),
            "subset_halo_cells": int(self.halo_cells),
            "subset_bbox_south": float(self.bbox_south),
            "subset_bbox_north": float(self.bbox_north),
            "subset_bbox_west": float(self.bbox_west),
            "subset_bbox_east": float(self.bbox_east),
        }


def assemble_canonical_guidance_v2(
    *,
    model: ModelId,
    forecast_reference_time: np.datetime64,
    source_lead_hours: tuple[int, ...],
    x: np.ndarray,
    y: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    instantaneous_fields: dict[str, np.ndarray],
    interval_fields: dict[str, np.ndarray],
    interval_start_hours: dict[str, tuple[int, ...]],
    grid_id: str,
    configuration_snapshot_id: str,
    variable_lineage_manifest_id: str,
    crs_wkt2: str | None = None,
    retained_subset: RetainedGridSubset | None = None,
) -> xr.Dataset:
    """Assemble ``canonical-guidance.v2`` (plan Section 5.1) for one
    model's one selected cycle.

    ``instantaneous_fields``/``interval_fields`` map canonical
    variable ID -> array shaped ``(source_lead_time, y, x)``.
    ``interval_start_hours`` maps each interval variable ID to its
    per-lead interval start hour (so ``APCP``'s ``(start, lead]``
    bound is retained exactly, including GFS bucket starts that are
    not simply ``lead - 1``).

    ``x``/``y``/``lat``/``lon`` and every field are already the
    *retained* bbox+halo subset of the native grid; ``retained_subset``
    records which native window that is, and is written to the dataset
    attributes as provenance.
    """
    n_lead = len(source_lead_hours)
    lead_time = np.array(
        [np.timedelta64(h, "h") for h in source_lead_hours], dtype="timedelta64[ns]"
    )
    valid_time = forecast_reference_time + lead_time

    data_vars: dict[str, tuple[object, ...]] = {}

    def _mask(shape: tuple[int, ...]) -> np.ndarray:
        return np.zeros(shape, dtype=np.uint16)

    for variable_id, array in instantaneous_fields.items():
        unit_id = _INSTANTANEOUS_VARIABLE_UNITS.get(variable_id)
        if unit_id is None:
            raise CanonicalGuidanceV2Error(
                f"unknown instantaneous canonical_variable_id {variable_id!r}"
            )
        if array.shape != (n_lead, *lat.shape):
            raise CanonicalGuidanceV2Error(
                f"instantaneous field {variable_id!r} has shape {array.shape!r}, expected "
                f"{(n_lead, *lat.shape)!r}"
            )
        data_vars[variable_id] = (
            ("source_lead_time", "y", "x"),
            array.astype(np.float32),
            {
                "unit_id": unit_id,
                "temporal_semantics": "instantaneous",
                "spatial_support": "point",
                "quality_mask": f"{variable_id}_quality_mask",
            },
        )
        data_vars[f"{variable_id}_quality_mask"] = (
            ("source_lead_time", "y", "x"),
            _mask((n_lead, *lat.shape)),
        )

    for variable_id, array in interval_fields.items():
        unit_id = _INTERVAL_VARIABLE_UNITS.get(variable_id)
        if unit_id is None:
            raise CanonicalGuidanceV2Error(
                f"unknown interval canonical_variable_id {variable_id!r}"
            )
        if array.shape != (n_lead, *lat.shape):
            raise CanonicalGuidanceV2Error(
                f"interval field {variable_id!r} has shape {array.shape!r}, expected "
                f"{(n_lead, *lat.shape)!r}"
            )
        starts = interval_start_hours.get(variable_id)
        if starts is None or len(starts) != n_lead:
            raise CanonicalGuidanceV2Error(
                f"interval field {variable_id!r} requires interval_start_hours for every lead"
            )
        interval_start = np.array(
            [forecast_reference_time + np.timedelta64(h, "h") for h in starts],
            dtype="datetime64[ns]",
        )
        interval_end = valid_time
        data_vars[variable_id] = (
            ("source_lead_time", "y", "x"),
            array.astype(np.float32),
            {
                "unit_id": unit_id,
                "temporal_semantics": "accumulation"
                if variable_id != "probability_of_precipitation_1h"
                else "probability",
                "spatial_support": "point",
                "interval_closure": "left_open_right_closed",
                "quality_mask": f"{variable_id}_quality_mask",
            },
        )
        data_vars[f"{variable_id}_quality_mask"] = (
            ("source_lead_time", "y", "x"),
            _mask((n_lead, *lat.shape)),
        )
        data_vars[f"{variable_id}_interval_bounds"] = (
            ("source_lead_time", "bounds"),
            np.stack([interval_start, interval_end.astype("datetime64[ns]")], axis=1),
        )

    if retained_subset is not None:
        expected_shape = retained_subset.retained_shape
        if lat.shape != expected_shape:
            raise CanonicalGuidanceV2Error(
                f"retained subset window {expected_shape!r} does not match the retained "
                f"coordinate mesh shape {lat.shape!r}"
            )

    dataset = xr.Dataset(
        data_vars=data_vars,
        coords={
            "forecast_reference_time": forecast_reference_time,
            "source_lead_time": lead_time,
            "source_valid_time": ("source_lead_time", valid_time),
            "y": y,
            "x": x,
            "latitude": (("y", "x"), lat),
            "longitude": (("y", "x"), lon),
        },
        attrs={
            "schema_version": "canonical-guidance.v2",
            "model": model,
            "time_encoding": "UTC",
            "grid_id": grid_id,
            "configuration_snapshot_id": configuration_snapshot_id,
            "variable_lineage_manifest_id": variable_lineage_manifest_id,
            **({"crs_wkt2": crs_wkt2} if crs_wkt2 is not None else {}),
            **(retained_subset.as_attrs() if retained_subset is not None else {}),
        },
    )
    return dataset


_EXPECTED_MODELS = frozenset({"hrrr", "nbm", "gfs"})

# Codex review (Phase 2 remediation, finding 3): the required
# model-specific canonical variable set. HRRR/GFS never contribute
# probability_of_precipitation_1h (Section 4.5: NBM is the sole PoP
# contributor); NBM additionally requires it.
_REQUIRED_INSTANTANEOUS_VARIABLES: frozenset[str] = frozenset(
    {
        "air_temperature_2m",
        "dew_point_temperature_2m",
        "eastward_wind_10m",
        "northward_wind_10m",
        "wind_gust_10m",
    }
)
_REQUIRED_VARIABLES_BY_MODEL: dict[str, frozenset[str]] = {
    "hrrr": _REQUIRED_INSTANTANEOUS_VARIABLES | {"liquid_equivalent_precipitation_amount_1h"},
    "gfs": _REQUIRED_INSTANTANEOUS_VARIABLES | {"liquid_equivalent_precipitation_amount_1h"},
    "nbm": _REQUIRED_INSTANTANEOUS_VARIABLES
    | {"liquid_equivalent_precipitation_amount_1h", "probability_of_precipitation_1h"},
}

_EXPECTED_TEMPORAL_SEMANTICS: dict[str, str] = {
    **{variable_id: "instantaneous" for variable_id in _INSTANTANEOUS_VARIABLE_UNITS},
    "liquid_equivalent_precipitation_amount_1h": "accumulation",
    "probability_of_precipitation_1h": "probability",
}
_EXPECTED_SPATIAL_SUPPORT = "point"

# Physically plausible bounds for canonical-guidance.v2 values (finding
# 3: validation must reject an unbounded/implausible value, not merely
# a non-finite one).
_VALUE_BOUNDS: dict[str, tuple[float | None, float | None]] = {
    "air_temperature_2m": (180.0, 340.0),
    "dew_point_temperature_2m": (150.0, 340.0),
    "eastward_wind_10m": (None, None),
    "northward_wind_10m": (None, None),
    "wind_gust_10m": (0.0, 100.0),
    "liquid_equivalent_precipitation_amount_1h": (0.0, None),
    "probability_of_precipitation_1h": (0.0, 1.0),
}

_CONFIGURATION_SNAPSHOT_ID_RE = re.compile(r"^cfg_sha256_[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^art_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_GRID_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_INTERVAL_WIDTH_NS = np.timedelta64(1, "h").astype("timedelta64[ns]")
_INTERVAL_WIDTH_TOLERANCE_NS = np.timedelta64(0, "ns")

# Codex re-review finding 3: a canonical artifact must carry the grid
# profile its own model actually publishes. Identifier *syntax* alone
# let an HRRR dataset claim the NBM grid (or an unrelated grid) and
# still validate, which would silently blend fields sampled on
# different geometries. A conforming grid identifier names its model as
# one of its own dot/dash components (``phase2-hrrr.v1``,
# ``hrrr-conus.v1``) and never names a different model.
_GRID_ID_COMPONENT_RE = re.compile(r"[.\-_]")


def _grid_id_models(grid_id: str) -> frozenset[str]:
    """Every model token that appears as a component of ``grid_id``."""
    components = set(_GRID_ID_COMPONENT_RE.split(grid_id))
    return frozenset(components & _EXPECTED_MODELS)


# NBM publishes wind speed and direction; the canonical earth-relative
# U/V components are derived from that pair cornerwise on the native
# grid before any interpolation. A lineage manifest therefore covers the
# source pair while the canonical dataset carries the derived
# components -- a legitimate, declared derivation, not a gap.
_DERIVED_WIND_COMPONENTS = frozenset({"eastward_wind_10m", "northward_wind_10m"})
_DERIVED_WIND_SOURCE_FIELDS = frozenset({"wind_speed_10m", "wind_from_direction_10m"})


class CanonicalGuidanceLineageV2Error(CanonicalGuidanceV2Error):
    """Raised when a ``canonical-guidance.v2`` dataset's declared
    ``variable_lineage_manifest_id`` does not resolve to a real,
    complete, model- and grid-consistent ``variable-lineage.v2``
    manifest (Codex re-review finding 3)."""


def validate_canonical_guidance_lineage_v2(
    dataset: xr.Dataset,
    *,
    lineage_manifest: object,
    lineage_artifact_id: str,
    lineage_artifact_type: str,
    lineage_schema_version: str,
) -> None:
    """Assert that ``dataset`` references a real ``variable-lineage.v2``
    manifest whose *content* matches it (Codex re-review finding 3).

    ``validate_canonical_guidance_v2`` can only check the shape of the
    declared identifier, because it receives no artifact repository.
    This function is the content half of the same contract and is called
    by production normalization, where the manifest and its registered
    artifact identity are both in hand. It rejects:

    - a lineage reference pointing at a different artifact than the one
      that was actually created;
    - an artifact that is not of type/schema ``variable-lineage.v2``
      (e.g. a raw GRIB message or a GFS-QPF-only lineage record);
    - a manifest whose model, grid, cycle, or configuration snapshot
      disagrees with the dataset it claims to describe;
    - a manifest that does not cover exactly the dataset's own variables
      and source leads.
    """
    from mesoforge.contracts.lineage_v2 import VariableLineageManifestV2

    errors: list[str] = []
    declared = dataset.attrs.get("variable_lineage_manifest_id")
    if declared != lineage_artifact_id:
        errors.append(
            f"variable_lineage_manifest_id {declared!r} does not reference the created "
            f"variable-lineage.v2 artifact {lineage_artifact_id!r}"
        )
    if lineage_artifact_type != "variable-lineage":
        errors.append(
            f"variable_lineage_manifest_id must reference a 'variable-lineage' artifact, got "
            f"artifact_type {lineage_artifact_type!r}"
        )
    if lineage_schema_version != "variable-lineage.v2":
        errors.append(
            "variable_lineage_manifest_id must reference a 'variable-lineage.v2' artifact, got "
            f"schema version {lineage_schema_version!r}"
        )
    if not isinstance(lineage_manifest, VariableLineageManifestV2):
        errors.append(
            f"lineage manifest must be a VariableLineageManifestV2, got {type(lineage_manifest)!r}"
        )
        raise CanonicalGuidanceLineageV2Error(
            f"canonical-guidance.v2 lineage validation failed with {len(errors)} problem(s): "
            + "; ".join(errors)
        )

    model = str(dataset.attrs.get("model"))
    if lineage_manifest.model != model:
        errors.append(
            f"lineage manifest describes model {lineage_manifest.model!r}, but the dataset "
            f"declares {model!r}"
        )
    grid_id = str(dataset.attrs.get("grid_id"))
    if str(lineage_manifest.grid_id) != grid_id:
        errors.append(
            f"lineage manifest describes grid {lineage_manifest.grid_id!r}, but the dataset "
            f"declares {grid_id!r}"
        )
    configuration_snapshot_id = str(dataset.attrs.get("configuration_snapshot_id"))
    if str(lineage_manifest.configuration_snapshot_id) != configuration_snapshot_id:
        errors.append(
            "lineage manifest configuration_snapshot_id "
            f"{lineage_manifest.configuration_snapshot_id!r} does not match the dataset's "
            f"{configuration_snapshot_id!r}"
        )

    reference_time = np.datetime_as_string(
        dataset["forecast_reference_time"].values.astype("datetime64[ns]"), unit="s"
    )
    if not str(lineage_manifest.forecast_reference_time).startswith(str(reference_time)):
        errors.append(
            f"lineage manifest forecast_reference_time "
            f"{lineage_manifest.forecast_reference_time!r} does not match the dataset's "
            f"{reference_time!r}"
        )

    dataset_leads = {
        int(value / np.timedelta64(1, "h")) for value in dataset["source_lead_time"].values
    }
    manifest_leads = set(lineage_manifest.expected_source_lead_hours)
    if manifest_leads != dataset_leads:
        errors.append(
            f"lineage manifest covers source leads {sorted(manifest_leads)!r}, but the dataset "
            f"carries {sorted(dataset_leads)!r}"
        )

    dataset_variables = {
        str(name)
        for name in dataset.data_vars
        if not str(name).endswith("_quality_mask") and not str(name).endswith("_interval_bounds")
    }
    manifest_variables = {str(v) for v in lineage_manifest.expected_canonical_variable_ids}
    # The manifest covers the model's *source* fields, which are not
    # always the canonical output fields: NBM publishes wind speed and
    # direction, from which the canonical earth-relative U/V components
    # are derived before interpolation. Every canonical variable must
    # therefore either be covered directly or be a wind component whose
    # source pair is covered.
    uncovered = dataset_variables - manifest_variables
    if uncovered and not (
        uncovered <= _DERIVED_WIND_COMPONENTS and _DERIVED_WIND_SOURCE_FIELDS <= manifest_variables
    ):
        errors.append(
            f"lineage manifest covers variables {sorted(manifest_variables)!r}, which does not "
            f"account for dataset variable(s) {sorted(uncovered)!r}"
        )
    unused = manifest_variables - dataset_variables
    if unused and not unused <= _DERIVED_WIND_SOURCE_FIELDS:
        errors.append(
            f"lineage manifest covers variable(s) {sorted(unused)!r} that the dataset does not "
            "carry and that are not a declared derivation source"
        )

    # Bounded canonical retention (owner architecture decision): the
    # dataset says which native window it retains; the manifest says
    # under which policy it was cut. A dataset whose retained window
    # contradicts its own declared policy is not auditable, so the two
    # must agree exactly.
    policy = lineage_manifest.canonical_retention_policy
    if str(dataset.attrs.get("subset_policy_id")) != str(policy.policy_id):
        errors.append(
            f"dataset subset_policy_id {dataset.attrs.get('subset_policy_id')!r} does not match "
            f"the lineage manifest's canonical retention policy {policy.policy_id!r}"
        )
    for attr_name, expected in (
        ("subset_halo_cells", policy.halo_cells),
        ("subset_bbox_south", policy.bbox_south),
        ("subset_bbox_north", policy.bbox_north),
        ("subset_bbox_west", policy.bbox_west),
        ("subset_bbox_east", policy.bbox_east),
    ):
        actual = dataset.attrs.get(attr_name)
        if actual is None or float(actual) != float(expected):
            errors.append(
                f"dataset {attr_name} {actual!r} does not match the lineage manifest's "
                f"declared retention policy value {expected!r}"
            )

    if errors:
        raise CanonicalGuidanceLineageV2Error(
            f"canonical-guidance.v2 lineage validation failed with {len(errors)} problem(s): "
            + "; ".join(errors)
        )


_SUBSET_INT_ATTRS = (
    "source_grid_ny",
    "source_grid_nx",
    "subset_y_start",
    "subset_y_end",
    "subset_x_start",
    "subset_x_end",
    "subset_halo_cells",
)
_SUBSET_FLOAT_ATTRS = (
    "subset_bbox_south",
    "subset_bbox_north",
    "subset_bbox_west",
    "subset_bbox_east",
)


def _validate_retained_subset_attrs(dataset: xr.Dataset, errors: list[str]) -> None:
    """Validate the bounded-retention provenance a Phase 2 canonical
    artifact carries (owner architecture decision).

    ``canonical-guidance.v2`` retains only the configured bbox+halo
    window of the model's native grid, so the artifact must say exactly
    which window of which native grid that is, or the values cannot be
    placed back onto the source grid and audited. The attributes are
    all-or-nothing: a partially-declared subset is a broken contract,
    not a full-grid artifact.
    """
    present = [name for name in (*_SUBSET_INT_ATTRS, *_SUBSET_FLOAT_ATTRS) if name in dataset.attrs]
    if not present:
        errors.append(
            "canonical-guidance.v2 must declare its retained bbox+halo subset window "
            f"(missing every one of {sorted((*_SUBSET_INT_ATTRS, *_SUBSET_FLOAT_ATTRS))!r})"
        )
        return
    missing = [
        name for name in (*_SUBSET_INT_ATTRS, *_SUBSET_FLOAT_ATTRS) if name not in dataset.attrs
    ]
    if missing:
        errors.append(f"retained subset provenance is incomplete; missing {sorted(missing)!r}")
        return

    if dataset.attrs.get("subset_policy_id") != SUBSET_POLICY_ID:
        errors.append(
            f"subset_policy_id must be exactly {SUBSET_POLICY_ID!r}, got "
            f"{dataset.attrs.get('subset_policy_id')!r}"
        )

    values: dict[str, int] = {}
    for name in _SUBSET_INT_ATTRS:
        raw = dataset.attrs[name]
        try:
            values[name] = int(raw)
        except (TypeError, ValueError):
            errors.append(f"{name!r} must be an integer, got {raw!r}")
            return
    for name in _SUBSET_FLOAT_ATTRS:
        raw = dataset.attrs[name]
        try:
            float(raw)
        except (TypeError, ValueError):
            errors.append(f"{name!r} must be a float, got {raw!r}")

    if values["subset_halo_cells"] < 1:
        errors.append(
            "subset_halo_cells must be at least 1 so every bilinear corner the domain can "
            f"require is retained, got {values['subset_halo_cells']}"
        )
    for axis, start, end, extent in (
        ("y", values["subset_y_start"], values["subset_y_end"], values["source_grid_ny"]),
        ("x", values["subset_x_start"], values["subset_x_end"], values["source_grid_nx"]),
    ):
        if extent <= 0:
            errors.append(f"source_grid_n{axis} must be positive, got {extent}")
            continue
        if start < 0 or end > extent or end <= start:
            errors.append(
                f"retained {axis} window [{start}, {end}) must be a nonempty half-open range "
                f"inside the native extent [0, {extent})"
            )
            continue
        if axis in dataset.dims and int(dataset.sizes[axis]) != end - start:
            errors.append(
                f"retained {axis} dimension size {int(dataset.sizes[axis])} does not match the "
                f"declared window [{start}, {end})"
            )


def validate_canonical_guidance_v2(dataset: xr.Dataset) -> None:
    """Section 5.1: distinct validator from ``validate_canonical_dataset``
    (v1); dispatched exactly by schema version, never applied to a v1
    dataset.

    Enforces the full canonical contract (Codex review remediation,
    finding 3): required model-specific fields/dimensions, exact
    units/semantics/support, ``source_valid_time = reference + lead``,
    interval bounds/width/closure, finite and physically bounded
    values, grid identity/coordinates/shape, quality masks, and
    lineage/configuration identities.
    """
    errors: list[str] = []

    if dataset.attrs.get("schema_version") != "canonical-guidance.v2":
        errors.append(
            f"schema_version must be 'canonical-guidance.v2', got "
            f"{dataset.attrs.get('schema_version')!r}"
        )
    model = dataset.attrs.get("model")
    if model not in _EXPECTED_MODELS:
        errors.append(f"model must be one of {sorted(_EXPECTED_MODELS)!r}, got {model!r}")
    if "forecast_reference_time" not in dataset.coords:
        errors.append("missing forecast_reference_time coordinate")
    elif dataset["forecast_reference_time"].ndim != 0:
        errors.append("forecast_reference_time must be scalar")
    for required_coord in ("source_lead_time", "source_valid_time", "latitude", "longitude"):
        if required_coord not in dataset.coords:
            errors.append(f"missing required coordinate {required_coord!r}")
    for required_dim in ("y", "x"):
        if required_dim not in dataset.dims:
            errors.append(f"missing required dimension {required_dim!r}")

    # -- lineage/config identities (finding 3) -----------------------
    grid_id = dataset.attrs.get("grid_id")
    if not isinstance(grid_id, str) or not _GRID_ID_RE.match(grid_id):
        errors.append(
            f"grid_id must be a non-empty lowercase kebab/dot identifier, got {grid_id!r}"
        )
    else:
        # Codex re-review finding 3: the grid must be the one this
        # model actually publishes, not merely a syntactically valid
        # identifier. A model-incompatible grid means the dataset's
        # values were sampled on a different geometry than the blend
        # assumes.
        named_models = _grid_id_models(grid_id)
        if named_models != {model}:
            errors.append(
                f"model {model!r} requires a grid_id naming its own model profile (e.g. "
                f"'phase2-{model}.v1'), got {grid_id!r} naming {sorted(named_models)!r}; a "
                "model-incompatible grid profile is never blendable"
            )
    configuration_snapshot_id = dataset.attrs.get("configuration_snapshot_id")
    if not isinstance(configuration_snapshot_id, str) or not _CONFIGURATION_SNAPSHOT_ID_RE.match(
        configuration_snapshot_id
    ):
        errors.append(
            "configuration_snapshot_id must be 'cfg_sha256_' followed by 64 lowercase hex "
            f"characters, got {configuration_snapshot_id!r}"
        )
    variable_lineage_manifest_id = dataset.attrs.get("variable_lineage_manifest_id")
    if not isinstance(variable_lineage_manifest_id, str) or not _ARTIFACT_ID_RE.match(
        variable_lineage_manifest_id
    ):
        errors.append(
            "variable_lineage_manifest_id must be a real artifact ID, got "
            f"{variable_lineage_manifest_id!r}"
        )

    _validate_retained_subset_attrs(dataset, errors)

    if errors:
        raise CanonicalGuidanceV2Error(
            f"canonical-guidance.v2 validation failed with {len(errors)} problem(s): "
            + "; ".join(errors)
        )

    # -- source_lead_time / source_valid_time identity (finding 3) ---
    lead_values = dataset["source_lead_time"].values
    if len(lead_values) == 0:
        errors.append("source_lead_time must be non-empty")
    elif np.any(lead_values < np.timedelta64(0, "ns")):
        errors.append("source_lead_time must be nonnegative")
    elif len(lead_values) > 1 and not np.all(np.diff(lead_values) > np.timedelta64(0, "ns")):
        errors.append("source_lead_time must be strictly increasing")

    reference_time = dataset["forecast_reference_time"].values
    valid_time = dataset["source_valid_time"].values
    expected_valid_time = (reference_time + lead_values).astype("datetime64[ns]")
    if not np.array_equal(valid_time.astype("datetime64[ns]"), expected_valid_time):
        errors.append(
            "source_valid_time must equal forecast_reference_time + source_lead_time exactly; "
            f"expected {expected_valid_time!r}, got {valid_time.astype('datetime64[ns]')!r}"
        )

    # -- grid identity/coordinates/shape (finding 3) ------------------
    x_values = dataset["x"].values if "x" in dataset.coords else None
    y_values = dataset["y"].values if "y" in dataset.coords else None
    for name, values in (("x", x_values), ("y", y_values)):
        if values is None:
            continue
        if len(values) == 0:
            errors.append(f"{name!r} coordinate must be non-empty")
            continue
        finite = np.isfinite(values.astype(np.float64))
        if not np.all(finite):
            errors.append(f"{name!r} coordinate must be entirely finite")
            continue
        if len(values) > 1:
            diffs = np.diff(values.astype(np.float64))
            if not (np.all(diffs > 0) or np.all(diffs < 0)):
                errors.append(f"{name!r} coordinate must be strictly monotonic")

    latitude = dataset["latitude"].values if "latitude" in dataset.coords else None
    longitude = dataset["longitude"].values if "longitude" in dataset.coords else None
    if latitude is not None and longitude is not None:
        if latitude.shape != longitude.shape:
            errors.append(
                f"latitude shape {latitude.shape!r} must match longitude shape {longitude.shape!r}"
            )
        if y_values is not None and x_values is not None:
            expected_shape = (len(y_values), len(x_values))
            if latitude.shape != expected_shape:
                errors.append(
                    f"latitude/longitude shape {latitude.shape!r} must be (len(y), len(x)) = "
                    f"{expected_shape!r}"
                )
        finite_lat = np.isfinite(latitude.astype(np.float64))
        if not np.all(finite_lat):
            errors.append("latitude must be entirely finite")
        elif np.any(latitude < -90.0) or np.any(latitude > 90.0):
            errors.append("latitude must be in [-90, 90]")
        if not np.all(np.isfinite(longitude.astype(np.float64))):
            errors.append("longitude must be entirely finite")

    # -- required model-specific fields (finding 3) -------------------
    required_variables = _REQUIRED_VARIABLES_BY_MODEL.get(str(model), frozenset())
    present_variables = {
        str(name)
        for name in dataset.data_vars
        if not str(name).endswith("_quality_mask") and not str(name).endswith("_interval_bounds")
    }
    missing_variables = required_variables - present_variables
    if missing_variables:
        errors.append(
            f"model {model!r} is missing required canonical variable(s) "
            f"{sorted(missing_variables)!r}"
        )
    extra_variables = present_variables - required_variables
    if extra_variables:
        errors.append(
            f"model {model!r} has unapproved canonical variable(s) {sorted(extra_variables)!r}"
        )

    expected_data_vars = (
        required_variables
        | {f"{name}_quality_mask" for name in required_variables}
        | {
            f"{name}_interval_bounds"
            for name in required_variables
            if name in _INTERVAL_VARIABLE_UNITS
        }
    )
    actual_data_vars = {str(name) for name in dataset.data_vars}
    if actual_data_vars != expected_data_vars:
        errors.append(
            "data variables must be exactly the approved fields, masks, and interval bounds; "
            f"missing={sorted(expected_data_vars - actual_data_vars)!r}, "
            f"extra={sorted(actual_data_vars - expected_data_vars)!r}"
        )

    expected_dims = {"source_lead_time", "y", "x", "bounds"}
    if set(dataset.dims) != expected_dims:
        errors.append(
            f"dimensions must be exactly {sorted(expected_dims)!r}, got "
            f"{sorted(str(name) for name in dataset.dims)!r}"
        )

    n_lead = len(lead_values)
    for raw_name in dataset.data_vars:
        variable_name = str(raw_name)
        if variable_name.endswith("_quality_mask") or variable_name.endswith("_interval_bounds"):
            continue
        data_array = dataset[raw_name]
        values = data_array.values
        attrs = data_array.attrs
        if data_array.dims != ("source_lead_time", "y", "x"):
            errors.append(
                f"data variable {variable_name!r} dimensions/order must be exactly "
                "('source_lead_time', 'y', 'x')"
            )

        # -- exact units/semantics/support (finding 3) -----------------
        expected_unit = _INSTANTANEOUS_VARIABLE_UNITS.get(
            variable_name, _INTERVAL_VARIABLE_UNITS.get(variable_name)
        )
        if expected_unit is not None and attrs.get("unit_id") != expected_unit:
            errors.append(
                f"data variable {variable_name!r} unit_id must be exactly {expected_unit!r}, "
                f"got {attrs.get('unit_id')!r}"
            )
        expected_semantics = _EXPECTED_TEMPORAL_SEMANTICS.get(variable_name)
        if expected_semantics is not None and attrs.get("temporal_semantics") != expected_semantics:
            errors.append(
                f"data variable {variable_name!r} temporal_semantics must be exactly "
                f"{expected_semantics!r}, got {attrs.get('temporal_semantics')!r}"
            )
        if attrs.get("spatial_support") != _EXPECTED_SPATIAL_SUPPORT:
            errors.append(
                f"data variable {variable_name!r} spatial_support must be exactly "
                f"{_EXPECTED_SPATIAL_SUPPORT!r}, got {attrs.get('spatial_support')!r}"
            )

        mask_name = attrs.get("quality_mask")
        if mask_name is None or mask_name not in dataset.data_vars:
            errors.append(f"data variable {variable_name!r} missing a registered quality mask")
            continue
        mask_values = dataset[mask_name].values
        if dataset[mask_name].values.dtype != np.uint16:
            errors.append(f"quality mask {mask_name!r} must have dtype uint16")
        if mask_values.shape != values.shape:
            errors.append(
                f"quality mask {mask_name!r} shape {mask_values.shape!r} must match data "
                f"variable {variable_name!r} shape {values.shape!r}"
            )
        if dataset[mask_name].dims != data_array.dims:
            errors.append(
                f"quality mask {mask_name!r} dimensions/order must match {variable_name!r}"
            )
        if not np.all(np.isin(mask_values, (0, 1))):
            errors.append(f"quality mask {mask_name!r} contains an unapproved mask value")
        finite = np.isfinite(values.astype(np.float64))
        masked_missing = mask_values != 0
        if np.any((~finite) & (~masked_missing)):
            errors.append(
                f"data variable {variable_name!r} has non-finite values not marked missing"
            )

        # -- finite and physically bounded values (finding 3) ----------
        bounds = _VALUE_BOUNDS.get(variable_name)
        if bounds is not None:
            lower, upper = bounds
            unmasked_finite = finite & ~masked_missing
            checked = values.astype(np.float64)[unmasked_finite]
            below = lower is not None and np.any(checked < lower)
            above = upper is not None and np.any(checked > upper)
            if checked.size and (below or above):
                errors.append(
                    f"data variable {variable_name!r} has value(s) outside the physically "
                    f"plausible bound [{lower!r}, {upper!r}]"
                )

        # -- interval bounds/width/closure (finding 3) ------------------
        bounds_name = f"{variable_name}_interval_bounds"
        is_interval = variable_name in _INTERVAL_VARIABLE_UNITS
        if is_interval and bounds_name not in dataset.data_vars:
            errors.append(f"interval variable {variable_name!r} is missing mandatory bounds")
        if not is_interval and bounds_name in dataset.data_vars:
            errors.append(f"instantaneous variable {variable_name!r} must not have interval bounds")
        if bounds_name in dataset.data_vars:
            if dataset[bounds_name].dims != ("source_lead_time", "bounds"):
                errors.append(
                    f"{bounds_name!r} dimensions/order must be exactly "
                    "('source_lead_time', 'bounds')"
                )
            interval_bounds = dataset[bounds_name].values
            if interval_bounds.shape != (n_lead, 2):
                errors.append(
                    f"{bounds_name!r} must have shape ({n_lead}, 2), got {interval_bounds.shape!r}"
                )
            else:
                starts = interval_bounds[:, 0].astype("datetime64[ns]")
                ends = interval_bounds[:, 1].astype("datetime64[ns]")
                if not np.array_equal(ends, expected_valid_time):
                    errors.append(
                        f"{bounds_name!r} interval end must equal source_valid_time exactly"
                    )
                if np.any(ends <= starts):
                    errors.append(
                        f"{bounds_name!r} interval end must be strictly after interval start"
                    )
                widths = ends - starts
                if np.any(widths != _INTERVAL_WIDTH_NS):
                    errors.append(
                        f"{bounds_name!r} interval width must be exactly 1 hour for every lead"
                    )
            if attrs.get("interval_closure") != "left_open_right_closed":
                errors.append(
                    f"data variable {variable_name!r} interval_closure must be exactly "
                    f"'left_open_right_closed', got {attrs.get('interval_closure')!r}"
                )

    required_thermodynamic = {
        "air_temperature_2m",
        "dew_point_temperature_2m",
        "air_temperature_2m_quality_mask",
        "dew_point_temperature_2m_quality_mask",
    }
    if required_thermodynamic.issubset(dataset.data_vars):
        temperature = dataset["air_temperature_2m"].values.astype(np.float64)
        dew_point = dataset["dew_point_temperature_2m"].values.astype(np.float64)
        usable = (
            (dataset["air_temperature_2m_quality_mask"].values == 0)
            & (dataset["dew_point_temperature_2m_quality_mask"].values == 0)
            & np.isfinite(temperature)
            & np.isfinite(dew_point)
        )
        if np.any(dew_point[usable] > temperature[usable] + 1e-6):
            errors.append("dew_point_temperature_2m must not exceed air_temperature_2m + 1e-6 K")

    if errors:
        raise CanonicalGuidanceV2Error(
            f"canonical-guidance.v2 validation failed with {len(errors)} problem(s): "
            + "; ".join(errors)
        )
