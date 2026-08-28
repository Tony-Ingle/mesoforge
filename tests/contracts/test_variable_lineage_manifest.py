"""Contract tests for mesoforge.contracts.lineage.VariableLineageManifest
(plan Section 3.4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mesoforge.contracts.lineage import VariableLineageEntry, VariableLineageManifest

_VARIABLE_IDS = ("air_temperature_2m", "eastward_wind_10m", "northward_wind_10m")
_LEADS = tuple(range(2))  # small for test brevity


def _entry(variable_id: str, lead: int) -> VariableLineageEntry:
    return VariableLineageEntry(
        canonical_variable_id=variable_id,
        lead_hours=lead,
        index_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
        selected_grib_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
        source_cycle="2026082818",
        resolved_url="https://aws/hrrr.20260828/conus/hrrr.t18z.wrfsfcf00.grib2",
        source_revision="etag=abc;length=100",
        message_number=1,
        byte_start=0,
        byte_end=100,
        inventory_row="1:0:d=2026082818:TMP:2 m above ground:anl:",
        grib_keys={"discipline": 0},
        selector_expression=":TMP:2 m above ground:(anl|[0-9]+ hour fcst):",
        decode_backend_kwargs={"indexpath": ""},
        unit_conversion="identity",
        wind_rotation_policy=None if variable_id == "air_temperature_2m" else "identity",
        grid_relative=None if variable_id == "air_temperature_2m" else False,
        source_grid_id="hrrr-conus.v1",
        output_grid_id="hrrr-conus-grasston-subset.v1",
        spatial_subset_y_start=0,
        spatial_subset_y_end=6,
        spatial_subset_x_start=0,
        spatial_subset_x_end=6,
    )


def _complete_entries() -> tuple[VariableLineageEntry, ...]:
    return tuple(_entry(v, lead) for v in _VARIABLE_IDS for lead in _LEADS)


class TestVariableLineageManifest:
    def test_accepts_complete_manifest(self) -> None:
        manifest = VariableLineageManifest(
            entries=_complete_entries(),
            expected_canonical_variable_ids=_VARIABLE_IDS,
            expected_lead_hours=_LEADS,
        )
        assert len(manifest.entries) == 6

    def test_rejects_missing_entry(self) -> None:
        entries = _complete_entries()[:-1]
        with pytest.raises(ValidationError, match="missing"):
            VariableLineageManifest(
                entries=entries,
                expected_canonical_variable_ids=_VARIABLE_IDS,
                expected_lead_hours=_LEADS,
            )

    def test_rejects_duplicate_entry(self) -> None:
        entries = _complete_entries() + (_entry("air_temperature_2m", 0),)
        with pytest.raises(ValidationError, match="duplicate"):
            VariableLineageManifest(
                entries=entries,
                expected_canonical_variable_ids=_VARIABLE_IDS,
                expected_lead_hours=_LEADS,
            )

    def test_rejects_unexpected_entry(self) -> None:
        entries = _complete_entries() + (_entry("air_temperature_2m", 5),)
        with pytest.raises(ValidationError, match="unexpected"):
            VariableLineageManifest(
                entries=entries,
                expected_canonical_variable_ids=_VARIABLE_IDS,
                expected_lead_hours=_LEADS,
            )

    def test_rejects_malformed_artifact_id(self) -> None:
        with pytest.raises(ValidationError):
            VariableLineageEntry(
                canonical_variable_id="air_temperature_2m",
                lead_hours=0,
                index_artifact_id="not-an-id",
                selected_grib_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
                source_cycle="2026082818",
                resolved_url="https://aws/x",
                source_revision="etag=abc;length=100",
                message_number=1,
                byte_start=0,
                byte_end=100,
                inventory_row="1:0:d=2026082818:TMP:2 m above ground:anl:",
                grib_keys={"discipline": 0},
                selector_expression=":TMP:2 m above ground:(anl|[0-9]+ hour fcst):",
                decode_backend_kwargs={"indexpath": ""},
                unit_conversion="identity",
                wind_rotation_policy=None,
                grid_relative=None,
                source_grid_id="hrrr-conus.v1",
                output_grid_id="hrrr-conus-grasston-subset.v1",
                spatial_subset_y_start=0,
                spatial_subset_y_end=6,
                spatial_subset_x_start=0,
                spatial_subset_x_end=6,
            )
