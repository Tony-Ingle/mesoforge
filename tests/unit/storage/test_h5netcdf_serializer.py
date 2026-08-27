"""Unit tests for storage/netcdf.py:H5NetcdfDatasetSerializer (Task 5,
plan Section 4.6).

RED: written before src/mesoforge/storage/netcdf.py exists. Round trips
through in-memory bytes only -- no NetCDF file is committed to the repo.
"""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from mesoforge.storage.netcdf import H5NetcdfDatasetSerializer
from tests.fixtures.synthetic import build_synthetic_dataset


class TestH5NetcdfDatasetSerializer:
    def test_round_trip_preserves_structure(self) -> None:
        dataset = build_synthetic_dataset()
        serializer = H5NetcdfDatasetSerializer()

        payload = serializer.serialize(dataset)
        assert isinstance(payload, bytes)
        assert len(payload) > 0

        restored = serializer.deserialize(payload)

        assert set(restored.data_vars) == set(dataset.data_vars)
        assert set(restored.dims) == set(dataset.dims)
        for name in dataset.data_vars:
            assert restored[name].dtype == dataset[name].dtype
            assert restored[name].dims == dataset[name].dims
            np.testing.assert_array_equal(restored[name].values, dataset[name].values)

    def test_round_trip_preserves_coordinates(self) -> None:
        dataset = build_synthetic_dataset()
        serializer = H5NetcdfDatasetSerializer()
        restored = serializer.deserialize(serializer.serialize(dataset))

        np.testing.assert_array_equal(
            restored["forecast_reference_time"].values,
            dataset["forecast_reference_time"].values,
        )
        np.testing.assert_array_equal(restored["lead_time"].values, dataset["lead_time"].values)
        np.testing.assert_array_equal(restored["valid_time"].values, dataset["valid_time"].values)
        np.testing.assert_array_equal(restored["x"].values, dataset["x"].values)
        np.testing.assert_array_equal(restored["y"].values, dataset["y"].values)

    def test_round_trip_preserves_attributes(self) -> None:
        dataset = build_synthetic_dataset()
        serializer = H5NetcdfDatasetSerializer()
        restored = serializer.deserialize(serializer.serialize(dataset))

        for key, value in dataset.attrs.items():
            assert restored.attrs[key] == value

    def test_round_trip_preserves_variable_attributes(self) -> None:
        dataset = build_synthetic_dataset()
        serializer = H5NetcdfDatasetSerializer()
        restored = serializer.deserialize(serializer.serialize(dataset))

        for key, value in dataset["air_temperature_2m"].attrs.items():
            assert restored["air_temperature_2m"].attrs[key] == value

    def test_round_trip_preserves_quality_mask(self) -> None:
        dataset = build_synthetic_dataset(quality_mask_bits=(1, 2, 4, 0, 0, 0, 0, 0))
        serializer = H5NetcdfDatasetSerializer()
        restored = serializer.deserialize(serializer.serialize(dataset))
        np.testing.assert_array_equal(
            restored["air_temperature_2m_quality_mask"].values,
            dataset["air_temperature_2m_quality_mask"].values,
        )

    def test_two_serializations_of_same_dataset_produce_same_logical_content(self) -> None:
        dataset = build_synthetic_dataset()
        serializer = H5NetcdfDatasetSerializer()
        first = serializer.deserialize(serializer.serialize(dataset))
        second = serializer.deserialize(serializer.serialize(dataset))
        xr.testing.assert_identical(first, second)

    def test_deserialize_rejects_garbage_bytes(self) -> None:
        serializer = H5NetcdfDatasetSerializer()
        with pytest.raises(Exception):  # noqa: B017 - backend-specific parse error
            serializer.deserialize(b"not a netcdf file")
