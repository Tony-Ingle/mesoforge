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
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition
from tests.support.phase2_source_settings import (
    make_gfs_settings,
    make_hrrr_phase2_settings,
    make_nbm_settings,
)

_CUTOFF = datetime(2030, 8, 31, 12, 20, tzinfo=UTC)


def _acquisition(*, index_at: datetime, grib_at: datetime, lead: int = 1) -> Phase2LeadAcquisition:
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
        index_completed_at=index_at,
        selected_messages=(),
        grib_attempts=(),
        grib_completed_at=grib_at,
        full_object_etag=None,
        full_object_last_modified=None,
        full_object_content_length=188,
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
