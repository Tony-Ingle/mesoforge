from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import xarray as xr

from mesoforge.catalog.configuration import MatchingPolicy
from mesoforge.contracts.observations_v2 import NormalizedObservationV2
from mesoforge.verification.matching import match_baseline_to_observations_v2

ART = "art_00000000-0000-0000-0000-000000000001"
DIGEST = "sha256:" + "a" * 64
STATIONS = ("station.kcbg", "station.kjmr", "station.kros")


def _baseline() -> xr.Dataset:
    shape = (36, 3)
    valid = np.datetime64("2026-01-01T00:00") + np.arange(1, 37).astype("timedelta64[h]")
    variables = {
        "air_temperature_2m": 280.0,
        "dew_point_temperature_2m": 275.0,
        "eastward_wind_10m": 3.0,
        "northward_wind_10m": 4.0,
        "wind_gust_10m": 7.0,
        "probability_of_precipitation_1h": 0.6,
        "liquid_equivalent_precipitation_amount_1h": 1.0,
    }
    data: dict[str, object] = {}
    for name, value in variables.items():
        data[name] = (("target_horizon", "location"), np.full(shape, value))
        data[f"{name}_state"] = (("target_horizon", "location"), np.zeros(shape, dtype=np.uint8))
    return xr.Dataset(
        data,
        coords={
            "target_horizon": range(1, 37),
            "location": list(STATIONS),
            "target_valid_time": ("target_horizon", valid),
        },
    )


def _observation(
    *,
    available: datetime,
    interval_start: datetime | None = None,
    interval_end: datetime | None = None,
    revision: str = "b",
) -> NormalizedObservationV2:
    event = datetime(2026, 1, 1, 1, tzinfo=UTC)
    end = interval_end or event
    return NormalizedObservationV2(
        logical_observation_digest=DIGEST,
        revision_digest="sha256:" + revision * 64,
        station_id="station.kcbg",
        provider_station_id="KCBG",
        event_time=event,
        report_time=event,
        provider_available_at=available,
        ingested_at=available,
        metar_type="METAR",
        raw_observation="KCBG P0004",
        raw_record_digest=DIGEST,
        raw_artifact_id=ART,
        raw_record_index=0,
        station_snapshot_artifact_id=ART,
        latitude_degrees=45.0,
        longitude_degrees=-93.0,
        elevation_m=290.0,
        temperature_k=279.0,
        dew_point_k=274.0,
        wind_speed_m_s=5.0,
        wind_from_direction_degrees=216.86989765,
        eastward_wind_10m_m_s=3.0,
        northward_wind_10m_m_s=4.0,
        wind_gust_m_s=8.0,
        precipitation_amount_kg_m2=1.016,
        precipitation_truth_status="reported",
        precipitation_interval_start=interval_start or end - timedelta(hours=1),
        precipitation_interval_end=end,
        mesoforge_qc_state="eligible",
    )


def _match(observations: list[NormalizedObservationV2], *, cutoff: datetime):
    return match_baseline_to_observations_v2(
        baseline=_baseline(),
        station_ids=STATIONS,
        target_horizons=tuple(range(1, 37)),
        observations=observations,
        matching_policy=MatchingPolicy(matching_policy_id="metar-nearest-15m.v1"),
        verification_cutoff=cutoff,
        baseline_artifact_id=ART,
        observations_artifact_id=ART,
    )


def test_full_coverage_is_three_stations_by_36_horizons() -> None:
    rows = _match([], cutoff=datetime(2026, 1, 2, tzinfo=UTC))
    assert len(rows) == 108
    assert len({(str(row.station_id), row.target_horizon_hours) for row in rows}) == 108
    assert all(row.row_status == "matched_no_fields" for row in rows)


def test_revision_cutoff_and_exact_precipitation_interval_matching() -> None:
    cutoff = datetime(2026, 1, 1, 2, tzinfo=UTC)
    older = _observation(available=datetime(2026, 1, 1, 1, 10, tzinfo=UTC), revision="b")
    later = _observation(available=datetime(2026, 1, 1, 3, tzinfo=UTC), revision="c")
    row = _match([older, later], cutoff=cutoff)[0]
    assert row.selected_revision_digest == older.revision_digest
    assert row.qpf_status == "matched"
    assert row.pop_status == "matched"

    wrong_interval = _observation(
        available=cutoff, interval_end=datetime(2026, 1, 1, 1, 16, tzinfo=UTC)
    )
    row = _match([wrong_interval], cutoff=cutoff)[0]
    assert row.qpf_status == "precipitation_interval_mismatch"
    assert row.pop_status == "precipitation_interval_mismatch"


def test_precipitation_interval_uses_end_time_tolerance_but_requires_one_hour() -> None:
    cutoff = datetime(2026, 1, 1, 2, tzinfo=UTC)
    for minute_offset in (-15, 15):
        interval_end = datetime(2026, 1, 1, 1, tzinfo=UTC) + timedelta(minutes=minute_offset)
        row = _match([_observation(available=cutoff, interval_end=interval_end)], cutoff=cutoff)[0]
        assert row.temperature_status == "matched"
        assert row.wind_speed_status == "matched"
        assert row.qpf_status == "matched"
        assert row.pop_status == "matched"

    wrong_duration = _observation(
        available=cutoff,
        interval_start=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        interval_end=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )
    row = _match([wrong_duration], cutoff=cutoff)[0]
    assert row.qpf_status == "precipitation_interval_mismatch"
    assert row.pop_status == "precipitation_interval_mismatch"


def test_only_post_cutoff_revision_is_explicitly_unavailable() -> None:
    cutoff = datetime(2026, 1, 1, 2, tzinfo=UTC)
    later = _observation(available=datetime(2026, 1, 1, 3, tzinfo=UTC))
    row = _match([later], cutoff=cutoff)[0]
    assert row.temperature_status == "revision_after_cutoff"
    assert row.selected_revision_digest is None
