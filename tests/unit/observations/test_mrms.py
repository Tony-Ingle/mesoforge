"""Real GRIB messages exercise the small, product-specific MRMS contract."""

from __future__ import annotations

import gzip
import hashlib
from datetime import datetime
from typing import Any

import eccodes
import pytest

from mesoforge.observations import mrms

QPE = "MultiSensor_QPE_01H_Pass2"
GAUGE = "GaugeInflIndex_01H_Pass2"
RADAR = "RadarAccumulationQualityIndex_01H"


def make_mrms_message(
    product: str = QPE,
    values: tuple[float, ...] = (1.5, 0, -1, -3),
    *,
    overrides: dict[str, Any] | None = None,
    compressed: bool = True,
) -> bytes:
    """Small aligned native-grid subset; also reused by ingestion/storage tests."""
    contract = mrms.PRODUCT_CONTRACTS[product]
    handle = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        keys = {
            "centre": 161,
            "subCentre": 0,
            "discipline": 209,
            "tablesVersion": 255,
            "localTablesVersion": 1,
            "parameterCategory": contract.category,
            "parameterNumber": contract.parameter,
            "significanceOfReferenceTime": 3,
            "dataDate": 20260924,
            "dataTime": 1200,
            "forecastTime": 0,
            "shapeOfTheEarth": 2,
            "Ni": 2,
            "Nj": 2,
            "latitudeOfFirstGridPointInDegrees": 45.005,
            "longitudeOfFirstGridPointInDegrees": 266.735,
            "latitudeOfLastGridPointInDegrees": 44.995,
            "longitudeOfLastGridPointInDegrees": 266.745,
            "iDirectionIncrementInDegrees": 0.01,
            "jDirectionIncrementInDegrees": 0.01,
            "scanningMode": 0,
            "packingType": "grid_simple",
            "bitsPerValue": 24,
        }
        for key, value in keys.items():
            eccodes.codes_set(handle, key, value)
        for key, value in (overrides or {}).items():
            eccodes.codes_set(handle, key, value)
        eccodes.codes_set_values(handle, values)
        raw = bytes(eccodes.codes_get_message(handle))
        return gzip.compress(raw, mtime=0) if compressed else raw
    finally:
        eccodes.codes_release(handle)


def _extract(raw: bytes, *, product: str = QPE, **kwargs: float) -> dict[str, Any]:
    return mrms.extract_mrms(
        raw,
        product=product,
        latitude=kwargs.get("latitude", 45.005),
        longitude=kwargs.get("longitude", -93.265),
    )


def test_qpe_contract_derives_exact_hour_from_documentation_not_template_zero() -> None:
    raw = make_mrms_message()
    result = _extract(raw)
    assert result["grib"]["productDefinitionTemplateNumber"] == 0
    assert result["grib"]["decoded_units"] == "unknown"
    assert result["grib"]["algorithm_version"] is None
    contract = result["product_contract"]
    assert (contract["discipline"], contract["category"], contract["parameter"]) == (209, 6, 37)
    assert contract["contract_id"] == "mrms.multisensor-qpe-01h-pass2.v1"
    assert contract["units"] == "mm"
    temporal = result["temporal"]
    assert temporal["product_time"] == temporal["interval_end"] == "2026-09-24T12:00:00Z"
    assert temporal["interval_start"] == "2026-09-24T11:00:00Z"
    assert temporal["closure"] == "(start,end]"
    assert temporal["duration_seconds"] == 3600
    assert temporal["encoded_statistical_bounds"] is None
    assert temporal["semantics_origin"] == "documented_product_contract"
    assert temporal["semantics_source"] == "https://vlab.noaa.gov/web/wdtd/-/multi-sensor-qpe"
    assert (
        datetime.fromisoformat(temporal["interval_end"])
        - datetime.fromisoformat(temporal["interval_start"])
    ).total_seconds() == 3600
    assert result["value"] == {"state": "positive", "value": 1.5, "units": "mm", "raw_value": 1.5}
    assert result["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["message_sha256"] == hashlib.sha256(gzip.decompress(raw)).hexdigest()


@pytest.mark.parametrize(
    ("latitude", "longitude", "index", "state", "amount", "native"),
    [
        (45.005, -93.255, 1, "zero", 0, 0),
        (44.995, -93.265, 2, "missing", None, -1),
        (44.995, -93.255, 3, "no_coverage", None, -3),
    ],
)
def test_zero_missing_no_coverage_are_distinct(
    latitude: float, longitude: float, index: int, state: str, amount: float | None, native: float
) -> None:
    result = _extract(make_mrms_message(), latitude=latitude, longitude=longitude)
    assert result["value"] == {"state": state, "value": amount, "units": "mm", "raw_value": native}
    assert result["extraction"]["grid_index"] == index
    assert result["extraction"]["distance_m"] < 1e-6


def test_nearest_native_point_records_grid_identity_distance_and_deterministic_tie() -> None:
    raw = make_mrms_message()
    result = _extract(raw, latitude=45.005, longitude=-93.26)
    selected = result["extraction"]
    assert selected["grid_index"] == 0  # equal-longitude-distance tie -> lower scan index
    assert selected["row"] == selected["column"] == 0
    assert selected["native_coordinate"] == pytest.approx(
        {"latitude": 45.005, "longitude": -93.265}
    )
    assert 394.19 < selected["distance_m"] < 394.22
    assert selected["forecast_coordinate"] == {"latitude": 45.005, "longitude": -93.26}
    assert selected["policy_id"] == mrms.EXTRACTION_POLICY
    assert result["grid"]["spacing_latitude_degrees"] == 0.01
    assert result["grid"]["is_native_aligned_subset"] is True
    assert len(result["grid"]["identity_sha256"]) == 64
    # No interpolation: nearest-cell value, not the 0.75 mm average.
    assert result["value"]["value"] == 1.5
    with pytest.raises(mrms.MRMSContractError, match="outside"):
        _extract(raw, latitude=44.8)


@pytest.mark.parametrize(
    "overrides",
    [
        {"parameterNumber": 36},
        {"centre": 7},
        {"localTablesVersion": 2},
        {"dataTime": 1230},
        {"forecastTime": 1},
        {"iDirectionIncrementInDegrees": 0.02},
        {"shapeOfTheEarth": 6},
        {"scanningMode": 64},
    ],
)
def test_wrong_product_time_or_grid_fails(overrides: dict[str, Any]) -> None:
    with pytest.raises(mrms.MRMSContractError):
        _extract(make_mrms_message(overrides=overrides))


def test_known_contradictory_units_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    original = eccodes.codes_get

    def get(handle: int, key: str) -> Any:
        return "K" if key == "units" else original(handle, key)

    monkeypatch.setattr(eccodes, "codes_get", get)
    with pytest.raises(mrms.MRMSContractError, match="units contradict"):
        _extract(make_mrms_message())


def test_future_statistical_bounds_must_agree_with_contract() -> None:
    keys = {
        "productDefinitionTemplateNumber": 8,
        "indicatorOfUnitOfTimeRange": 1,
        "forecastTime": -1,
        "typeOfStatisticalProcessing": 1,
        "numberOfTimeRange": 1,
        "indicatorOfUnitForTimeRange": 1,
        "lengthOfTimeRange": 1,
        "yearOfEndOfOverallTimeInterval": 2026,
        "monthOfEndOfOverallTimeInterval": 9,
        "dayOfEndOfOverallTimeInterval": 24,
        "hourOfEndOfOverallTimeInterval": 12,
        "minuteOfEndOfOverallTimeInterval": 0,
        "secondOfEndOfOverallTimeInterval": 0,
    }
    result = _extract(make_mrms_message(overrides=keys))
    assert result["temporal"]["encoded_statistical_bounds"] == {
        "interval_start": "2026-09-24T11:00:00Z",
        "interval_end": "2026-09-24T12:00:00Z",
    }
    for change in (
        {"lengthOfTimeRange": 6},
        {"hourOfEndOfOverallTimeInterval": 13},
        {"typeOfStatisticalProcessing": 0},
        {"forecastTime": 0},
    ):
        with pytest.raises(mrms.MRMSContractError, match="contradicts"):
            _extract(make_mrms_message(overrides=keys | change))


def test_quality_is_separately_timed_aligned_evidence_without_rejection_threshold() -> None:
    qpe = _extract(make_mrms_message())
    for product in (GAUGE, RADAR):
        support = _extract(make_mrms_message(product, (0.1,) * 4), product=product)
        mrms.validate_support_alignment(qpe, support)
        assert support["value"]["state"] == "positive"
        assert support["value"]["units"] == "1"
        assert support["temporal"]["interval_start"] is None
        assert support["temporal"]["interval_end"] is None
        assert support["temporal"]["semantics_origin"] == "hourly_support_association"
        assert support["temporal"]["encoded_statistical_bounds"] is None
        later = _extract(make_mrms_message(product, overrides={"dataTime": 1300}), product=product)
        with pytest.raises(mrms.MRMSContractError, match="not aligned"):
            mrms.validate_support_alignment(qpe, later)
        other_cell = _extract(make_mrms_message(product), product=product, latitude=44.995)
        with pytest.raises(mrms.MRMSContractError, match="not aligned"):
            mrms.validate_support_alignment(qpe, other_cell)


def test_offline_replay_and_revision_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Offline normalization attempted network access")

    monkeypatch.setattr(socket, "create_connection", no_network)
    raw = make_mrms_message()
    first = _extract(raw)
    assert first == _extract(raw)
    changed = _extract(make_mrms_message(values=(2.0, 0, -1, -3)))
    assert changed["raw_sha256"] != first["raw_sha256"]
    assert changed["temporal"] == first["temporal"]
    assert changed["extraction"] == first["extraction"]
    assert changed["value"]["value"] == 2.0
    assert first["value"]["value"] == 1.5  # no mutation of prior normalized revision
    recompressed = _extract(gzip.compress(gzip.decompress(raw), mtime=1))
    assert recompressed["raw_sha256"] != first["raw_sha256"]
    assert recompressed["message_sha256"] == first["message_sha256"]
    assert recompressed["value"] == first["value"]


def test_malformed_truncated_multiple_messages_and_unknown_negative_are_not_missing() -> None:
    raw = make_mrms_message(compressed=False)
    for broken in (b"garbage", raw[:-1], raw + raw, gzip.compress(raw)[:-5]):
        with pytest.raises(mrms.MRMSContractError):
            _extract(broken)
    with pytest.raises(mrms.MRMSContractError, match="sentinel"):
        _extract(make_mrms_message(values=(-2, 0, -1, -3)))


def test_compressed_expansion_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = make_mrms_message()
    monkeypatch.setattr(mrms, "_MAX_MESSAGE_BYTES", len(gzip.decompress(raw)) - 1)
    with pytest.raises(mrms.MRMSContractError, match="complete GRIB"):
        _extract(raw)


def test_decoder_generic_missing_value_is_not_a_native_sentinel() -> None:
    result = _extract(make_mrms_message(values=(9999, 0, -1, -3)))
    assert result["value"]["state"] == "positive"
    assert result["value"]["value"] == 9999
