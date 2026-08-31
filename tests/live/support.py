"""Small, test-only safety boundary shared by provider live canaries."""

from __future__ import annotations

import os
import tempfile
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import xarray as xr

from mesoforge.catalog.sources import Phase2FieldContract, RetryPolicy
from mesoforge.guidance.http_fetch import fetch_with_range, fetch_with_retry, header
from mesoforge.guidance.index_parsing import (
    IndexRow,
    compute_message_byte_range,
    parse_index_rows,
)

MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_MESSAGE_BYTES = 32 * 1024 * 1024
GRIB_UNITS_BY_APPROVED_UNIT = {
    "K": frozenset({"K"}),
    "m/s": frozenset({"m s**-1", "m s-1"}),
    "degree": frozenset({"degrees", "degree true"}),
    "kg/m^2": frozenset({"kg m**-2", "kg m-2"}),
    "percent": frozenset({"%"}),
}


@dataclass(frozen=True)
class _Response:
    status_code: int
    headers: dict[str, str]
    content: bytes


class BoundedRequestsTransport:
    """Requests transport that refuses to buffer beyond a fixed byte cap."""

    def __init__(self) -> None:
        import requests

        self._session = requests.Session()

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> _Response:
        cap = MAX_MESSAGE_BYTES if headers and "Range" in headers else MAX_INDEX_BYTES
        response = self._session.get(
            url, headers=headers or {}, timeout=timeout, stream=True, allow_redirects=True
        )
        payload = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            payload.extend(chunk)
            if len(payload) > cap:
                response.close()
                raise RuntimeError(f"live response exceeded bounded cap of {cap} bytes")
        return _Response(response.status_code, dict(response.headers), bytes(payload))

    def head(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> _Response:
        response = self._session.head(
            url, headers=headers or {}, timeout=timeout, allow_redirects=True
        )
        return _Response(response.status_code, dict(response.headers), b"")


class _Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _NoSleep:
    def sleep(self, seconds: float) -> None:
        del seconds


def load_phase2_configuration() -> Any:
    from mesoforge.catalog.configuration import load_configuration_source

    config, _ = load_configuration_source(
        base_path=Path("configs/base.yaml"),
        environment_path=Path("configs/phase1-grasston.yaml"),
        additional_overlay_paths=(Path("configs/phase2-grasston.yaml"),),
    )
    assert config.phase2 is not None
    return config.phase2


def fetch_index(
    transport: BoundedRequestsTransport,
    *,
    endpoint: str,
    index_url: str,
    retry_policy: RetryPolicy,
    cycle: datetime,
    deadline_minutes: float,
    destination: Path,
) -> tuple[IndexRow, ...]:
    fetched = fetch_with_retry(
        transport,
        _Clock(),
        _NoSleep(),
        method="get",
        urls_by_endpoint=((endpoint, index_url),),
        retry_policy=retry_policy,
        cycle_deadline=cycle.replace(tzinfo=UTC) + timedelta(minutes=deadline_minutes),
    )
    assert len(fetched.payload) <= MAX_INDEX_BYTES
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(fetched.payload)
    return parse_index_rows(fetched.payload.decode("utf-8"))


def fetch_rows(
    transport: BoundedRequestsTransport,
    *,
    endpoint: str,
    grib_url: str,
    rows: tuple[IndexRow, ...],
    selected: tuple[IndexRow, ...],
    retry_policy: RetryPolicy,
    cycle: datetime,
    deadline_minutes: float,
    destination: Path,
) -> tuple[Path, ...]:
    destination.mkdir(parents=True, exist_ok=True)
    head = fetch_with_retry(
        transport,
        _Clock(),
        _NoSleep(),
        method="head",
        urls_by_endpoint=((endpoint, grib_url),),
        retry_policy=retry_policy,
        cycle_deadline=cycle.replace(tzinfo=UTC) + timedelta(minutes=deadline_minutes),
        accept_status=frozenset({200, 206}),
    )
    raw_length = header(head.headers, "Content-Length")
    assert raw_length is not None and int(raw_length) > 0
    full_length = int(raw_length)
    paths: list[Path] = []
    for ordinal, row in enumerate(selected):
        start, end = compute_message_byte_range(rows, selected=row, full_object_length=full_length)
        assert 0 < end - start <= MAX_MESSAGE_BYTES
        fetched = fetch_with_range(
            transport,
            _Clock(),
            _NoSleep(),
            endpoint=endpoint,
            url=grib_url,
            range_header=f"bytes={start}-{end - 1}",
            byte_start=start,
            byte_end=end,
            retry_policy=retry_policy,
            cycle_deadline=cycle.replace(tzinfo=UTC) + timedelta(minutes=deadline_minutes),
            expected_length=end - start,
            full_object_length=full_length,
        )
        path = destination / f"message-{ordinal:02d}.grib2"
        path.write_bytes(fetched.payload)
        paths.append(path)
    return tuple(paths)


def decode_contract_message(
    path: Path, *, contract: Phase2FieldContract, read_keys: tuple[str, ...]
) -> xr.DataArray:
    """Decode one ranged message and select it by authoritative GRIB keys."""
    import cfgrib

    from mesoforge.guidance.decoding import cfgrib_backend_kwargs

    # Keep any decoder scratch/index files under pytest's tmp_path too.
    previous_tempdir = tempfile.tempdir
    tempfile.tempdir = str(path.parent)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=FutureWarning, module=r"cfgrib\..*")
            datasets = cfgrib.open_datasets(
                str(path), backend_kwargs=cfgrib_backend_kwargs(read_keys)
            )
            datasets = [dataset.load() for dataset in datasets]
    finally:
        tempfile.tempdir = previous_tempdir
    matches = [
        data
        for dataset in datasets
        for data in dataset.data_vars.values()
        if data.attrs.get("GRIB_discipline") == contract.discipline
        and data.attrs.get("GRIB_parameterCategory") == contract.parameter_category
        and data.attrs.get("GRIB_parameterNumber") == contract.parameter_number
        and data.attrs.get("GRIB_typeOfLevel") == contract.type_of_level
    ]
    assert len(matches) == 1
    matched = cast(xr.DataArray, matches[0])
    assert matched.attrs["GRIB_units"] in GRIB_UNITS_BY_APPROVED_UNIT[contract.expected_unit_id]
    return matched


def require_live_cycle[T](model: str, transport_factory: Callable[[], T]) -> tuple[datetime, T]:
    """Validate both opt-ins before invoking the network transport factory."""
    if os.environ.get("MESOFORGE_LIVE_TESTS") != "1":
        pytest.skip("live tests require MESOFORGE_LIVE_TESTS=1 (opt-in only)")
    variable = f"MESOFORGE_LIVE_{model}_CYCLE"
    raw_cycle = os.environ.get(variable)
    if raw_cycle is None:
        pytest.skip(f"{variable}=YYYYMMDDTHH is required")
    try:
        cycle = datetime.strptime(raw_cycle, "%Y%m%dT%H")
    except ValueError:
        pytest.fail(f"{variable} must have exact format YYYYMMDDTHH")
    return cycle, transport_factory()
