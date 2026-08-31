"""Unit tests for mesoforge.contracts.observations_v2 (plan Section
6.1, Task 11): metar-observations.v2 schema additions.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mesoforge.contracts.observations_v2 import NormalizedObservationV2, RawMetarRecordV2

_NOW = datetime(2026, 8, 30, 18, 0, tzinfo=UTC)


def _raw_kwargs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "icao_id": "KCBG",
        "obs_time": _NOW,
        "report_time": _NOW,
        "receipt_time": _NOW,
        "temp": 20.0,
        "dewp": 15.0,
        "wdir": 180.0,
        "wspd": 10.0,
        "wgst": 18.0,
        "qc_field": 1.0,
        "metar_type": "METAR",
        "raw_ob": "KCBG 301800Z 18010G18KT 10SM CLR 20/15 A3000",
        "lat": 45.5,
        "lon": -93.2,
        "elev": 285.0,
    }
    values.update(overrides)
    return values


class TestRawMetarRecordV2:
    def test_accepts_valid_record(self) -> None:
        record = RawMetarRecordV2(**_raw_kwargs())  # type: ignore[arg-type]
        assert record.dewp == 15.0
        assert record.wgst == 18.0

    def test_gust_and_dew_point_are_optional(self) -> None:
        record = RawMetarRecordV2(**_raw_kwargs(dewp=None, wgst=None))  # type: ignore[arg-type]
        assert record.dewp is None
        assert record.wgst is None

    def test_rejects_non_finite_dewp(self) -> None:
        with pytest.raises(ValidationError, match="finite"):
            RawMetarRecordV2(**_raw_kwargs(dewp=float("nan")))  # type: ignore[arg-type]


def _normalized_kwargs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "logical_observation_digest": "sha256:" + "0" * 64,
        "revision_digest": "sha256:" + "1" * 64,
        "station_id": "station.kcbg",
        "provider_station_id": "KCBG",
        "event_time": _NOW,
        "report_time": _NOW,
        "provider_available_at": _NOW,
        "ingested_at": _NOW,
        "metar_type": "METAR",
        "raw_observation": "KCBG 301800Z 18010G18KT 10SM CLR 20/15 A3000",
        "raw_record_digest": "sha256:" + "2" * 64,
        "raw_artifact_id": "art_00000000-0000-4000-8000-000000000000",
        "raw_record_index": 0,
        "station_snapshot_artifact_id": "art_00000000-0000-4000-8000-000000000001",
        "latitude_degrees": 45.5,
        "longitude_degrees": -93.2,
        "elevation_m": 285.0,
        "mesoforge_qc_state": "eligible",
    }
    values.update(overrides)
    return values


class TestNormalizedObservationV2:
    def test_accepts_missing_precipitation(self) -> None:
        obs = NormalizedObservationV2(**_normalized_kwargs())  # type: ignore[arg-type]
        assert obs.precipitation_truth_status == "missing"
        assert obs.precipitation_amount_kg_m2 is None

    def test_reported_requires_amount(self) -> None:
        with pytest.raises(ValidationError, match="requires a non-null"):
            NormalizedObservationV2(**_normalized_kwargs(precipitation_truth_status="reported"))  # type: ignore[arg-type]

    def test_missing_status_rejects_nonnull_amount(self) -> None:
        with pytest.raises(ValidationError, match="requires precipitation_amount_kg_m2 to be null"):
            NormalizedObservationV2(
                **_normalized_kwargs(
                    precipitation_truth_status="missing", precipitation_amount_kg_m2=1.0
                )
            )  # type: ignore[arg-type]

    def test_accepts_reported_with_amount(self) -> None:
        obs = NormalizedObservationV2(
            **_normalized_kwargs(
                precipitation_truth_status="reported",
                precipitation_amount_kg_m2=3.0,
                precipitation_interval_start=_NOW,
                precipitation_interval_end=_NOW,
            )
        )  # type: ignore[arg-type]
        assert obs.precipitation_amount_kg_m2 == 3.0

    def test_rejects_non_finite_gust(self) -> None:
        with pytest.raises(ValidationError, match="finite"):
            NormalizedObservationV2(**_normalized_kwargs(wind_gust_m_s=float("inf")))  # type: ignore[arg-type]
