"""Unit probes for the Phase 2 production provider's fail-closed
selection contracts (Codex re-review findings 4 and 5).

These exercise the pure helpers directly, independently of the
integration-backed acceptance proof, so a regression in the cutoff or
replay-identity logic is caught by the offline suite too.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from mesoforge.application.phase2_production import (
    _late_acquisitions,
    _source_grid_profile_id,
    _wind_rotation_policy,
)
from mesoforge.guidance.acquisition_v2 import (
    Phase2LeadAcquisition,
    parse_provider_availability,
    resolve_available_at,
)
from tests.support.phase2_source_settings import (
    make_gfs_settings,
    make_hrrr_phase2_settings,
    make_nbm_settings,
)

_CUTOFF = datetime(2030, 8, 31, 12, 20, tzinfo=UTC)


def _acquisition(
    *,
    index_at: datetime,
    grib_at: datetime,
    lead: int = 1,
    retrieved_at: datetime | None = None,
) -> Phase2LeadAcquisition:
    """Build one acquisition whose *provider availability* is
    ``index_at``/``grib_at``.

    ``retrieved_at`` is the unrelated local wall-clock moment the bytes
    were fetched; it defaults to long after the availability instants so
    that any test which still passes proves the policy reads
    availability rather than retrieval time.
    """
    retrieved = (
        retrieved_at if retrieved_at is not None else max(index_at, grib_at) + timedelta(hours=6)
    )
    return Phase2LeadAcquisition(
        model="hrrr",
        cycle_date=date(2030, 8, 31),
        cycle_hour=12,
        forecast_hour=lead,
        endpoint="aws",
        resolved_grib_url="https://example/hrrr.grib2",
        resolved_index_url="https://example/hrrr.grib2.idx",
        index_payload=b"1:0:d=2030083112:TMP:2 m above ground:1 hour fcst:\n",
        index_attempts=(),
        index_completed_at=retrieved,
        selected_messages=(),
        grib_attempts=(),
        grib_completed_at=retrieved,
        full_object_etag=None,
        full_object_last_modified=None,
        full_object_content_length=188,
        index_available_at=index_at,
        grib_available_at=grib_at,
    )


class TestInformationCutoffDetection:
    """Finding 4: no selected input may have an ``available_at`` after
    the request's information cutoff."""

    def test_accepts_acquisitions_completed_exactly_at_the_cutoff(self) -> None:
        acquisitions = (_acquisition(index_at=_CUTOFF, grib_at=_CUTOFF),)
        assert _late_acquisitions(acquisitions, _CUTOFF) == ()

    def test_detects_a_late_message_acquisition(self) -> None:
        late = _CUTOFF + timedelta(seconds=1)
        acquisitions = (_acquisition(index_at=_CUTOFF, grib_at=late),)
        assert _late_acquisitions(acquisitions, _CUTOFF) == (late,)

    def test_detects_a_late_index_acquisition(self) -> None:
        """An index that only appeared after the cutoff is just as much a
        leak of future information as a late message."""
        late = _CUTOFF + timedelta(minutes=5)
        acquisitions = (_acquisition(index_at=late, grib_at=_CUTOFF),)
        assert _late_acquisitions(acquisitions, _CUTOFF) == (late,)

    def test_reports_every_late_lead_not_only_the_first(self) -> None:
        first = _CUTOFF + timedelta(minutes=1)
        second = _CUTOFF + timedelta(minutes=2)
        acquisitions = (
            _acquisition(index_at=_CUTOFF, grib_at=_CUTOFF, lead=1),
            _acquisition(index_at=first, grib_at=second, lead=2),
        )
        assert set(_late_acquisitions(acquisitions, _CUTOFF)) == {first, second}

    def test_an_empty_candidate_is_vacuously_on_time(self) -> None:
        assert _late_acquisitions((), _CUTOFF) == ()

    def test_a_late_retrieval_of_a_punctually_published_object_is_not_late(self) -> None:
        """The defect this closes: an already-published retrospective
        cycle must not be rejected merely because this process fetched
        it after the cutoff.

        Publication is hours before the cutoff; retrieval is hours
        after. Only publication may gate the cutoff, so nothing is late.
        """
        published = _CUTOFF - timedelta(hours=4)
        retrieved = _CUTOFF + timedelta(hours=3)
        acquisitions = (
            _acquisition(index_at=published, grib_at=published, retrieved_at=retrieved),
        )
        assert acquisitions[0].index_completed_at > _CUTOFF
        assert acquisitions[0].grib_completed_at > _CUTOFF
        assert _late_acquisitions(acquisitions, _CUTOFF) == ()

    def test_a_genuinely_late_publication_is_still_rejected(self) -> None:
        """The converse must keep holding: an object the provider only
        published after the cutoff is a real leak of future information
        even when it was retrieved promptly afterwards.
        """
        published = _CUTOFF + timedelta(minutes=10)
        acquisitions = (
            _acquisition(
                index_at=_CUTOFF,
                grib_at=published,
                retrieved_at=_CUTOFF + timedelta(minutes=11),
            ),
        )
        assert _late_acquisitions(acquisitions, _CUTOFF) == (published,)


class TestProviderAvailabilityParsing:
    """The authoritative availability instant comes from the provider's
    own ``Last-Modified`` assertion, never from the local clock."""

    def test_parses_a_real_provider_http_date_as_utc(self) -> None:
        parsed = parse_provider_availability("Wed, 02 Sep 2026 13:40:26 GMT")
        assert parsed == datetime(2026, 9, 2, 13, 40, 26, tzinfo=UTC)

    @pytest.mark.parametrize("value", [None, "", "not-a-date", "Wed, 99 Xxx 2026 13:40:26 GMT"])
    def test_returns_none_for_a_missing_or_malformed_header(self, value: str | None) -> None:
        assert parse_provider_availability(value) is None

    def test_resolve_prefers_the_provider_assertion_over_retrieval_time(self) -> None:
        retrieved = datetime(2026, 9, 2, 18, 33, tzinfo=UTC)
        resolved = resolve_available_at("Wed, 02 Sep 2026 13:40:26 GMT", retrieved_at=retrieved)
        assert resolved == datetime(2026, 9, 2, 13, 40, 26, tzinfo=UTC)

    def test_resolve_falls_back_to_retrieval_time_when_the_provider_is_silent(self) -> None:
        """A provider that asserts nothing gets the conservative
        fallback: retrieval time is necessarily no earlier than
        publication, so the fallback can only ever make an object look
        later, never admit a genuinely late one."""
        retrieved = datetime(2026, 9, 2, 18, 33, tzinfo=UTC)
        assert resolve_available_at(None, retrieved_at=retrieved) == retrieved


class TestLineageProvenanceHelpers:
    """Finding 3: the lineage manifest must record the policy actually
    applied, per model and per variable."""

    @pytest.mark.parametrize(
        "model,variable,expected",
        [
            ("HRRR", "eastward_wind_10m", "grid-to-earth-pyproj.v1"),
            ("HRRR", "northward_wind_10m", "grid-to-earth-pyproj.v1"),
            ("GFS", "eastward_wind_10m", "grid-to-earth-pyproj.v1"),
            ("NBM", "wind_speed_10m", "speed-direction-to-uv.v1"),
            ("NBM", "wind_from_direction_10m", "speed-direction-to-uv.v1"),
            ("HRRR", "air_temperature_2m", None),
            ("NBM", "probability_of_precipitation_1h", None),
            ("GFS", "liquid_equivalent_precipitation_amount_1h", None),
        ],
    )
    def test_wind_rotation_policy_matches_the_model_pipeline(
        self, model: str, variable: str, expected: str | None
    ) -> None:
        assert _wind_rotation_policy(model, variable) == expected

    def test_nbm_source_grid_profile_is_the_approved_profile_id(self) -> None:
        settings = make_nbm_settings()
        assert _source_grid_profile_id(settings, "NBM") == settings.grid_profile.profile_id

    def test_gfs_source_grid_profile_is_its_product_profile(self) -> None:
        assert _source_grid_profile_id(make_gfs_settings(), "GFS") == "gfs-pgrb2-0p25.v1"

    def test_hrrr_source_grid_profile_derives_from_product_and_sector(self) -> None:
        assert _source_grid_profile_id(make_hrrr_phase2_settings(), "HRRR") == ("hrrr-sfc-conus.v1")
