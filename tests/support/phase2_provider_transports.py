"""Scripted Phase 2 provider fixture transports (Codex re-review
finding 1).

The Phase 2 acceptance proof previously replaced
``Phase2ProductionProvider.discover()`` outright and injected already-
completed ``Phase2LeadAcquisition``/``IndexRow``/``SelectedMessage``
objects. That bypassed everything the provider actually does -- candidate
discovery, index retrieval and parsing, selector matching and ambiguity
rejection, HEAD/Content-Length framing, byte-range framing and
integrity/GRIB2 boundary validation, retry/deadline/cutoff policy -- so a
green acceptance run proved nothing about the real acquisition path.

This module provides the *only* substitution the acceptance proof is
allowed to make: a deterministic in-process object implementing the
``guidance.interfaces.HttpTransport`` structural protocol
(``get``/``head`` returning ``status_code``/``headers``/``content``).
Every byte the provider sees is served here, exactly as a provider would
serve it:

- a GET of ``<grib_url>.idx`` returns a real wgrib2-style inventory whose
  rows carry the true byte offsets of the concatenated message stream;
- a HEAD of ``<grib_url>`` returns the true ``Content-Length``;
- a ranged GET of ``<grib_url>`` honours the exact ``Range`` header the
  production code sent and returns HTTP 206 with a matching
  ``Content-Range``, so range integrity and GRIB2 framing validation run
  for real.

``Phase2ProductionProvider.discover()`` and ``acquire()`` are then used
completely unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np

from mesoforge.catalog.sources import (
    GfsSourceSettings,
    HrrrPhase2SourceSettings,
    NbmSourceSettings,
)
from mesoforge.guidance.precipitation import compute_bucket_start
from tests.fixtures import gfs_grib, hrrr_grib, nbm_grib
from tests.support.phase1_fixture_transports import FakeHttpResponse

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d+)")

# The inventory descriptor text each canonical variable's selector
# matches, per model. These mirror the real provider inventories the
# production selector builders target (``guidance.sources.*``); the
# selectors themselves are never imported here, so a selector regression
# shows up as a zero-match GribIndexError rather than being masked.
_HRRR_DESCRIPTORS: dict[str, str] = {
    "air_temperature_2m": "TMP:2 m above ground",
    "dew_point_temperature_2m": "DPT:2 m above ground",
    "eastward_wind_10m": "UGRD:10 m above ground",
    "northward_wind_10m": "VGRD:10 m above ground",
    "wind_gust_10m": "GUST:surface",
}
_NBM_DESCRIPTORS: dict[str, str] = {
    "air_temperature_2m": "TMP:2 m above ground",
    "dew_point_temperature_2m": "DPT:2 m above ground",
    "wind_speed_10m": "WIND:10 m above ground",
    "wind_from_direction_10m": "WDIR:10 m above ground",
    "wind_gust_10m": "GUST:10 m above ground",
}
_GFS_DESCRIPTORS: dict[str, str] = {
    "air_temperature_2m": "TMP:2 m above ground",
    "dew_point_temperature_2m": "DPT:2 m above ground",
    "eastward_wind_10m": "UGRD:10 m above ground",
    "northward_wind_10m": "VGRD:10 m above ground",
    "wind_gust_10m": "GUST:surface",
}


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    """One inventory row plus the exact message bytes it points at."""

    descriptor: str
    payload: bytes


@dataclass
class LeadObject:
    """One model/cycle/lead's full published object: the concatenated
    GRIB2 message stream plus the inventory describing it."""

    entries: tuple[InventoryEntry, ...]
    cycle_date: date
    cycle_hour: int

    @property
    def body(self) -> bytes:
        return b"".join(entry.payload for entry in self.entries)

    @property
    def content_length(self) -> int:
        return len(self.body)

    def index_text(self) -> str:
        """A real wgrib2-style ``.idx`` whose byte offsets are the true
        offsets of each message in ``body`` -- so the production
        ``compute_message_byte_range`` derives correct ranges and the
        ranged GET below returns exactly one complete GRIB2 message."""
        lines: list[str] = []
        offset = 0
        date_tag = f"d={self.cycle_date:%Y%m%d}{self.cycle_hour:02d}"
        for number, entry in enumerate(self.entries, start=1):
            lines.append(f"{number}:{offset}:{date_tag}:{entry.descriptor}:")
            offset += len(entry.payload)
        return "\n".join(lines) + "\n"


class ScriptedProviderTransport:
    """Deterministic in-process ``HttpTransport`` serving one model's
    published objects.

    Only bytes cross this boundary. ``fault_script`` may map a URL
    substring to a queue of scripted failure responses (or raised
    transport errors) consumed before the successful response, so
    retry/failover/deadline behaviour is exercised for real.
    """

    def __init__(
        self,
        *,
        objects: dict[str, LeadObject],
        url_for_lead: Callable[[str], int | None],
        fault_script: dict[str, list[FakeHttpResponse | Exception]] | None = None,
        index_suffix: str = ".idx",
    ) -> None:
        self._objects = objects
        self._url_for_lead = url_for_lead
        self._faults = fault_script or {}
        self._index_suffix = index_suffix
        self.get_calls: list[tuple[str, dict[str, str] | None]] = []
        self.head_calls: list[str] = []
        self.range_headers: list[str] = []

    # -- fault scripting -------------------------------------------

    def _scripted_fault(self, url: str) -> FakeHttpResponse | None:
        for fragment, queue in self._faults.items():
            if fragment in url and queue:
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
        return None

    def _object_for(self, url: str) -> LeadObject | None:
        base = url[: -len(self._index_suffix)] if url.endswith(self._index_suffix) else url
        return self._objects.get(base)

    # -- HttpTransport ---------------------------------------------

    def get(
        self, url: str, *, headers: dict[str, str] | None = None, timeout: object = None
    ) -> FakeHttpResponse:
        self.get_calls.append((url, dict(headers) if headers else None))
        fault = self._scripted_fault(url)
        if fault is not None:
            return fault

        published = self._object_for(url)
        if published is None:
            return FakeHttpResponse(status_code=404)

        if url.endswith(self._index_suffix):
            return FakeHttpResponse(
                status_code=200,
                headers={"Content-Type": "text/plain"},
                content=published.index_text().encode("utf-8"),
            )

        range_header = (headers or {}).get("Range")
        if range_header is None:
            raise AssertionError(
                f"a ranged GRIB GET to {url!r} must carry a Range header; production must never "
                "retrieve the full object"
            )
        self.range_headers.append(range_header)
        match = _RANGE_RE.fullmatch(range_header)
        if match is None:
            raise AssertionError(f"unexpected Range header format: {range_header!r}")
        start, end_inclusive = int(match.group(1)), int(match.group(2))
        body = published.body
        payload = body[start : end_inclusive + 1]
        return FakeHttpResponse(
            status_code=206,
            headers={
                "Content-Range": f"bytes {start}-{end_inclusive}/{len(body)}",
                "ETag": f'"{start}-{end_inclusive}"',
                "Last-Modified": "Sat, 31 Aug 2030 12:05:00 GMT",
            },
            content=payload,
        )

    def head(
        self, url: str, *, headers: dict[str, str] | None = None, timeout: object = None
    ) -> FakeHttpResponse:
        self.head_calls.append(url)
        fault = self._scripted_fault(url)
        if fault is not None:
            return fault
        published = self._object_for(url)
        if published is None:
            return FakeHttpResponse(status_code=404)
        return FakeHttpResponse(
            status_code=200,
            headers={
                "Content-Length": str(published.content_length),
                "ETag": f'"{published.content_length}"',
                "Last-Modified": "Sat, 31 Aug 2030 12:05:00 GMT",
            },
        )


# ----------------------------------------------------------------------
# Published-object builders (real eccodes GRIB2 bytes)
# ----------------------------------------------------------------------


def build_hrrr_lead_object(*, cycle: datetime, forecast_hour: int, base_value: float) -> LeadObject:
    shape = (hrrr_grib.NY, hrrr_grib.NX)
    args = {
        "forecast_hour": forecast_hour,
        "cycle_date": f"{cycle:%Y%m%d}",
        "cycle_hour": cycle.hour,
    }
    entries = [
        InventoryEntry(
            _HRRR_DESCRIPTORS["air_temperature_2m"] + f":{_step(forecast_hour)}",
            hrrr_grib.make_temperature_message(
                values_k=np.full(shape, 270 + base_value + forecast_hour / 10), **args
            ),
        ),
        InventoryEntry(
            _HRRR_DESCRIPTORS["dew_point_temperature_2m"] + f":{_step(forecast_hour)}",
            hrrr_grib.make_dew_point_message(
                values_k=np.full(shape, 268 + base_value + forecast_hour / 10), **args
            ),
        ),
        InventoryEntry(
            _HRRR_DESCRIPTORS["eastward_wind_10m"] + f":{_step(forecast_hour)}",
            hrrr_grib.make_wind_message(
                component="u",
                values_m_s=np.full(shape, base_value / 10),
                grid_relative=False,
                **args,
            ),
        ),
        InventoryEntry(
            _HRRR_DESCRIPTORS["northward_wind_10m"] + f":{_step(forecast_hour)}",
            hrrr_grib.make_wind_message(
                component="v",
                values_m_s=np.full(shape, base_value / 20),
                grid_relative=False,
                **args,
            ),
        ),
        InventoryEntry(
            _HRRR_DESCRIPTORS["wind_gust_10m"] + f":{_step(forecast_hour)}",
            hrrr_grib.make_gust_message(values_m_s=np.full(shape, base_value / 10 + 5), **args),
        ),
        InventoryEntry(
            f"APCP:surface:{forecast_hour - 1}-{forecast_hour} hour acc fcst",
            hrrr_grib.make_apcp_message(
                values_kg_m2=np.full(shape, base_value / 100 + forecast_hour / 1000), **args
            ),
        ),
    ]
    return LeadObject(tuple(entries), cycle.date(), cycle.hour)


def build_nbm_lead_object(*, cycle: datetime, forecast_hour: int, base_value: float) -> LeadObject:
    shape = (nbm_grib.NY, nbm_grib.NX)
    args = {
        "forecast_hour": forecast_hour,
        "cycle_date": f"{cycle:%Y%m%d}",
        "cycle_hour": cycle.hour,
    }
    entries: list[InventoryEntry] = []
    for variable, value in (
        ("air_temperature_2m", 270 + base_value + forecast_hour / 10),
        ("dew_point_temperature_2m", 268 + base_value + forecast_hour / 10),
        ("wind_speed_10m", base_value / 10),
        ("wind_from_direction_10m", 225.0),
        ("wind_gust_10m", base_value / 10 + 5),
    ):
        entries.append(
            InventoryEntry(
                _NBM_DESCRIPTORS[variable] + f":{_step(forecast_hour)}",
                nbm_grib.make_instantaneous_message(
                    canonical_variable_id=variable, values=np.full(shape, value), **args
                ),
            )
        )
    entries.append(
        InventoryEntry(
            f"APCP:surface:{forecast_hour - 1}-{forecast_hour} hour acc fcst",
            nbm_grib.make_apcp_deterministic_message(
                values_kg_m2=np.full(shape, base_value / 100 + forecast_hour / 1000), **args
            ),
        )
    )
    # PoP01's inventory row carries the exact probability suffix the
    # production selector requires, so the deterministic APCP row and the
    # probability row are distinguishable without ambiguity.
    entries.append(
        InventoryEntry(
            f"APCP:surface:{forecast_hour - 1}-{forecast_hour} hour acc fcst:"
            "prob >0.254:prob fcst 255/255:probability forecast",
            nbm_grib.make_pop01_message(
                values_percent=np.full(shape, min(99, 20 + forecast_hour)), **args
            ),
        )
    )
    return LeadObject(tuple(entries), cycle.date(), cycle.hour)


def build_gfs_lead_object(
    *, cycle: datetime, forecast_hour: int, base_value: float, duplicate_apcp: bool
) -> LeadObject:
    shape = (gfs_grib.NY, gfs_grib.NX)
    args = {
        "forecast_hour": forecast_hour,
        "cycle_date": f"{cycle:%Y%m%d}",
        "cycle_hour": cycle.hour,
    }
    entries: list[InventoryEntry] = []
    for variable, value in (
        ("air_temperature_2m", 270 + base_value + forecast_hour / 10),
        ("dew_point_temperature_2m", 268 + base_value + forecast_hour / 10),
        ("eastward_wind_10m", base_value / 10),
        ("northward_wind_10m", base_value / 20),
        ("wind_gust_10m", base_value / 10 + 5),
    ):
        entries.append(
            InventoryEntry(
                _GFS_DESCRIPTORS[variable] + f":{_step(forecast_hour)}",
                gfs_grib.make_instantaneous_message(
                    canonical_variable_id=variable,
                    values=np.full(shape, value),
                    grid_relative_wind=False,
                    **args,
                ),
            )
        )
    if forecast_hour == 0:
        # The analysis carries no accumulation record at all; production
        # only needs lead 0's instantaneous fields when it is acquired as
        # the previous-hour parent for lead 1 (which is a bucket-reset
        # hour and therefore passes its own value through).
        return LeadObject(tuple(entries), cycle.date(), cycle.hour)
    bucket_start = compute_bucket_start(forecast_hour)
    apcp = gfs_grib.make_apcp_message(
        start_step=bucket_start,
        end_step=forecast_hour,
        values_kg_m2=np.full(shape, (forecast_hour - bucket_start) * (base_value / 100)),
        cycle_date=f"{cycle:%Y%m%d}",
        cycle_hour=cycle.hour,
    )
    descriptor = f"APCP:surface:{bucket_start}-{forecast_hour} hour acc fcst"
    entries.append(InventoryEntry(descriptor, apcp))
    if duplicate_apcp:
        # Early GFS leads publish both the bucket record and a duplicate
        # continuous-total record with identical inventory text; the
        # production selector must retain both and prove equivalence.
        entries.append(InventoryEntry(descriptor, apcp))
    return LeadObject(tuple(entries), cycle.date(), cycle.hour)


def _step(forecast_hour: int) -> str:
    return "anl" if forecast_hour == 0 else f"{forecast_hour} hour fcst"


# ----------------------------------------------------------------------
# Whole-cycle transports built from a model's real URL templates
# ----------------------------------------------------------------------


@dataclass
class ScriptedCycle:
    """One model cycle's published lead objects, keyed by the exact GRIB
    URL the production source module builds for them."""

    objects: dict[str, LeadObject] = field(default_factory=dict)


def build_hrrr_transport(
    settings: HrrrPhase2SourceSettings,
    *,
    cycle: datetime,
    leads: tuple[int, ...],
    base_value: float,
    fault_script: dict[str, list[FakeHttpResponse | Exception]] | None = None,
) -> ScriptedProviderTransport:
    from mesoforge.guidance.sources import hrrr_phase2 as source

    objects: dict[str, LeadObject] = {}
    for lead in leads:
        for endpoint in settings.endpoint_order:
            url = source.build_grib_url(
                settings,
                endpoint=endpoint,
                cycle_date=cycle.date(),
                cycle_hour=cycle.hour,
                forecast_hour=lead,
            )
            objects[url] = build_hrrr_lead_object(
                cycle=cycle, forecast_hour=lead, base_value=base_value
            )
    return ScriptedProviderTransport(
        objects=objects,
        url_for_lead=lambda _url: None,
        fault_script=fault_script,
        index_suffix=settings.index_suffix,
    )


def build_nbm_transport(
    settings: NbmSourceSettings,
    *,
    cycle: datetime,
    leads: tuple[int, ...],
    base_value: float,
    fault_script: dict[str, list[FakeHttpResponse | Exception]] | None = None,
) -> ScriptedProviderTransport:
    from mesoforge.guidance.sources import nbm as source

    objects: dict[str, LeadObject] = {}
    for lead in leads:
        for endpoint in settings.endpoint_order:
            url = source.build_grib_url(
                settings,
                endpoint=endpoint,
                cycle_date=cycle.date(),
                cycle_hour=cycle.hour,
                forecast_hour=lead,
            )
            objects[url] = build_nbm_lead_object(
                cycle=cycle, forecast_hour=lead, base_value=base_value
            )
    return ScriptedProviderTransport(
        objects=objects,
        url_for_lead=lambda _url: None,
        fault_script=fault_script,
        index_suffix=settings.index_suffix,
    )


def build_gfs_transport(
    settings: GfsSourceSettings,
    *,
    cycle: datetime,
    leads: tuple[int, ...],
    base_value: float,
    duplicate_apcp_through_lead: int = 5,
    fault_script: dict[str, list[FakeHttpResponse | Exception]] | None = None,
) -> ScriptedProviderTransport:
    from mesoforge.guidance.sources import gfs as source

    objects: dict[str, LeadObject] = {}
    for lead in leads:
        for endpoint in settings.endpoint_order:
            url = source.build_grib_url(
                settings,
                endpoint=endpoint,
                cycle_date=cycle.date(),
                cycle_hour=cycle.hour,
                forecast_hour=lead,
            )
            objects[url] = build_gfs_lead_object(
                cycle=cycle,
                forecast_hour=lead,
                base_value=base_value,
                duplicate_apcp=lead <= duplicate_apcp_through_lead,
            )
    return ScriptedProviderTransport(
        objects=objects,
        url_for_lead=lambda _url: None,
        fault_script=fault_script,
        index_suffix=settings.index_suffix,
    )


class UnavailableTransport:
    """A transport whose every request is a terminal 404 -- the exact
    shape of a model whose cycle was never published. Production must
    treat it as an absent model, never as a partial cycle."""

    def __init__(self) -> None:
        self.get_calls: list[tuple[str, dict[str, str] | None]] = []
        self.head_calls: list[str] = []
        self.range_headers: list[str] = []

    def get(
        self, url: str, *, headers: dict[str, str] | None = None, timeout: object = None
    ) -> FakeHttpResponse:
        self.get_calls.append((url, dict(headers) if headers else None))
        return FakeHttpResponse(status_code=404)

    def head(
        self, url: str, *, headers: dict[str, str] | None = None, timeout: object = None
    ) -> FakeHttpResponse:
        self.head_calls.append(url)
        return FakeHttpResponse(status_code=404)


class FrozenSleeper:
    """Records every backoff without advancing the bound clock.

    The acceptance proof deliberately exercises real retry/failover
    paths (an unavailable model 404s through every attempt on every
    endpoint), which under a clock-advancing sleeper would consume
    simulated hours of wall time and push otherwise-punctual
    acquisitions past the request's information cutoff -- turning a
    source-availability scenario into a cutoff scenario and masking what
    it is meant to prove.

    Freezing the clock keeps each scenario testing one thing. The
    clock-advancing behaviour itself (bounded deterministic backoff,
    ``Retry-After`` capping, cycle-deadline expiry) is proved directly
    against ``guidance.http_fetch`` in the unit suite, and the
    information-cutoff contract has its own dedicated acceptance probes
    that move the clock explicitly.
    """

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
