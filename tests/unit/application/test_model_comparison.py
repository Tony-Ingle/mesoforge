"""Comparison readback binds saved facts and optionally recovers exact retained inputs."""

from copy import deepcopy
from datetime import datetime
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from mesoforge.application import model_comparison as application
from mesoforge.application.local_surface_grid import build_local_surface_grid, extract_grid_point
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.application.prepared_rap import RAP_CONFIGURATION
from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.common.identifiers import ArtifactId
from tests.unit.application import test_issued_temperature_verification as verification_tests
from tests.unit.application.test_prepared_temperature import prepare_fixture_guidance
from tests.unit.verification.test_model_comparison import _shadow_case

service_and_uow = verification_tests.service_and_uow
verification_case = verification_tests.verification_case


def issue_with_contributors(case):
    forecast = deepcopy(case.forecast)
    for hour in forecast["hours"]:
        for source, value in zip(hour["sources"], (280.5, 283.0), strict=True):
            source["temperature"] = {"value": value, "unit": "K"}
            source["missing_reasons"] = []
    issued = case.issuer.issue(forecast, batch_run_id=uuid4(), location_index=0)
    case.match.update(
        issued_forecast_id=str(issued.issued_forecast_id),
        forecast={"latitude": 45.8, "longitude": -93.1, **forecast["hours"][0]},
    )
    return case.service.verify(issued.issued_forecast_id, verification_tests._VALID)


def test_corrected_final_verifies_its_own_error_and_routes_legacy_comparison(verification_case):
    case = verification_case
    for hour in case.forecast["hours"]:
        hour["temperature"]["value"] -= 1.0
    case.forecast["learning_stage"] = {
        "variant_id": "corrected",
        "transformation_type": "deterministic_corrected",
        "overlay": {"predictions": [{"value": 280.25}], "correction": {"status": "applied"}},
    }
    from mesoforge.application.issuance import issued_forecast_context

    case.match["forecast_context"] = issued_forecast_context(case.forecast)
    verified = issue_with_contributors(case)
    assert verified["status"] == "verified"
    assert verified["result"]["temperature_error"]["value"] == 1.25  # final 280.25 - observed 279
    identifier = UUID(verified["result"]["match"]["issued_forecast_id"])
    before = verification_tests._inventory(case)
    with pytest.raises(ValueError, match="learning analyze"):
        application.compare_verified(
            [ArtifactId(verified["verification_id"])],
            read_verification=case.service.read,
            read_forecast=case.issuer.read,
        )
    with pytest.raises(ValueError, match="learning analyze"):
        application.compare_issued(identifier, read_forecast=case.issuer.read)
    assert verification_tests._inventory(case) == before


def test_read_compares_distinct_versions_without_writes_or_reverification(
    verification_case, monkeypatch
):
    case = verification_case
    first, second = issue_with_contributors(case), issue_with_contributors(case)
    before = verification_tests._inventory(case)
    forbidden = Mock(side_effect=AssertionError("Comparison must only read saved facts"))
    monkeypatch.setattr(case.service, "verify", forbidden)
    monkeypatch.setattr(case.objects, "put_if_absent", forbidden)
    monkeypatch.setattr(PreparedPointForecast, "forecast", forbidden)
    identifiers = [ArtifactId(row["verification_id"]) for row in (first, second, first)]
    result = application.compare_verified(
        identifiers, read_verification=case.service.read, read_forecast=case.issuer.read
    )
    assert len(result["results"]) == 2  # Repeated ID is not a third sample.
    assert len({row["issued_forecast_id"] for row in result["results"]}) == 2
    for row in result["results"]:
        assert row["contributor_evidence"]["origin"] == "issued_payload"
        assert row["errors"] == {"HRRR": 1.5, "GFS": 4.0, "blend_70_30": 2.25, "blend_50_50": 2.75}
        assert row["selected_observation"]["provenance"]["revision_digest"]
        assert row["hours_after_issuance"] == 0.5
        assert row["lead_bucket"] == "1-6"
    assert result["summary"]["all"]["paired_sample_count"] == 2
    assert verification_tests._inventory(case) == before
    forbidden.assert_not_called()


def test_grid_backed_issuance_verifies_and_compares_with_compact_context(verification_case):
    case = verification_case
    forecast = deepcopy(case.forecast)
    for hour in forecast["hours"]:
        for source, value in zip(hour["sources"], (280.5, 283.0), strict=True):
            source.update(temperature={"value": value, "unit": "K"}, missing_reasons=[])
    grid = build_local_surface_grid(
        latitude=45.8,
        longitude=-93.1,
        calculate_column=lambda **coordinates: {**forecast, **coordinates},
    )
    forecast = extract_grid_point(grid, latitude=45.8, longitude=-93.1)
    issued = case.issuer.issue(forecast, batch_run_id=uuid4(), location_index=0)
    original_bytes = case.objects.objects[issued.content_digest]
    compact_context = deepcopy(forecast)
    del compact_context["hours"], compact_context["local_grid_baseline"]
    case.match.update(
        issued_forecast_id=str(issued.issued_forecast_id),
        forecast={"latitude": 45.8, "longitude": -93.1, **forecast["hours"][0]},
        forecast_context=compact_context,
    )
    selected = case.issuer.select_hours(
        latitude=45.8,
        longitude=-93.1,
        start_valid_time=verification_tests._VALID,
        end_valid_time=verification_tests._CUTOFF,
    )
    selected_hour = next(
        row
        for row in selected["results"]
        if row["issued"]["issued_forecast_id"] == str(issued.issued_forecast_id)
    )
    assert selected_hour["forecast_context"] == compact_context
    assert selected_hour["forecast_context"]["local_grid"] == forecast["local_grid"]
    verified = case.service.verify(issued.issued_forecast_id, verification_tests._VALID)
    assert verified["status"] == "verified"
    assert verified["result"]["temperature_error"]["value"] == 2.25
    assert verified["result"]["match"]["forecast_context"] == compact_context
    before_readback = verification_tests._inventory(case)
    comparison = application.compare_verified(
        [ArtifactId(verified["verification_id"])],
        read_verification=case.service.read,
        read_forecast=case.issuer.read,
    )
    row = comparison["results"][0]
    assert row["provenance"]["forecast_context"] == compact_context
    assert row["errors"] == {
        "HRRR": 1.5,
        "GFS": 4.0,
        "blend_70_30": 2.25,
        "blend_50_50": 2.75,
    }
    assert comparison["summary"]["all"]["paired_sample_count"] == 1
    unverified = application.compare_issued(
        issued.issued_forecast_id, read_forecast=case.issuer.read
    )
    assert unverified["forecast_context"] == compact_context
    assert case.service.verify(issued.issued_forecast_id, verification_tests._VALID) == verified
    assert case.issuer.read(issued.issued_forecast_id)["forecast"] == forecast
    assert case.objects.objects[issued.content_digest] == original_bytes
    assert verification_tests._inventory(case) == before_readback


def test_old_issuance_without_guidance_keeps_control_score_and_explicit_missingness(
    verification_case,
):
    case = verification_case
    verified = case.service.verify(case.issued.issued_forecast_id, verification_tests._VALID)
    result = application.compare_verified(
        [ArtifactId(verified["verification_id"])],
        read_verification=case.service.read,
        read_forecast=case.issuer.read,
    )
    row = result["results"][0]
    assert row["errors"]["blend_70_30"] == 2.25
    assert row["predictions"]["HRRR"]["value"] is None
    assert row["contributor_evidence"]["origin"] == "unavailable"
    assert result["summary"]["all"]["excluded_count"] == 1


def test_each_issued_hour_can_be_compared_before_verification(verification_case, monkeypatch):
    case = verification_case
    first = issue_with_contributors(case)
    identifier = UUID(first["result"]["match"]["issued_forecast_id"])
    before = verification_tests._inventory(case)
    monkeypatch.setattr(
        application,
        "configured_service",
        Mock(side_effect=AssertionError("No observation selection")),
    )
    result = application.compare_issued(identifier, read_forecast=case.issuer.read)
    assert result["issued"]["issued_forecast_id"] == str(identifier)
    assert len(result["hours"]) == 36
    for row in result["hours"]:
        assert row["predictions"]["blend_50_50"]["value"] == 281.75
        assert row["predictions"]["blend_70_30"]["value"] == 281.25
        assert row["observation"]["value"] is None
        assert all(value is None for value in row["errors"].values())
    assert verification_tests._inventory(case) == before


def test_duplicate_verification_versions_of_same_issued_hour_require_explicit_choice(
    verification_case,
):
    case = verification_case
    first = issue_with_contributors(case)
    case.match["selected"]["provenance"]["revision_digest"] = "changed-observation-revision"
    second = case.service.verify(
        UUID(first["result"]["match"]["issued_forecast_id"]),
        datetime.fromisoformat(case.match["forecast"]["valid_time"]),
    )
    with pytest.raises(ValueError, match="only one verification"):
        application.compare_verified(
            [ArtifactId(row["verification_id"]) for row in (first, second)],
            read_verification=case.service.read,
            read_forecast=case.issuer.read,
        )


@pytest.mark.parametrize(
    "model,state",
    [("SYNTH_SHADOW", "late"), ("RAP", "eligible"), ("RAP", "missing"), ("RAP", "late")],
)
def test_saved_shadow_eligibility_preserves_control_and_common_comparison_samples(
    verification_case, model, state
):
    case = verification_case
    verified = issue_with_contributors(case)
    previous_id = UUID(verified["result"]["match"]["issued_forecast_id"])
    previous = case.issuer.read(previous_id)
    configuration, example = _shadow_case()
    if model == "RAP":
        configuration = RAP_CONFIGURATION
    forecast = deepcopy(previous["forecast"])
    forecast["contributor_configuration"] = configuration.model_dump(mode="json")
    for hour in forecast["hours"]:
        hour["shadow_sources"] = deepcopy(example["shadow_sources"])
        source = hour["shadow_sources"][0]
        source.update(
            model=model,
            data_kind="synthetic_demonstration",
            cycle="2026-01-01T13:00:00Z" if state == "late" else "2026-01-01T12:00:00Z",
            source_lead_hours=hour["horizon_hours"] - (1 if state == "late" else 0),
        )
        if state == "missing":
            source["temperature"]["value"] = None
            source["missing_reasons"] = ["Synthetic RAP fixture input missing"]
    issued = case.issuer.issue(forecast, batch_run_id=uuid4(), location_index=0)
    case.match.update(
        issued_forecast_id=str(issued.issued_forecast_id),
        forecast={"latitude": 45.8, "longitude": -93.1, **forecast["hours"][0]},
        forecast_context={key: value for key, value in forecast.items() if key != "hours"},
    )
    fact = case.service.verify(issued.issued_forecast_id, verification_tests._VALID)
    assert fact["status"] == "verified"
    before = verification_tests._inventory(case)
    comparison = application.compare_verified(
        [ArtifactId(fact["verification_id"])],
        read_verification=case.service.read,
        read_forecast=case.issuer.read,
    )
    row = comparison["results"][0]
    assert row["errors"]["blend_70_30"] == 2.25
    assert row["errors"]["blend_50_50"] == 2.75
    assert row["predictions"][model]["value"] == (None if state == "missing" else 300.0)
    assert row["errors"][model] == (21.0 if state == "eligible" else None)
    metrics = comparison["summary"]["all"]
    assert metrics["paired_sample_count"] == (1 if state == "eligible" else 0)
    for prediction in metrics["predictions"].values():
        assert prediction["sample_count"] == (1 if state == "eligible" else 0)
        if state != "eligible":
            assert prediction["mae"] is prediction["mean_bias"] is prediction["rmse"] is None
    if state == "eligible":
        assert metrics["predictions"]["RAP"] == {
            "sample_count": 1,
            "mae": 21.0,
            "mean_bias": 21.0,
            "rmse": 21.0,
            "unit": "K",
        }
    elif state == "late":
        assert row["ineligible_reasons"][model] == ["source_cycle_after_forecast_issuance"]
    else:
        assert "RAP_missing" in row["exclusion_reasons"]
    if model == "SYNTH_SHADOW":
        assert row["errors"]["three_model_comparison"] is None
    assert case.issuer.read(previous_id) == previous
    assert verification_tests._inventory(case) == before


@pytest.mark.parametrize("change", ["digest", "match", "error"])
def test_disagreeing_saved_verification_is_rejected(verification_case, change):
    case = verification_case
    verified = issue_with_contributors(case)
    altered = deepcopy(verified)
    if change == "digest":
        altered["result"]["issued_forecast_digest"] = "sha256:" + "0" * 64
    elif change == "match":
        altered["result"]["match"]["forecast"]["temperature"]["value"] += 1
    else:
        altered["result"]["temperature_error"]["value"] += 1
    with pytest.raises(IntegrityError):
        application.compare_verified(
            [ArtifactId(verified["verification_id"])],
            read_verification=lambda _: altered,
            read_forecast=case.issuer.read,
        )


@pytest.fixture()
def legacy(tmp_path):
    directory = tmp_path / "prepared"
    prepare_fixture_guidance(directory)
    forecast = PreparedPointForecast.from_directory(directory).forecast(
        latitude=45.8, longitude=-93.1
    )
    for hour in forecast["hours"]:
        for source in hour["sources"]:
            del source["temperature"], source["missing_reasons"]
    identity = application.comparison_identity()
    return directory, {"forecast": forecast, "code_identity": identity}, identity


def test_retained_digest_is_validated_before_filesystem_lookup(tmp_path):
    resolver = application.RetainedContributors([tmp_path / "does-not-exist"], {})
    with pytest.raises(ValueError, match="Digest"):
        resolver._find("not-a-digest")


def test_legacy_recovery_checks_exact_inputs_and_reuses_loaded_point(legacy, monkeypatch):
    directory, saved, identity = legacy
    original = deepcopy(saved)
    before = {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    # Wrap the bound operation without replacing extraction with invented values.
    original_forecast = PreparedPointForecast.forecast
    calls = []

    def tracked(self, **coordinates):
        calls.append(coordinates)
        return original_forecast(self, **coordinates)

    monkeypatch.setattr(PreparedPointForecast, "forecast", tracked)
    resolver = application.RetainedContributors([directory], identity)
    for index in (0, 1, 0):
        hour, evidence = resolver.resolve(saved, saved["forecast"]["hours"][index])
        assert evidence["origin"] == "reconstructed_from_retained_prepared_guidance"
        assert hour["sources"][0]["temperature"]["value"] == pytest.approx(280 + index)
        assert hour["sources"][1]["temperature"]["value"] == pytest.approx(290 + index)
        assert hour["temperature"]["value"] == pytest.approx(283 + index)
    assert len(calls) == 1
    assert saved == original
    assert {
        p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()
    } == before


@pytest.mark.parametrize("change", ["manifest", "science", "dependencies"])
def test_unrecoverable_legacy_contributors_are_explicit(legacy, change):
    directory, saved, identity = legacy
    if change == "manifest":
        saved["forecast"]["manifest_sha256"] = "0" * 64
    else:
        saved = deepcopy(saved)
        group = "source_sha256" if change == "science" else "dependency_versions"
        key = "alignment/station_frame.py" if change == "science" else "numpy"
        saved["code_identity"][group][key] = "different"
    hour = saved["forecast"]["hours"][0]
    resolved, evidence = application.RetainedContributors([directory], identity).resolve(
        saved, hour
    )
    assert resolved == hour and evidence["origin"] == "unavailable"
    assert evidence["reasons"]


@pytest.mark.parametrize("change", ["control", "source", "file"])
def test_recovery_rejects_changed_control_provenance_or_bytes(legacy, change):
    directory, saved, identity = legacy
    hour = saved["forecast"]["hours"][0]
    if change == "control":
        hour["temperature"]["value"] += 1
    elif change == "source":
        hour["sources"][0]["raw_sha256"] = "0" * 64
    else:
        path = directory / "HRRR.nc"
        path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises((ValueError, IntegrityError), match="(?i)checksum|disagree"):
        application.RetainedContributors([directory], identity).resolve(saved, hour)


def test_cli_empty_set_and_unknown_id_are_explicit(tmp_path, monkeypatch, capsys):
    path = tmp_path / "ids.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        application, "configured_service", Mock(side_effect=AssertionError("No storage"))
    )
    assert application.main(["--verification-ids-file", str(path)]) == 0
    assert '"paired_sample_count": 0' in capsys.readouterr().out
    service = Mock()
    service.read.side_effect = NotFound("Unknown verification")
    monkeypatch.setattr(application, "configured_service", lambda: service)
    assert application.main(["--verification-id", str(ArtifactId.generate())]) == 2
    assert "Unknown verification" in capsys.readouterr().err
