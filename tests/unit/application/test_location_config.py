"""Canonical location delivery metadata; no delivery or forecast execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mesoforge.application.batch_forecast import _coordinates
from mesoforge.application.location_config import load_locations, location_email_recipients


def test_recipient_lists_are_optional_ordered_and_deduplicated() -> None:
    assert location_email_recipients({}) == []
    assert location_email_recipients({"email_recipients": []}) == []
    row = {
        "lat": 44.98861,
        "lon": -93.25553,
        "email_recipients": ["First@EXAMPLE.test", "second@example.test", "First@example.test"],
    }
    original = json.dumps(row)
    assert location_email_recipients(row) == ["First@example.test", "second@example.test"]
    assert _coordinates(row) == (44.98861, -93.25553)
    assert json.dumps(row) == original  # Configuration is input, never rewritten in place.
    assert location_email_recipients({"email_recipients": ["operator@LOCALHOST"]}) == [
        "operator@localhost"
    ]


@pytest.mark.parametrize(
    "recipients",
    [
        None,
        "one@example.test",
        [7],
        [""],
        ["missing-at"],
        ["a@b\r\nBcc: other@example.test"],
        ["a@.."],
        ["a@example..test"],
        ["a@-example.test"],
        ["a@example-.test"],
        ["a@example.test."],
        [".a@example.test"],
        ["a.@example.test"],
        ["a..b@example.test"],
        ["a" * 65 + "@example.test"],
        ["a@" + "b" * 64 + ".test"],
    ],
)
def test_invalid_recipient_metadata_is_explicit(recipients: object) -> None:
    row = {"lat": 44.98861, "lon": -93.25553, "email_recipients": recipients}
    with pytest.raises(ValueError):
        location_email_recipients(row)
    with pytest.raises(ValueError):
        _coordinates(row)


def test_historical_location_config_has_no_delivery_fallback(tmp_path: Path) -> None:
    path = tmp_path / "locations.json"
    path.write_text(json.dumps({"locations": [{"lat": 45.8, "lon": -93.1}]}), "utf-8")
    row = load_locations(path)[0]
    assert _coordinates(row) == (45.8, -93.1)
    assert location_email_recipients(row) == []


def test_registry_preserves_per_location_failure_boundaries(tmp_path: Path) -> None:
    rows = [
        {"lat": 44.98861, "lon": -93.25553, "email_recipients": ["invalid"]},
        {"lat": 45.80268, "lon": -93.07952, "email_recipients": []},
    ]
    path = tmp_path / "locations.json"
    path.write_text(json.dumps({"locations": rows}), "utf-8")
    loaded = load_locations(path)
    with pytest.raises(ValueError):
        _coordinates(loaded[0])
    assert _coordinates(loaded[1]) == (45.80268, -93.07952)


def test_maintained_registry_recipient_selection_is_explicit() -> None:
    path = Path(__file__).resolve().parents[3] / "configs/locations.json"
    rows = {row["id"]: row for row in load_locations(path)}
    assert set(rows) == {"minneapolis", "grasston"}
    assert location_email_recipients(rows["minneapolis"]) == ["ingle.j.anthony@gmail.com"]
    assert location_email_recipients(rows["grasston"]) == []
