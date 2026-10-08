"""Nominal schedules cannot manufacture missing source samples or hourly events."""

from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.catalog.native_horizons import native_field_contract

T = "air_temperature_2m"
QPF = "liquid_equivalent_precipitation_amount_1h"
CYCLE = datetime(2026, 10, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    ("source", "hour", "last"),
    [
        ("HRRR", 0, 48),
        ("HRRR", 1, 18),
        ("RAP", 3, 51),
        ("RAP", 0, 21),
        ("GFS", 0, 384),
        ("IFS", 0, 360),
        ("IFS", 6, 144),
        ("NBM", 1, 263),
    ],
)
def test_product_specific_cycles_expire_at_native_horizon(
    source: str, hour: int, last: int
) -> None:
    cycle = CYCLE.replace(hour=hour)
    contract = native_field_contract(source, T)
    assert contract.native_leads(cycle)[-1] == last
    endpoint = contract.plan(cycle, cycle + timedelta(hours=last))
    assert endpoint.status == "native"
    assert endpoint.source_leads == (last,)
    expired = contract.plan(cycle, cycle + timedelta(hours=last + 1))
    assert expired.status == "outside_native_horizon"
    assert expired.source_leads == ()


def test_source_age_is_not_forecast_lead_and_cannot_extend_short_range_model() -> None:
    reference = CYCLE + timedelta(hours=5)
    contract = native_field_contract("HRRR", T)
    assert contract.plan(CYCLE, reference + timedelta(hours=43)).source_leads == (48,)
    assert contract.plan(CYCLE, reference + timedelta(hours=44)).status == "outside_native_horizon"


@pytest.mark.parametrize(
    ("source", "target", "endpoints", "step"),
    [
        ("IFS", 1, (0, 3), 3),
        ("IFS", 145, (144, 150), 6),
        ("GFS", 121, (120, 123), 3),
        ("NBM", 49, (48, 51), 3),
        ("NBM", 193, (192, 198), 6),
    ],
)
def test_state_plans_request_adjacent_native_endpoints_only(
    source: str, target: int, endpoints: tuple[int, ...], step: int
) -> None:
    plan = native_field_contract(source, T).plan(CYCLE, CYCLE + timedelta(hours=target))
    assert plan.status == "bracketed"
    assert plan.source_leads == endpoints
    assert plan.native_step_hours == step
    assert plan.expected_interval is None
    # The plan still asks for the missing endpoint; it does not select a wider
    # bracket or claim readiness from a list of nominally possible source leads.
    retained_leads = {endpoints[0], endpoints[-1] + step}
    assert not set(plan.source_leads).issubset(retained_leads)


def test_nbm_native_vector_conversion_precedes_component_interpolation() -> None:
    component = native_field_contract("NBM", "eastward_wind_10m")
    assert component.normalization == "earth_relative_components_from_native_speed_direction"
    with pytest.raises(ValueError, match="Unregistered"):
        native_field_contract("NBM", "wind_from_direction_10m")


def test_gust_semantics_are_not_generic_state_interpolation() -> None:
    instantaneous = native_field_contract("GFS", "wind_gust_10m")
    assert instantaneous.plan(CYCLE, CYCLE + timedelta(hours=120)).status == "native"
    assert instantaneous.plan(CYCLE, CYCLE + timedelta(hours=121)).status == "no_native_event"
    with pytest.raises(ValueError, match="Unregistered"):
        native_field_contract("IFS", "wind_gust_10m")
    interval = native_field_contract("IFS", "wind_gust_10m_interval_maximum")
    assert interval.temporal_kind == "interval_maximum"
    assert interval.normalization == "maximum_since_previous_postprocessing"
    assert interval.plan(CYCLE, CYCLE + timedelta(hours=2)).status == "outside_native_horizon"
    assert interval.plan(CYCLE, CYCLE + timedelta(hours=4)).status == "no_native_event"
    assert interval.plan(CYCLE, CYCLE + timedelta(hours=3)).expected_interval is None


def test_nbm_hourly_amount_files_are_distinct_from_sparse_state_files() -> None:
    nbm = native_field_contract("NBM", QPF)
    plan = nbm.plan(CYCLE, CYCLE + timedelta(hours=51))
    assert plan.status == "native"
    assert plan.expected_interval == (CYCLE + timedelta(hours=50), CYCLE + timedelta(hours=51))
    for lead in (49, 50):
        hourly = nbm.plan(CYCLE, CYCLE + timedelta(hours=lead))
        assert hourly.status == "native"
        assert hourly.source_leads == (lead,)
        assert hourly.expected_interval == (
            CYCLE + timedelta(hours=lead - 1),
            CYCLE + timedelta(hours=lead),
        )
    assert nbm.plan(CYCLE, CYCLE + timedelta(hours=264)).expected_interval == (
        CYCLE + timedelta(hours=263),
        CYCLE + timedelta(hours=264),
    )


def test_gfs_and_ifs_coarse_amounts_do_not_become_hourly_qpf() -> None:
    gfs = native_field_contract("GFS", QPF)
    assert gfs.plan(CYCLE, CYCLE + timedelta(hours=120)).expected_interval == (
        CYCLE + timedelta(hours=119),
        CYCLE + timedelta(hours=120),
    )
    assert gfs.plan(CYCLE, CYCLE + timedelta(hours=123)).status == "outside_native_horizon"
    coarse = native_field_contract("GFS", "liquid_equivalent_precipitation_amount")
    assert coarse.plan(CYCLE, CYCLE + timedelta(hours=123)).status == "native"
    assert coarse.plan(CYCLE, CYCLE + timedelta(hours=123)).expected_interval is None
    assert coarse.normalization == "native_encoded_accumulation_interval_required"
    with pytest.raises(ValueError, match="Unregistered"):
        native_field_contract("IFS", QPF)
    ifs = native_field_contract("IFS", "liquid_equivalent_precipitation_amount")
    assert ifs.normalization == "cycle_accumulation"
    assert ifs.plan(CYCLE, CYCLE + timedelta(hours=3)).status == "native"
    assert ifs.plan(CYCLE, CYCLE + timedelta(hours=3)).expected_interval is None
    assert ifs.plan(CYCLE, CYCLE + timedelta(hours=4)).status == "no_native_event"


def test_nbm_probability_ranges_are_field_specific_not_file_horizon() -> None:
    for field, last in (("probability_of_precipitation_1h", 48), ("probability_of_thunder_1h", 36)):
        contract = native_field_contract("NBM", field)
        plan = contract.plan(CYCLE, CYCLE + timedelta(hours=last))
        assert contract.temporal_kind == "probability"
        assert plan.expected_interval == (
            CYCLE + timedelta(hours=last - 1),
            CYCLE + timedelta(hours=last),
        )
        assert (
            contract.plan(CYCLE, CYCLE + timedelta(hours=last + 1)).status
            == "outside_native_horizon"
        )
    for field in ("visibility", "probability_of_thunder_6h"):
        with pytest.raises(ValueError, match="Unregistered"):
            native_field_contract("NBM", field)


def test_nbm_native_states_and_probability_follow_valid_utc_not_cycle_lead_multiples():
    cycle = CYCLE.replace(hour=13)
    state = native_field_contract("NBM", T)
    assert state.plan(cycle, cycle + timedelta(hours=125)).status == "native"
    assert state.plan(cycle, cycle + timedelta(hours=126)).source_leads == (125, 128)
    assert state.plan(cycle, cycle + timedelta(hours=49)).source_leads == (48, 50)
    assert state.plan(cycle, cycle + timedelta(hours=49)).native_step_hours == 2
    probability = native_field_contract("NBM", "probability_of_precipitation_6h")
    assert probability.plan(cycle, cycle + timedelta(hours=125)).status == "native"
    assert probability.plan(cycle, cycle + timedelta(hours=128)).status == "no_native_event"
    assert probability.plan(cycle, cycle + timedelta(hours=125)).expected_interval == (
        cycle + timedelta(hours=119),
        cycle + timedelta(hours=125),
    )
    gefs = native_field_contract("GEFS", "probability_of_precipitation_6h")
    assert gefs.native_leads(CYCLE)[-1] == 384
    assert gefs.plan(CYCLE, CYCLE + timedelta(hours=7)).status == "no_native_event"
    with pytest.raises(ValueError, match="Unregistered"):
        native_field_contract("GEFS", T)


@pytest.mark.parametrize(
    "cycle", [datetime(2026, 10, 8), CYCLE.replace(minute=1), CYCLE.replace(hour=1)]
)
def test_reject_invalid_cycle_contract(cycle: datetime) -> None:
    with pytest.raises(ValueError):
        native_field_contract("GFS", T).native_leads(cycle)


def test_before_cycle_and_before_first_nbm_lead_do_not_extrapolate() -> None:
    assert (
        native_field_contract("IFS", T).plan(CYCLE, CYCLE - timedelta(hours=1)).status
        == "outside_native_horizon"
    )
    assert native_field_contract("NBM", T).plan(CYCLE, CYCLE).status == "outside_native_horizon"
