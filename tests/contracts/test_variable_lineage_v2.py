"""``variable-lineage.v2`` contract tests (Codex re-review finding 3).

The canonical guidance contract previously accepted any string that
*looked* like an artifact ID as its ``variable_lineage_manifest_id``.
These tests pin the real semantics instead: a manifest must be complete
across every expected variable/lead, internally consistent about model
and grid identity, and it must actually describe the dataset that
references it.
"""

from __future__ import annotations

import numpy as np
import pytest

from mesoforge.contracts.lineage_v2 import (
    CanonicalRetentionPolicyV2,
    VariableLineageEntryV2,
    VariableLineageManifestV2,
)
from mesoforge.guidance.canonical_v2 import (
    CanonicalGuidanceLineageV2Error,
    RetainedGridSubset,
    assemble_canonical_guidance_v2,
    validate_canonical_guidance_lineage_v2,
)

_CFG = "cfg_sha256_" + "0" * 64
_INDEX = "art_00000000-0000-0000-0000-0000000000a1"
_MESSAGE = "art_00000000-0000-0000-0000-0000000000b1"
_LINEAGE = "art_00000000-0000-0000-0000-0000000000c1"
_REFERENCE = "2030-08-31T12:00:00+00:00"
_LEADS = (1, 2)

# Phase 2 bounded canonical retention: the configured domain bbox plus a
# one-cell halo. The manifest declares the policy; the dataset declares
# the resolved native window it produced.
_BBOX = {
    "bbox_south": 45.05265,
    "bbox_north": 46.55265,
    "bbox_west": -94.07956,
    "bbox_east": -92.07956,
}
_RETENTION_POLICY = CanonicalRetentionPolicyV2(halo_cells=1, **_BBOX)  # type: ignore[arg-type]
_SUBSET = RetainedGridSubset(
    source_ny=8,
    source_nx=9,
    y_start=3,
    y_end=5,
    x_start=3,
    x_end=6,
    halo_cells=1,
    **_BBOX,  # type: ignore[arg-type]
)
_HRRR_VARIABLES = (
    "air_temperature_2m",
    "dew_point_temperature_2m",
    "eastward_wind_10m",
    "northward_wind_10m",
    "wind_gust_10m",
    "liquid_equivalent_precipitation_amount_1h",
)


def _entry(variable_id: str, lead: int, **overrides: object) -> VariableLineageEntryV2:
    values: dict[str, object] = {
        "canonical_variable_id": variable_id,
        "source_lead_hours": lead,
        "index_artifact_id": _INDEX,
        "selected_grib_artifact_ids": (_MESSAGE,),
        "message_numbers": (1,),
        "byte_ranges": ((0, 188),),
        "inventory_rows": (f"1:0:d=2030083112:TMP:2 m above ground:{lead} hour fcst:",),
        "source_cycle": _REFERENCE,
        "endpoint": "aws",
        "resolved_index_url": "https://example/hrrr.grib2.idx",
        "resolved_grib_url": "https://example/hrrr.grib2",
        "selector_expression": ":TMP:2 m above ground:1 hour fcst:$",
        "decode_backend_kwargs": {"indexpath": "", "errors": "raise"},
        "unit_conversion": "identity:K",
        "wind_rotation_policy": None,
        "source_grid_profile_id": "hrrr-sfc-conus.v1",
        "source_grid_id": "hrrr-sfc-conus.v1",
        "output_grid_id": "phase2-hrrr.v1",
    }
    values.update(overrides)
    return VariableLineageEntryV2(**values)  # type: ignore[arg-type]


def _manifest(**overrides: object) -> VariableLineageManifestV2:
    values: dict[str, object] = {
        "model": "hrrr",
        "grid_id": "phase2-hrrr.v1",
        "source_grid_profile_id": "hrrr-sfc-conus.v1",
        "forecast_reference_time": _REFERENCE,
        "configuration_snapshot_id": _CFG,
        "entries": tuple(
            _entry(variable_id, lead) for lead in _LEADS for variable_id in _HRRR_VARIABLES
        ),
        "expected_canonical_variable_ids": _HRRR_VARIABLES,
        "expected_source_lead_hours": _LEADS,
        "canonical_retention_policy": _RETENTION_POLICY,
    }
    values.update(overrides)
    return VariableLineageManifestV2(**values)  # type: ignore[arg-type]


class TestManifestCompleteness:
    def test_accepts_a_complete_manifest(self) -> None:
        manifest = _manifest()
        assert len(manifest.entries) == len(_HRRR_VARIABLES) * len(_LEADS)
        assert manifest.schema_version == "variable-lineage.v2"

    def test_rejects_a_missing_variable_lead_entry(self) -> None:
        entries = tuple(
            _entry(variable_id, lead)
            for lead in _LEADS
            for variable_id in _HRRR_VARIABLES
            if not (lead == 2 and variable_id == "wind_gust_10m")
        )
        with pytest.raises(ValueError, match="missing 1 expected variable/lead"):
            _manifest(entries=entries)

    def test_rejects_a_duplicate_entry(self) -> None:
        entries = _manifest().entries + (_entry("wind_gust_10m", 1),)
        with pytest.raises(ValueError, match="duplicate variable/lead entry"):
            _manifest(entries=entries)

    def test_rejects_an_unexpected_entry(self) -> None:
        entries = _manifest().entries + (_entry("air_temperature_2m", 99),)
        with pytest.raises(ValueError, match="unexpected variable/lead entries"):
            _manifest(entries=entries)

    def test_rejects_an_entry_on_a_different_grid(self) -> None:
        entries = (
            _entry("air_temperature_2m", 1, output_grid_id="phase2-nbm.v1"),
        ) + _manifest().entries[1:]
        with pytest.raises(ValueError, match="does not match the manifest grid_id"):
            _manifest(entries=entries)

    def test_rejects_an_entry_on_a_different_source_grid_profile(self) -> None:
        entries = (
            _entry("air_temperature_2m", 1, source_grid_profile_id="nbm-core-conus.v1"),
        ) + _manifest().entries[1:]
        with pytest.raises(ValueError, match="source_grid_profile_id"):
            _manifest(entries=entries)


class TestEntryEvidence:
    def test_rejects_an_entry_with_no_selected_message(self) -> None:
        with pytest.raises(ValueError, match="at least one selected GRIB message"):
            _entry("air_temperature_2m", 1, selected_grib_artifact_ids=())

    def test_rejects_non_parallel_message_evidence(self) -> None:
        """Message artifacts, numbers, ranges, and inventory rows must
        line up one-per-selected-message, or the lineage is unreadable."""
        with pytest.raises(ValueError, match="must be parallel per selected message"):
            _entry(
                "liquid_equivalent_precipitation_amount_1h",
                1,
                selected_grib_artifact_ids=(_MESSAGE, _MESSAGE),
                message_numbers=(1,),
            )

    def test_rejects_an_empty_byte_range(self) -> None:
        with pytest.raises(ValueError, match="nonempty"):
            _entry("air_temperature_2m", 1, byte_ranges=((188, 188),))

    def test_rejects_an_empty_selector_or_unit_conversion(self) -> None:
        with pytest.raises(ValueError, match="selector_expression"):
            _entry("air_temperature_2m", 1, selector_expression="")
        with pytest.raises(ValueError, match="unit_conversion"):
            _entry("air_temperature_2m", 1, unit_conversion="")

    def test_accepts_plural_dual_parent_precipitation_evidence(self) -> None:
        """GFS's duplicate bucket/continuous APCP parents are a real,
        expected plural case -- not a validation error."""
        entry = _entry(
            "liquid_equivalent_precipitation_amount_1h",
            1,
            selected_grib_artifact_ids=(_MESSAGE, "art_00000000-0000-0000-0000-0000000000b2"),
            message_numbers=(6, 7),
            byte_ranges=((940, 1152), (1152, 1364)),
            inventory_rows=("6:940:APCP:", "7:1152:APCP:"),
        )
        assert len(entry.selected_grib_artifact_ids) == 2


def _dataset(model: str = "hrrr", grid_id: str = "phase2-hrrr.v1"):
    reference = np.datetime64("2030-08-31T12:00:00", "ns")
    shape = (len(_LEADS), 2, 3)
    lat = np.full(shape[1:], 45.5)
    lon = np.full(shape[1:], -93.0)
    return assemble_canonical_guidance_v2(
        model=model,  # type: ignore[arg-type]
        forecast_reference_time=reference,
        source_lead_hours=_LEADS,
        x=np.arange(shape[2], dtype=np.float64),
        y=np.arange(shape[1], dtype=np.float64),
        lat=lat,
        lon=lon,
        instantaneous_fields={
            "air_temperature_2m": np.full(shape, 280.0),
            "dew_point_temperature_2m": np.full(shape, 275.0),
            "eastward_wind_10m": np.full(shape, 3.0),
            "northward_wind_10m": np.full(shape, 2.0),
            "wind_gust_10m": np.full(shape, 8.0),
        },
        interval_fields={"liquid_equivalent_precipitation_amount_1h": np.full(shape, 1.0)},
        interval_start_hours={
            "liquid_equivalent_precipitation_amount_1h": tuple(lead - 1 for lead in _LEADS)
        },
        grid_id=grid_id,
        configuration_snapshot_id=_CFG,
        variable_lineage_manifest_id=_LINEAGE,
        retained_subset=_SUBSET,
    )


def _validate(dataset, manifest, **overrides):
    kwargs = {
        "lineage_manifest": manifest,
        "lineage_artifact_id": _LINEAGE,
        "lineage_artifact_type": "variable-lineage",
        "lineage_schema_version": "variable-lineage.v2",
    }
    kwargs.update(overrides)
    validate_canonical_guidance_lineage_v2(dataset, **kwargs)


class TestCanonicalLineageContentValidation:
    def test_accepts_a_matching_manifest(self) -> None:
        _validate(_dataset(), _manifest())

    def test_rejects_a_wrong_artifact_type(self) -> None:
        """Pointing lineage at a raw GRIB message artifact -- exactly the
        production defect the re-review found -- must fail."""
        with pytest.raises(CanonicalGuidanceLineageV2Error, match="artifact_type"):
            _validate(_dataset(), _manifest(), lineage_artifact_type="hrrr-grib-selected-grib")

    def test_rejects_a_wrong_schema_version(self) -> None:
        """A GFS-QPF-only lineage record is not variable-lineage.v2."""
        with pytest.raises(CanonicalGuidanceLineageV2Error, match="variable-lineage.v2"):
            _validate(
                _dataset(),
                _manifest(),
                lineage_artifact_type="gfs-qpf-lineage",
                lineage_schema_version="gfs-qpf-lineage.v1",
            )

    def test_rejects_a_reference_to_a_different_artifact(self) -> None:
        with pytest.raises(CanonicalGuidanceLineageV2Error, match="does not reference"):
            _validate(
                _dataset(),
                _manifest(),
                lineage_artifact_id="art_00000000-0000-0000-0000-0000000000ff",
            )

    def test_rejects_a_manifest_for_a_different_model(self) -> None:
        manifest = _manifest(
            model="nbm",
            grid_id="phase2-nbm.v1",
            entries=tuple(
                _entry(variable_id, lead, output_grid_id="phase2-nbm.v1")
                for lead in _LEADS
                for variable_id in _HRRR_VARIABLES
            ),
        )
        with pytest.raises(CanonicalGuidanceLineageV2Error, match="describes model"):
            _validate(_dataset(), manifest)

    def test_rejects_a_manifest_with_a_different_configuration_snapshot(self) -> None:
        manifest = _manifest(configuration_snapshot_id="cfg_sha256_" + "1" * 64)
        with pytest.raises(CanonicalGuidanceLineageV2Error, match="configuration_snapshot_id"):
            _validate(_dataset(), manifest)

    def test_rejects_a_manifest_covering_different_leads(self) -> None:
        leads = (1, 2, 3)
        manifest = _manifest(
            entries=tuple(
                _entry(variable_id, lead) for lead in leads for variable_id in _HRRR_VARIABLES
            ),
            expected_source_lead_hours=leads,
        )
        with pytest.raises(CanonicalGuidanceLineageV2Error, match="source leads"):
            _validate(_dataset(), manifest)

    def test_rejects_a_manifest_covering_different_variables(self) -> None:
        variables = _HRRR_VARIABLES[:-1]
        manifest = _manifest(
            entries=tuple(
                _entry(variable_id, lead) for lead in _LEADS for variable_id in variables
            ),
            expected_canonical_variable_ids=variables,
        )
        with pytest.raises(CanonicalGuidanceLineageV2Error, match="covers variables"):
            _validate(_dataset(), manifest)

    def test_rejects_a_non_manifest_object(self) -> None:
        with pytest.raises(CanonicalGuidanceLineageV2Error, match="must be a"):
            _validate(_dataset(), {"schema_version": "variable-lineage.v2"})
