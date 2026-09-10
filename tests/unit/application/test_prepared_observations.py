"""Focused offline preparation checks using synthetic, provider-shaped METAR bytes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from mesoforge.application.prepared_observations import (
    acquire_bundle,
    load_bundle,
    normalize_rows,
)
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.common.errors import IntegrityError
from mesoforge.common.identifiers import ArtifactId
from tests.support.phase1_fixture_transports import (
    FakeHttpResponse,
    FixedClock,
    FixtureAviationWeatherTransport,
    RecordingSleeper,
)

ROOT = Path(__file__).resolve().parents[3]
START = datetime(2026, 9, 10, 17, tzinfo=UTC)
END = START + timedelta(hours=3)
ACQUIRED = END + timedelta(hours=1)


def _record(**overrides: Any) -> dict[str, Any]:
    """Invented provider-format input; it is not a captured real observation."""
    value = {
        "icaoId": "KROS",
        "obsTime": int((START + timedelta(minutes=10)).timestamp()),
        "reportTime": (START + timedelta(minutes=11)).isoformat(),
        "receiptTime": (START + timedelta(minutes=12)).isoformat(),
        "metarType": "METAR",
        "rawOb": "SYNTHETIC KROS 101710Z 18005KT 10SM CLR 20/10 A3000",
        "lat": 45.69624,
        "lon": -92.95427,
        "elev": 282.0,
        "temp": 20.0,
        "dewp": 10.0,
        "wdir": 180.0,
        "wspd": 5.0,
        "qcField": 12.0,
        "unusedProviderField": {"retained": True},
    }
    return {**value, **overrides}


def _acquire(directory: Path, **overrides: Any) -> tuple[dict[str, Any], bytes, list[str]]:
    payload = json.dumps([_record()], indent=2).encode()
    transport = FixtureAviationWeatherTransport()
    transport.metar_queue.append(FakeHttpResponse(200, {"ETag": '"fixture"'}, payload))
    clock = FixedClock(ACQUIRED)
    arguments = {
        "latitude": 45.8,
        "longitude": -93.1,
        "start_valid_time": START,
        "end_valid_time": END,
        "transport": transport,
        "clock": clock,
        "sleeper": RecordingSleeper(clock),
        **overrides,
    }
    metadata = acquire_bundle(directory, **arguments)
    return metadata, payload, transport.get_calls


def test_fixed_acquisition_pads_time_and_preserves_exact_bytes_for_offline_readback(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "retained"
    metadata, payload, calls = _acquire(directory)
    assert len(calls) == 1
    query = parse_qs(urlsplit(calls[0]).query)
    assert set(query["ids"][0].split(",")) == {"KCBG", "KJMR", "KROS"}
    assert query["date"] == ["2026-09-10T20:15:00Z"]
    assert float(query["hours"][0]) == 3.5
    assert (directory / "metar.json").read_bytes() == payload
    replay_metadata, configuration, replay_payload = load_bundle(directory)
    assert replay_metadata == metadata
    assert replay_payload == payload
    assert configuration.phase2 is not None
    assert configuration.phase2.aviationweather.metar_window_hours == 6.5
    assert len(calls) == 1


@pytest.mark.parametrize("filename", ["metar.json", "configuration.json"])
def test_offline_bundle_load_rejects_tampered_retained_bytes(tmp_path: Path, filename: str) -> None:
    directory = tmp_path / "retained"
    _acquire(directory)
    target = directory / filename
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises((IntegrityError, ValueError)):
        load_bundle(directory)


@pytest.mark.parametrize(
    "overrides",
    [
        {"latitude": 44.0},
        {"start_valid_time": START.replace(tzinfo=None)},
        {"end_valid_time": START},
        {"start_valid_time": START - timedelta(hours=4)},
        {"end_valid_time": ACQUIRED + timedelta(hours=1)},
    ],
)
def test_invalid_acquisition_scope_fails_before_network_or_files(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    directory = tmp_path / "retained"
    transport = FixtureAviationWeatherTransport()
    with pytest.raises(ValueError):
        _acquire(directory, transport=transport, **overrides)
    assert transport.get_calls == []
    assert not directory.exists()


def test_acquisition_does_not_overwrite_existing_raw_directory(tmp_path: Path) -> None:
    directory = tmp_path / "retained"
    _acquire(directory)
    retained = {path.name: path.read_bytes() for path in directory.iterdir()}
    transport = FixtureAviationWeatherTransport()
    with pytest.raises((ValueError, FileExistsError)):
        _acquire(directory, transport=transport)
    assert transport.get_calls == []
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == retained


def test_normalization_preserves_time_qc_revision_and_original_record_indices() -> None:
    configuration, _ = load_configuration_source(
        base_path=ROOT / "configs/base.yaml",
        environment_path=ROOT / "configs/phase1-grasston.yaml",
        additional_overlay_paths=(ROOT / "configs/phase2-grasston.yaml",),
    )
    raw = ArtifactId("art_00000000-0000-0000-0000-000000000001")
    snapshot = ArtifactId("art_00000000-0000-0000-0000-000000000002")
    rows = [
        _record(icaoId="KMSP"),
        _record(obsTime=int((START - timedelta(hours=1)).timestamp())),
        _record(wspd=None),
        _record(temp=400.0),
    ]
    parameters = {
        "configuration": configuration,
        "station_ids": ("KCBG", "KJMR", "KROS"),
        "raw_artifact_id": raw,
        "station_snapshot_artifact_id": snapshot,
        "ingested_at": ACQUIRED,
        "query_window_start": START - timedelta(minutes=15),
        "query_window_end": END + timedelta(minutes=15),
    }
    result = normalize_rows(json.dumps(rows).encode(), **parameters)
    accepted, qc_rejected = result["rows"]
    assert accepted["raw_record_index"] == 2
    assert accepted["raw_artifact_id"] == str(raw)
    assert accepted["station_snapshot_artifact_id"] == str(snapshot)
    assert accepted["station_id"] == "station.kros"
    assert accepted["provider_station_id"] == "KROS"
    assert accepted["temperature_k"] == pytest.approx(293.15)
    assert datetime.fromisoformat(accepted["event_time"]) == START + timedelta(minutes=10)
    assert datetime.fromisoformat(accepted["report_time"]) == START + timedelta(minutes=11)
    assert datetime.fromisoformat(accepted["provider_available_at"]) == START + timedelta(
        minutes=12
    )
    assert datetime.fromisoformat(accepted["ingested_at"]) == ACQUIRED
    assert accepted["provider_qc_field"] == 12.0
    assert accepted["mesoforge_qc_state"] == "partial"
    assert "wind_speed_missing" in accepted["quality_flags"]
    assert qc_rejected["raw_record_index"] == 3
    assert qc_rejected["temperature_k"] is None
    assert "temperature_out_of_range" in qc_rejected["quality_flags"]
    assert accepted["logical_observation_digest"] == qc_rejected["logical_observation_digest"]
    assert accepted["revision_digest"] != qc_rejected["revision_digest"]
    assert accepted["raw_record_digest"] != qc_rejected["raw_record_digest"]
    assert [excluded["raw_record_index"] for excluded in result["excluded"]] == [0, 1]
    assert all(excluded["reason"] for excluded in result["excluded"])
    assert normalize_rows(json.dumps(rows).encode(), **parameters) == result
