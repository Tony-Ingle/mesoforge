"""Native NBM PoP cycle selection and bounded acquisition, without live providers."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from mesoforge.application.pop_selection import POP, acquire_selected_pop, select_pop_guidance
from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.selected_objects import SelectedObjectError
from tests.support.phase1_fixture_transports import FixedClock, RecordingSleeper
from tests.support.phase2_source_settings import make_nbm_settings
from tests.unit.guidance.test_acquisition_v2 import _FakeResponse, _grib2_message

TARGET = datetime(2026, 9, 11, 12, tzinfo=UTC)
NOW = TARGET + timedelta(hours=2)
SETTINGS = make_nbm_settings()


class InventoryTransport:
    def __init__(self, missing=lambda cycle, lead: False):
        self.missing = missing
        self.downloaded_bytes = 0
        self.calls = []
        self.changed_etag = False

    def _objects(self, url):
        cycle = int(re.search(r"/(\d{2})/core/", url).group(1))
        lead = int(re.search(r"\.f(\d{3})\.co", url).group(1))
        # Keep a real multi-hour probability in missing-hour inventories: it
        # must not be reinterpreted as the requested hourly probability.
        start = lead - 6 if self.missing(cycle, lead) else lead - 1
        payload = b"".join(_grib2_message(value) for value in (b"t", b"q", b"p"))
        stamp = f"20260911{cycle:02}"
        inventory = (
            f"1:0:d={stamp}:TMP:2 m above ground:{lead} hour fcst:\n"
            f"2:100:d={stamp}:APCP:surface:{lead - 1}-{lead} hour acc fcst:\n"
            f"3:200:d={stamp}:APCP:surface:{start}-{lead} hour acc fcst:"
            "prob >0.254:prob fcst 255/255\n"
        ).encode()
        headers = {
            "ETag": '"changed"' if self.changed_etag else '"source-object"',
            "Last-Modified": format_datetime(NOW - timedelta(minutes=1), usegmt=True),
        }
        return inventory, payload, headers

    def get(self, url, *, headers=None, timeout=None):
        self.calls.append(("GET", url, headers))
        inventory, payload, metadata = self._objects(url)
        if url.endswith(".idx"):
            result = _FakeResponse(200, metadata, inventory)
        else:
            assert headers["Range"] == "bytes=200-299"
            assert headers["If-Match"] == '"source-object"'
            result = _FakeResponse(
                206,
                {**metadata, "Content-Length": "100", "Content-Range": "bytes 200-299/300"},
                payload[200:],
            )
        self.downloaded_bytes += len(result.content)
        return result

    def head(self, url, *, headers=None, timeout=None):
        self.calls.append(("HEAD", url, headers))
        _, _, metadata = self._objects(url)
        return _FakeResponse(200, {**metadata, "Content-Length": "300"})


def select(transport, *, explicit_cycle=None):
    clock = FixedClock(NOW)
    return select_pop_guidance(
        target_reference_time=TARGET,
        horizons=(1, 2, 3),
        settings=SETTINGS,
        transport=transport,
        clock=clock,
        sleeper=RecordingSleeper(clock),
        explicit_cycle=explicit_cycle,
    )


def test_native_hourly_discovery_and_pop_only_pinned_acquisition():
    transport = InventoryTransport()
    selection = select(transport)
    assert selection["status"] == "selected"
    assert selection["selected_cycle"] == "2026-09-11T12:00:00Z"
    assert selection["source_leads"] == [1, 2, 3]
    assert selection["missing_hours"] == []
    assert not any(url.endswith(".grib2") for method, url, _ in transport.calls if method == "GET")
    probe = selection["candidates"][0]["probes"][0]
    assert probe["selected_message"]["interval_start"] == "2026-09-11T12:00:00Z"
    assert probe["selected_message"]["interval_end"] == "2026-09-11T13:00:00Z"
    assert probe["selected_message"]["threshold_comparator"] == ">"
    clock = FixedClock(NOW)
    acquired, evidence = acquire_selected_pop(
        selection,
        settings=SETTINGS,
        transport=transport,
        clock=clock,
        sleeper=RecordingSleeper(clock),
    )
    assert [row.forecast_hour for row in acquired] == [1, 2, 3]
    assert all(list(row.payloads_by_variable()) == [POP] for row in acquired)
    assert all(row["status"] == "matched" for row in evidence["object_validation"])
    assert (
        sum(
            row.get("content_bytes", 0)
            for row in evidence["object_validation"]
            if "canonical_variable_id" in row
        )
        == 300
    )


def test_older_complete_cycle_is_aligned_by_valid_time():
    selection = select(InventoryTransport(lambda cycle, lead: cycle == 12 and lead == 2))
    assert selection["selected_cycle"] == "2026-09-11T11:00:00Z"
    assert selection["source_leads"] == [2, 3, 4]
    assert selection["candidates"][0]["status"] == "partial"
    assert [p["valid_time"] for p in selection["candidates"][1]["probes"]] == [
        "2026-09-11T13:00:00Z",
        "2026-09-11T14:00:00Z",
        "2026-09-11T15:00:00Z",
    ]


def test_newest_partial_cycle_keeps_native_values_and_explicit_hourly_gaps():
    transport = InventoryTransport(lambda cycle, lead: lead + cycle == 14)
    selection = select(transport)
    assert selection["status"] == "partial"
    assert selection["selected_cycle"] == "2026-09-11T12:00:00Z"
    assert selection["source_leads"] == [1, 3]
    assert selection["missing_hours"][0]["horizon_hours"] == 2
    assert "No native one-hour PoP" in selection["missing_hours"][0]["reason"]
    clock = FixedClock(NOW)
    acquired, _ = acquire_selected_pop(
        selection,
        settings=SETTINGS,
        transport=transport,
        clock=clock,
        sleeper=RecordingSleeper(clock),
    )
    assert [row.forecast_hour for row in acquired] == [1, 3]


def test_no_native_hourly_probabilities_is_explicitly_unavailable():
    selection = select(InventoryTransport(lambda cycle, lead: True))
    assert selection["status"] == "unavailable" and selection["selected_cycle"] is None
    assert len(selection["candidates"]) == 4


def test_explicit_cycle_disables_automatic_cycle_substitution():
    selection = select(InventoryTransport(), explicit_cycle=TARGET - timedelta(hours=2))
    assert selection["selected_cycle"] == "2026-09-11T10:00:00Z"
    assert selection["source_leads"] == [3, 4, 5]
    assert len(selection["candidates"]) == 1


def test_source_leads_past_36_are_accepted_when_native_hourly_guidance_exists():
    clock = FixedClock(NOW)
    selection = select_pop_guidance(
        target_reference_time=TARGET,
        horizons=tuple(range(1, 37)),
        settings=SETTINGS,
        transport=InventoryTransport(),
        clock=clock,
        sleeper=RecordingSleeper(clock),
        explicit_cycle=TARGET - timedelta(hours=1),
    )
    assert selection["source_leads"] == list(range(2, 38))
    assert selection["missing_hours"] == []
    assert selection["candidates"][0]["probes"][-1]["selected_message"]["interval_start"] == (
        "2026-09-12T23:00:00Z"
    )


@pytest.mark.parametrize(
    "mutation", ["etag", "lead", "cycle", "settings", "partial_as_complete", "interval", "event"]
)
def test_changed_selection_or_provider_object_never_reaches_unpinned_acquisition(mutation):
    transport = InventoryTransport(lambda cycle, lead: lead + cycle == 14)
    selection = deepcopy(select(transport))
    if mutation == "etag":
        transport.changed_etag = True
    elif mutation == "lead":
        selection["source_leads"] = [1]
    elif mutation == "cycle":
        selection["candidates"][0]["probes"][0]["cycle"] = "2026-09-11T11:00:00Z"
    elif mutation == "settings":
        selection["settings_sha256"] = "0" * 64
    elif mutation == "interval":
        selection["candidates"][0]["probes"][0]["selected_message"]["interval_start"] = (
            "2026-09-11T06:00:00Z"
        )
    elif mutation == "event":
        selection["candidates"][0]["probes"][0]["selected_message"]["threshold_comparator"] = ">="
    else:
        selection["status"] = "selected"
    clock = FixedClock(NOW)
    with pytest.raises(
        (ValueError, SelectedObjectError, FetchError), match="identity|differs|changed|settings|PoP"
    ):
        acquire_selected_pop(
            selection,
            settings=SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=RecordingSleeper(clock),
        )
    assert not any(url.endswith(".grib2") for method, url, _ in transport.calls if method == "GET")
