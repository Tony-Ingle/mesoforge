from __future__ import annotations

from datetime import UTC, datetime

from mesoforge.application.phase2_production import (
    _group_normalization_payloads,
    _required_source_leads,
)
from mesoforge.common.identifiers import ArtifactId


def test_source_leads_add_whole_hour_cycle_age_and_cover_all_horizons() -> None:
    target = datetime(2026, 8, 30, 12, tzinfo=UTC)
    result = _required_source_leads(
        target_reference_time=target,
        candidate_reference_time=datetime(2026, 8, 30, 6, tzinfo=UTC),
        max_source_lead_hours=48,
        include_previous_apcp_dependency=False,
    )
    assert result == (tuple(range(7, 43)), tuple(range(7, 43)))


def test_candidate_rejected_when_any_target_horizon_exceeds_source_range() -> None:
    assert (
        _required_source_leads(
            target_reference_time=datetime(2026, 8, 30, 12, tzinfo=UTC),
            candidate_reference_time=datetime(2026, 8, 30, 6, tzinfo=UTC),
            max_source_lead_hours=41,
            include_previous_apcp_dependency=False,
        )
        is None
    )


def test_gfs_previous_apcp_dependency_is_acquired_but_not_a_target_lead() -> None:
    result = _required_source_leads(
        target_reference_time=datetime(2026, 8, 30, 12, tzinfo=UTC),
        candidate_reference_time=datetime(2026, 8, 30, 5, tzinfo=UTC),
        max_source_lead_hours=48,
        include_previous_apcp_dependency=True,
    )
    assert result is not None
    target_leads, acquisition_leads = result
    assert target_leads == tuple(range(8, 44))
    assert acquisition_leads == (7, *target_leads)


def test_plural_gfs_payloads_group_into_ordered_tuple() -> None:
    refs = (
        (
            "3:liquid_equivalent_precipitation_amount_1h:record:first",
            ArtifactId("art_00000000-0000-0000-0000-000000000001"),
        ),
        (
            "3:liquid_equivalent_precipitation_amount_1h:record:second",
            ArtifactId("art_00000000-0000-0000-0000-000000000002"),
        ),
    )
    grouped = _group_normalization_payloads(refs, (b"first", b"second"))
    assert grouped["liquid_equivalent_precipitation_amount_1h"][3] == (
        b"first",
        b"second",
    )


def test_nbm_policy_can_cover_three_hour_old_cycle_through_target_horizon_36() -> None:
    # Production passes 39 as the NBM maximum source lead because the
    # configured maximum cycle age is three hours.
    result = _required_source_leads(
        target_reference_time=datetime(2026, 8, 30, 12, tzinfo=UTC),
        candidate_reference_time=datetime(2026, 8, 30, 9, tzinfo=UTC),
        max_source_lead_hours=39,
        include_previous_apcp_dependency=False,
    )
    assert result == (tuple(range(4, 40)), tuple(range(4, 40)))
