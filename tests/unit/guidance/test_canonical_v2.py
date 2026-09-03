"""Unit tests for mesoforge.guidance.canonical_v2 (plan Section 5.1,
Task 3/4/5): canonical-guidance.v2 assembly and validation.
"""

from __future__ import annotations

import numpy as np
import pytest

from mesoforge.guidance.canonical_v2 import (
    CanonicalGuidanceV2Error,
    RetainedGridSubset,
    assemble_canonical_guidance_v2,
    validate_canonical_guidance_v2,
)

_LAT = np.array([[45.0, 45.5], [46.0, 46.5]])
_LON = np.array([[-93.5, -93.0], [-93.5, -93.0]])
_X = np.array([0.0, 1.0])
_Y = np.array([0.0, 1.0])
_REF_TIME = np.datetime64("2026-08-30T12:00:00")
_CFG_SNAPSHOT_ID = "cfg_sha256_" + "0" * 64
_LINEAGE_ID = "art_00000000-0000-0000-0000-000000000001"

# A 2x2 retained window cut from a 6x6 native grid with a one-cell halo
# (Phase 2 bounded canonical retention).
_SUBSET = RetainedGridSubset(
    source_ny=6,
    source_nx=6,
    y_start=2,
    y_end=4,
    x_start=2,
    x_end=4,
    halo_cells=1,
    bbox_south=45.05265,
    bbox_north=46.55265,
    bbox_west=-94.07956,
    bbox_east=-92.07956,
)


def _base_kwargs(**overrides):
    values = dict(
        model="nbm",
        forecast_reference_time=_REF_TIME,
        source_lead_hours=(1, 2, 3),
        x=_X,
        y=_Y,
        lat=_LAT,
        lon=_LON,
        instantaneous_fields={
            "air_temperature_2m": np.full((3, 2, 2), 280.0),
            "dew_point_temperature_2m": np.full((3, 2, 2), 275.0),
            "eastward_wind_10m": np.full((3, 2, 2), 3.0),
            "northward_wind_10m": np.full((3, 2, 2), 2.0),
            "wind_gust_10m": np.full((3, 2, 2), 8.0),
        },
        interval_fields={
            "liquid_equivalent_precipitation_amount_1h": np.full((3, 2, 2), 1.0),
            "probability_of_precipitation_1h": np.full((3, 2, 2), 0.4),
        },
        interval_start_hours={
            "liquid_equivalent_precipitation_amount_1h": (0, 1, 2),
            "probability_of_precipitation_1h": (0, 1, 2),
        },
        grid_id="nbm-conus.v1",
        configuration_snapshot_id=_CFG_SNAPSHOT_ID,
        variable_lineage_manifest_id=_LINEAGE_ID,
        retained_subset=_SUBSET,
    )
    values.update(overrides)
    return values


class TestModelCompatibleGridProfile:
    """Codex re-review finding 3: grid identifier *syntax* is not a grid
    contract. A canonical artifact must carry the grid profile its own
    model publishes, because blending values sampled on two different
    geometries is a silent scientific error."""

    def test_accepts_a_model_matching_grid(self) -> None:
        for model, grid_id in (
            ("nbm", "phase2-nbm.v1"),
            ("hrrr", "hrrr-conus.v1"),
            ("gfs", "gfs-0p25.v1"),
        ):
            dataset = _valid_dataset_for(model, grid_id)
            validate_canonical_guidance_v2(dataset)

    @pytest.mark.parametrize(
        "model,grid_id",
        [
            ("nbm", "phase2-hrrr.v1"),
            ("nbm", "phase2-gfs.v1"),
            ("hrrr", "phase2-nbm.v1"),
            ("gfs", "hrrr-conus.v1"),
        ],
    )
    def test_rejects_a_model_incompatible_grid(self, model: str, grid_id: str) -> None:
        dataset = _valid_dataset_for(model, grid_id)
        with pytest.raises(CanonicalGuidanceV2Error, match="model-incompatible grid"):
            validate_canonical_guidance_v2(dataset)

    def test_rejects_a_grid_naming_no_model_at_all(self) -> None:
        dataset = _valid_dataset_for("nbm", "synthetic-grid.v1")
        with pytest.raises(CanonicalGuidanceV2Error, match="model-incompatible grid"):
            validate_canonical_guidance_v2(dataset)

    def test_rejects_a_grid_naming_two_models(self) -> None:
        dataset = _valid_dataset_for("nbm", "nbm-hrrr-merged.v1")
        with pytest.raises(CanonicalGuidanceV2Error, match="model-incompatible grid"):
            validate_canonical_guidance_v2(dataset)


def _valid_dataset_for(model: str, grid_id: str):
    """A minimally valid canonical dataset for ``model`` on ``grid_id``.

    HRRR/GFS never carry PoP (NBM is the sole PoP contributor), so the
    interval field set differs by model.
    """
    interval_fields = {"liquid_equivalent_precipitation_amount_1h": np.full((3, 2, 2), 1.0)}
    interval_start_hours = {"liquid_equivalent_precipitation_amount_1h": (0, 1, 2)}
    if model == "nbm":
        interval_fields["probability_of_precipitation_1h"] = np.full((3, 2, 2), 0.4)
        interval_start_hours["probability_of_precipitation_1h"] = (0, 1, 2)
    return assemble_canonical_guidance_v2(
        **_base_kwargs(
            model=model,
            grid_id=grid_id,
            interval_fields=interval_fields,
            interval_start_hours=interval_start_hours,
        )
    )


class TestAssembleCanonicalGuidanceV2:
    def test_assembles_valid_dataset(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        assert dataset.attrs["schema_version"] == "canonical-guidance.v2"
        assert dataset.attrs["model"] == "nbm"
        assert "air_temperature_2m" in dataset.data_vars
        assert "liquid_equivalent_precipitation_amount_1h_interval_bounds" in dataset.data_vars

    def test_rejects_unknown_instantaneous_variable(self) -> None:
        kwargs = _base_kwargs(instantaneous_fields={"not_a_variable": np.zeros((3, 2, 2))})
        with pytest.raises(CanonicalGuidanceV2Error, match="unknown instantaneous"):
            assemble_canonical_guidance_v2(**kwargs)

    def test_rejects_wrong_shape(self) -> None:
        kwargs = _base_kwargs(instantaneous_fields={"air_temperature_2m": np.zeros((2, 2, 2))})
        with pytest.raises(CanonicalGuidanceV2Error, match="shape"):
            assemble_canonical_guidance_v2(**kwargs)

    def test_requires_interval_start_hours_for_every_lead(self) -> None:
        kwargs = _base_kwargs(
            interval_start_hours={
                "liquid_equivalent_precipitation_amount_1h": (0, 1),
                "probability_of_precipitation_1h": (0, 1, 2),
            }
        )
        with pytest.raises(CanonicalGuidanceV2Error, match="interval_start_hours"):
            assemble_canonical_guidance_v2(**kwargs)

    def test_interval_bounds_end_equals_valid_time(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        bounds = dataset["liquid_equivalent_precipitation_amount_1h_interval_bounds"].values
        valid_time = dataset["source_valid_time"].values
        assert np.array_equal(bounds[:, 1], valid_time.astype("datetime64[ns]"))


class TestValidateCanonicalGuidanceV2:
    def test_accepts_valid_dataset(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        validate_canonical_guidance_v2(dataset)  # does not raise

    def test_accepts_valid_hrrr_dataset_without_pop(self) -> None:
        kwargs = _base_kwargs(
            model="hrrr",
            grid_id="hrrr-conus.v1",
            interval_fields={"liquid_equivalent_precipitation_amount_1h": np.full((3, 2, 2), 1.0)},
            interval_start_hours={"liquid_equivalent_precipitation_amount_1h": (0, 1, 2)},
        )
        dataset = assemble_canonical_guidance_v2(**kwargs)
        validate_canonical_guidance_v2(dataset)  # does not raise

    def test_rejects_wrong_schema_version(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        dataset.attrs["schema_version"] = "canonical-guidance.v1"
        with pytest.raises(CanonicalGuidanceV2Error, match="schema_version"):
            validate_canonical_guidance_v2(dataset)

    def test_rejects_unknown_model(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        dataset.attrs["model"] = "rrfs"
        with pytest.raises(CanonicalGuidanceV2Error, match="model"):
            validate_canonical_guidance_v2(dataset)

    def test_never_accepted_by_v1_validator(self) -> None:
        """Section 5.1: canonical-guidance.v2 must never enter the v1
        validator path -- a v2 dataset's schema_version alone must
        differ enough that any caller routing by exact version cannot
        confuse the two."""
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        assert dataset.attrs["schema_version"] != "canonical-guidance.v1"

    def test_rejects_wrong_unit(self) -> None:
        """Independent probe: mutating a data variable's unit_id must be
        rejected, not silently accepted."""
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        dataset["air_temperature_2m"].attrs["unit_id"] = "degree"
        with pytest.raises(CanonicalGuidanceV2Error, match="unit_id"):
            validate_canonical_guidance_v2(dataset)

    def test_rejects_2099_valid_time(self) -> None:
        """Independent probe: source_valid_time must equal
        forecast_reference_time + source_lead_time exactly; a mutated
        far-future valid time must be rejected."""
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        bad_valid_time = np.full(
            dataset["source_valid_time"].shape, np.datetime64("2099-01-01T00:00:00")
        )
        dataset = dataset.assign_coords(
            source_valid_time=("source_lead_time", bad_valid_time.astype("datetime64[ns]"))
        )
        with pytest.raises(CanonicalGuidanceV2Error, match="source_valid_time"):
            validate_canonical_guidance_v2(dataset)

    def test_rejects_missing_required_hrrr_field(self) -> None:
        """Independent probe: an HRRR dataset missing a required field
        (e.g. wind_gust_10m) must be rejected."""
        kwargs = _base_kwargs(
            model="hrrr",
            grid_id="hrrr-conus.v1",
            instantaneous_fields={
                "air_temperature_2m": np.full((3, 2, 2), 280.0),
                "dew_point_temperature_2m": np.full((3, 2, 2), 275.0),
                "eastward_wind_10m": np.full((3, 2, 2), 3.0),
                "northward_wind_10m": np.full((3, 2, 2), 2.0),
            },
            interval_fields={"liquid_equivalent_precipitation_amount_1h": np.full((3, 2, 2), 1.0)},
            interval_start_hours={"liquid_equivalent_precipitation_amount_1h": (0, 1, 2)},
        )
        dataset = assemble_canonical_guidance_v2(**kwargs)
        with pytest.raises(CanonicalGuidanceV2Error, match="missing required canonical variable"):
            validate_canonical_guidance_v2(dataset)

    def test_rejects_arbitrary_grid_id(self) -> None:
        """Independent probe: grid_id must be a well-formed identifier,
        not an arbitrary string."""
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        dataset.attrs["grid_id"] = "!!! not a grid id !!!"
        with pytest.raises(CanonicalGuidanceV2Error, match="grid_id"):
            validate_canonical_guidance_v2(dataset)

    def test_rejects_wrong_configuration_snapshot_id(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        dataset.attrs["configuration_snapshot_id"] = "not-a-real-snapshot-id"
        with pytest.raises(CanonicalGuidanceV2Error, match="configuration_snapshot_id"):
            validate_canonical_guidance_v2(dataset)

    def test_rejects_out_of_bound_value(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        dataset["air_temperature_2m"].values[...] = 999.0
        with pytest.raises(CanonicalGuidanceV2Error, match="physically plausible bound"):
            validate_canonical_guidance_v2(dataset)

    def test_rejects_wrong_interval_width(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        bounds = dataset["liquid_equivalent_precipitation_amount_1h_interval_bounds"].values.copy()
        bounds[:, 0] = bounds[:, 0] - np.timedelta64(1, "h")
        dataset["liquid_equivalent_precipitation_amount_1h_interval_bounds"].values[...] = bounds
        with pytest.raises(CanonicalGuidanceV2Error, match="interval width"):
            validate_canonical_guidance_v2(dataset)

    def test_rejects_extra_variable_and_unapproved_mask_value(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        dataset["unexpected"] = dataset["air_temperature_2m"].copy()
        dataset["air_temperature_2m_quality_mask"].values[0, 0, 0] = 2
        with pytest.raises(CanonicalGuidanceV2Error, match="unapproved"):
            validate_canonical_guidance_v2(dataset)

    def test_rejects_missing_interval_bounds(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs()).drop_vars(
            "probability_of_precipitation_1h_interval_bounds"
        )
        with pytest.raises(CanonicalGuidanceV2Error, match="mandatory bounds"):
            validate_canonical_guidance_v2(dataset)

    def test_rejects_interval_bounds_on_instantaneous_variable(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        dataset["air_temperature_2m_interval_bounds"] = dataset[
            "probability_of_precipitation_1h_interval_bounds"
        ].copy()
        with pytest.raises(CanonicalGuidanceV2Error, match="must not have interval bounds"):
            validate_canonical_guidance_v2(dataset)

    @pytest.mark.parametrize(
        ("variable", "value"),
        (("air_temperature_2m", 179.0), ("wind_gust_10m", 100.1)),
    )
    def test_rejects_exact_plan_bound_mutations(self, variable: str, value: float) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        dataset[variable].values[0, 0, 0] = value
        with pytest.raises(CanonicalGuidanceV2Error, match="physically plausible bound"):
            validate_canonical_guidance_v2(dataset)

    def test_rejects_dew_point_above_temperature(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        dataset["dew_point_temperature_2m"].values[0, 0, 0] = 281.0
        with pytest.raises(CanonicalGuidanceV2Error, match="must not exceed"):
            validate_canonical_guidance_v2(dataset)
