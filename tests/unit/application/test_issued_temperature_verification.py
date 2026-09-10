"""Single-hour verification through the real artifact service and in-memory storage."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from mesoforge.application import issued_temperature_verification as application
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.contracts.artifacts import Availability
from mesoforge.storage.json import CanonicalJsonSerializer
from tests.unit.application import test_artifact_service as artifact_tests

service_and_uow = artifact_tests.service_and_uow
_JSON = CanonicalJsonSerializer()
_TARGET = datetime(2026, 1, 1, 12, tzinfo=UTC)
_VALID = _TARGET + timedelta(hours=1)
_CUTOFF = _TARGET + timedelta(hours=2)
_EVALUATED = _TARGET + timedelta(days=1)


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def _inventory(case):
    return copy.deepcopy(
        (
            case.uow.artifacts,
            case.uow.activities,
            case.uow.stored_objects,
            case.uow.issued_forecasts,
            case.objects.objects,
        )
    )


@pytest.fixture()
def verification_case(service_and_uow):
    artifacts, uow, objects = service_and_uow

    def register(kind, payload, *, revision="v1"):
        return artifacts.register_source(
            artifact_tests._source_request(
                source_locator=f"synthetic://temperature-verification/{kind}",
                source_revision=revision,
                artifact_type=kind,
                artifact_schema_version=(
                    "metar-observations.v2"
                    if kind == "normalized-metar-observations"
                    else "station-catalog-snapshot.v1"
                ),
                media_type="application/json",
                availability=Availability(
                    available_at=_CUTOFF,
                    ingested_at=_CUTOFF,
                    authority="mesoforge.synthetic",
                    method="synthetic-fixture.v1",
                ),
                attributes={"data_kind": "synthetic_observation_fixture"},
            ),
            _JSON.serialize(payload),
        )

    station = register(
        "station-catalog-snapshot",
        {"stations": [{"station_id": "station.test", "provider_icao_id": "KTST"}]},
    )
    selected = {
        "station_id": "KTST",
        "catalog_station_id": "station.test",
        "network": "METAR",
        "provider": "aviationweather.gov",
        "latitude": 45.81,
        "longitude": -93.1,
        "elevation_m": 290.0,
        "distance_km": 1.1,
        "observation_time": _iso(_VALID),
        "temperature": {"value": 279.0, "unit": "K"},
        "qc": {"temperature_eligible": True, "state": "eligible", "flags": []},
        "provenance": {
            "revision_digest": str(Digest.of_bytes(b"synthetic revision one")),
            "logical_observation_digest": str(Digest.of_bytes(b"synthetic logical record")),
            "raw_record_digest": str(Digest.of_bytes(b"synthetic raw record")),
            "station_snapshot_artifact_id": str(station.artifact_id),
            "provider_available_at": _iso(_VALID + timedelta(minutes=1)),
            "ingested_at": _iso(_VALID + timedelta(minutes=2)),
        },
        "status": "selected",
        "reasons": [],
    }
    observations = register("normalized-metar-observations", {"rows": [selected]})
    forecast = {
        "data_kind": "synthetic_demonstration",
        "latitude": 45.8,
        "longitude": -93.1,
        "target_reference_time": _iso(_TARGET),
        "hours": [
            {
                "horizon_hours": horizon,
                "valid_time": _iso(_TARGET + timedelta(hours=horizon)),
                "temperature": {"value": 281.25, "unit": "K"},
                "missing_reasons": [],
                "sources": [
                    {"model": "HRRR", "cycle": _iso(_TARGET), "weight": 0.7},
                    {
                        "model": "GFS",
                        "cycle": _iso(_TARGET - timedelta(hours=6)),
                        "weight": 0.3,
                    },
                ],
            }
            for horizon in range(1, 37)
        ],
    }
    issuer = ForecastIssuanceService(
        objects,
        uow,
        code_identity={"fixture": "synthetic issuance"},
        clock=lambda: _TARGET + timedelta(minutes=30),
    )
    issued = issuer.issue(forecast, batch_run_id=uuid4(), location_index=0)
    saved = issuer.read(issued.issued_forecast_id)
    match = {
        "issued_forecast_id": saved["issued_forecast_id"],
        "issued_at": saved["issued_at"],
        "forecast": {"latitude": 45.8, "longitude": -93.1, **forecast["hours"][0]},
        "forecast_context": {key: value for key, value in forecast.items() if key != "hours"},
        "forecast_code_identity": saved["code_identity"],
        "selection_policy": {
            "max_distance_km": 50,
            "max_time_difference_minutes": 15,
            "boundaries": "inclusive",
            "ranking": ["distance", "absolute_time_difference", "station_id"],
            "revisions": "latest retained revision per logical observation, before QC",
        },
        "input_provenance": {
            "observations": observations.model_dump(mode="json"),
            "station_snapshots": [station.model_dump(mode="json")],
            "observation_qc_policy": {"policy_id": "metar-normalization.v1"},
        },
        "status": "matched",
        "unavailable_reason": None,
        "selected": selected,
        "candidates": [selected],
    }
    read = Mock(side_effect=issuer.read)
    preview = Mock(side_effect=lambda _identifier, _time: copy.deepcopy(match))
    identity = {"source_sha256": {"verification": "a" * 64}, "fixture": "synthetic"}
    service = application.IssuedTemperatureVerificationService(
        artifacts,
        read_forecast=read,
        preview_match=preview,
        code_identity=identity,
        code_revision="a" * 40,
        environment_digest=Digest.of_bytes(b"locked test environment"),
        clock=lambda: _EVALUATED,
    )
    return SimpleNamespace(
        service=service,
        artifacts=artifacts,
        uow=uow,
        objects=objects,
        issuer=issuer,
        issued=issued,
        saved=saved,
        forecast=forecast,
        match=match,
        read=read,
        preview=preview,
        identity=identity,
        register=register,
    )


def test_verify_round_trip_and_retry_preserve_exact_forecast_and_observation(verification_case):
    case = verification_case
    original_bytes = case.objects.objects[case.issued.content_digest]
    result = case.service.verify(case.issued.issued_forecast_id, _VALID)
    assert result["status"] == "verified"
    fact = result["result"]
    assert fact["temperature_error"] == {
        "value": 2.25,
        "unit": "K",
        "definition": "forecast_minus_observation",
    }
    assert fact["match"] == case.match
    assert fact["match"]["issued_forecast_id"] == str(case.issued.issued_forecast_id)
    assert fact["match"]["selected"]["provenance"]["revision_digest"] == str(
        Digest.of_bytes(b"synthetic revision one")
    )
    assert fact["issued_forecast_digest"] == str(Digest.of_bytes(original_bytes))
    assert fact["code_identity"] == case.identity
    assert fact["verification_cutoff"] == _iso(_CUTOFF)
    assert len(case.uow.activities) == 1
    activity = next(iter(case.uow.activities.values()))
    assert activity.status == "succeeded"
    assert [str(item.artifact_id) for item in activity.inputs] == [
        case.match["input_provenance"]["observations"]["artifact_id"],
        case.match["input_provenance"]["station_snapshots"][0]["artifact_id"],
    ]
    after = _inventory(case)
    assert case.service.verify(case.issued.issued_forecast_id, _VALID) == result
    case.read.side_effect = AssertionError("Readback recalculated the forecast")
    case.preview.side_effect = AssertionError("Readback selected another observation")
    assert case.service.read(ArtifactId(result["verification_id"])) == result
    assert _inventory(case) == after
    assert case.objects.objects[case.issued.content_digest] == original_bytes
    assert case.uow.issued_forecasts == {case.issued.issued_forecast_id: case.issued}


def test_two_issued_versions_of_same_hour_have_independent_verification_facts(verification_case):
    case = verification_case
    first = case.service.verify(case.issued.issued_forecast_id, _VALID)
    second_issue = case.issuer.issue(case.forecast, batch_run_id=uuid4(), location_index=0)
    case.match["issued_forecast_id"] = str(second_issue.issued_forecast_id)
    second = case.service.verify(second_issue.issued_forecast_id, _VALID)
    assert first["verification_id"] != second["verification_id"]
    assert first["result"]["temperature_error"] == second["result"]["temperature_error"]
    assert second["result"]["match"]["issued_forecast_id"] == str(second_issue.issued_forecast_id)
    assert case.service.read(ArtifactId(first["verification_id"])) == first
    assert len(case.uow.activities) == len(case.uow.issued_forecasts) == 2


@pytest.mark.parametrize("change", ["observation_revision", "matching_policy"])
def test_changed_input_or_policy_preserves_the_previous_result(verification_case, change):
    case = verification_case
    first = case.service.verify(case.issued.issued_forecast_id, _VALID)
    if change == "observation_revision":
        case.match["selected"]["provenance"]["revision_digest"] = str(
            Digest.of_bytes(b"synthetic corrected revision")
        )
        case.match["selected"]["temperature"]["value"] = 280.0
        manifest = case.register(
            "normalized-metar-observations", {"rows": [case.match["selected"]]}, revision="v2"
        )
        case.match["input_provenance"]["observations"] = manifest.model_dump(mode="json")
    else:
        case.match["selection_policy"]["policy_version"] = "explicit-test-version-two"
    second = case.service.verify(case.issued.issued_forecast_id, _VALID)
    assert first["verification_id"] != second["verification_id"]
    assert second["result"]["temperature_error"]["value"] == (
        1.25 if change == "observation_revision" else 2.25
    )
    assert case.service.read(ArtifactId(first["verification_id"])) == first
    assert case.service.verify(case.issued.issued_forecast_id, _VALID) == second
    assert len(case.uow.activities) == 2


@pytest.mark.parametrize("condition", ["unavailable", "failed_qc", "late_revision"])
def test_ineligible_match_returns_reason_and_creates_nothing(verification_case, condition):
    case = verification_case
    if condition == "unavailable":
        case.match.update(
            status="unavailable", selected=None, unavailable_reason="No retained report"
        )
    elif condition == "failed_qc":
        case.match["selected"]["qc"]["temperature_eligible"] = False
    else:
        case.match["selected"]["provenance"]["ingested_at"] = _iso(_CUTOFF + timedelta(seconds=1))
    before = _inventory(case)
    response = case.service.verify(case.issued.issued_forecast_id, _VALID)
    assert response["status"] == ("unavailable" if condition == "unavailable" else "ineligible")
    assert response["verification_id"] is None
    assert response["result"]["reasons"]
    assert response["result"]["temperature_error"]["value"] is None
    assert _inventory(case) == before


@pytest.mark.parametrize(
    "field",
    ["issued_forecast_id", "issued_at", "forecast", "forecast_context", "forecast_code_identity"],
)
def test_mismatched_preview_cannot_verify_another_saved_version(verification_case, field):
    case = verification_case
    case.match[field] = "substituted" if field.startswith("issued") else {}
    before = _inventory(case)
    with pytest.raises(IntegrityError, match="exact saved forecast"):
        case.service.verify(case.issued.issued_forecast_id, _VALID)
    assert _inventory(case) == before


def test_unknown_forecast_and_naive_time_fail_without_writes(verification_case):
    case = verification_case
    before = _inventory(case)
    with pytest.raises(NotFound):
        case.service.verify(uuid4(), _VALID)
    with pytest.raises(ValueError, match="timezone"):
        case.service.verify(case.issued.issued_forecast_id, _VALID.replace(tzinfo=None))
    case.preview.assert_not_called()
    assert _inventory(case) == before


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_damaged_input_cannot_create_a_verification(verification_case, failure):
    case = verification_case
    digest = case.match["input_provenance"]["observations"]["content_digest"]
    if failure == "missing":
        del case.objects.metadata[digest]
    else:
        case.objects.objects[digest] = b"damaged retained observations"
    before = _inventory(case)
    with pytest.raises(NotFound if failure == "missing" else IntegrityError):
        case.service.verify(case.issued.issued_forecast_id, _VALID)
    assert _inventory(case) == before


@pytest.mark.parametrize("failure", ["unknown", "wrong_type", "missing", "corrupt"])
def test_read_rejects_unknown_wrong_type_or_damaged_saved_result(verification_case, failure):
    case = verification_case
    verified = case.service.verify(case.issued.issued_forecast_id, _VALID)
    identifier = ArtifactId(verified["verification_id"])
    expected = NotFound
    if failure == "unknown":
        identifier = ArtifactId.generate()
    elif failure == "wrong_type":
        identifier = ArtifactId(case.match["input_provenance"]["observations"]["artifact_id"])
    elif failure == "missing":
        del case.objects.metadata[verified["artifact"]["content_digest"]]
    else:
        case.objects.objects[verified["artifact"]["content_digest"]] = b"damaged saved fact"
        expected = IntegrityError
    before = _inventory(case)
    case.read.side_effect = AssertionError("Readback read a forecast")
    case.preview.side_effect = AssertionError("Readback performed matching")
    with pytest.raises(expected):
        case.service.read(identifier)
    assert _inventory(case) == before


@pytest.mark.parametrize("command", ["verify", "read"])
def test_cli_routes_only_the_explicit_operation(monkeypatch, capsys, command):
    service = Mock()
    service.verify.return_value = service.read.return_value = {"status": "verified"}
    monkeypatch.setattr(application, "configured_service", Mock(return_value=service))
    identifier = uuid4()
    args = (
        ["verify", "--issued-forecast-id", str(identifier), "--valid-time", _iso(_VALID)]
        if command == "verify"
        else ["read", "--verification-id", f"art_{identifier}"]
    )
    assert application.main(args) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "verified"}
    if command == "verify":
        service.verify.assert_called_once_with(identifier, _VALID)
        service.read.assert_not_called()
    else:
        service.read.assert_called_once_with(ArtifactId(f"art_{identifier}"))
        service.verify.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        ["verify", "--issued-forecast-id", "bad", "--valid-time", _iso(_VALID)],
        ["verify", "--issued-forecast-id", str(UUID(int=1)), "--valid-time", "2026-01-01T13:00:00"],
        ["read", "--verification-id", "bad"],
    ],
)
def test_cli_rejects_invalid_input_before_connecting(monkeypatch, args):
    factory = Mock(side_effect=AssertionError("Invalid CLI input opened storage"))
    monkeypatch.setattr(application, "configured_service", factory)
    with pytest.raises(SystemExit) as exc:
        application.main(args)
    assert exc.value.code == 2
    factory.assert_not_called()


@pytest.mark.parametrize("failure", [NotFound("secret ID"), RuntimeError("secret DSN")])
def test_cli_sanitizes_storage_failure(monkeypatch, capsys, failure):
    monkeypatch.setattr(application, "configured_service", Mock(side_effect=failure))
    assert application.main(["read", "--verification-id", str(ArtifactId.generate())]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "error" in json.loads(captured.err)
    assert "secret" not in captured.err


def test_cli_reports_unavailable_without_claiming_a_saved_result(monkeypatch, capsys):
    service = Mock()
    service.verify.return_value = {"status": "unavailable", "verification_id": None}
    monkeypatch.setattr(application, "configured_service", Mock(return_value=service))
    assert (
        application.main(
            ["verify", "--issued-forecast-id", str(uuid4()), "--valid-time", _iso(_VALID)]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out) == service.verify.return_value


def test_window_continues_and_reports_each_outcome(verification_case, monkeypatch):
    case = verification_case
    monkeypatch.setattr(application, "select_issued_forecast_hours", case.issuer.select_hours)
    verified = {
        "status": "verified",
        "verification_id": "art_saved",
        "result": {"reasons": [], "temperature_error": {"value": 2.25, "unit": "K"}},
    }
    unavailable = {
        "status": "unavailable",
        "verification_id": None,
        "result": {
            "reasons": ["No observation"],
            "temperature_error": {"value": None, "unit": "K"},
        },
    }
    verify = Mock(
        side_effect=[
            verified,
            unavailable,
            {
                **unavailable,
                "status": "ineligible",
                "result": {**unavailable["result"], "reasons": ["Issued too late"]},
            },
            {**verified, "already_existing": True},
            RuntimeError("secret storage details"),
            verified,
        ]
    )
    monkeypatch.setattr(case.service, "verify", verify)
    result = case.service.verify_window(
        latitude=45.8,
        longitude=-93.1,
        start_valid_time=_VALID,
        end_valid_time=_VALID + timedelta(hours=6),
    )
    assert result["summary"] == {
        "verified": 2,
        "unavailable": 1,
        "ineligible": 1,
        "already_existing": 1,
        "errors": 1,
    }
    assert [row["status"] for row in result["results"]] == [
        "verified",
        "unavailable",
        "ineligible",
        "already_existing",
        "error",
        "verified",
    ]
    assert result["results"][1]["reasons"] == ["No observation"]
    assert result["results"][2]["reasons"] == ["Issued too late"]
    assert "secret" not in json.dumps(result)
    assert verify.call_count == 6
    for index, call in enumerate(verify.call_args_list):
        assert call.args == (case.issued.issued_forecast_id, _VALID + timedelta(hours=index))
        assert call.kwargs == {"report_reuse": True}


def test_empty_window_does_not_attempt_verification(verification_case, monkeypatch):
    case = verification_case
    monkeypatch.setattr(application, "select_issued_forecast_hours", case.issuer.select_hours)
    verify = Mock(side_effect=AssertionError("Empty window attempted verification"))
    monkeypatch.setattr(case.service, "verify", verify)
    result = case.service.verify_window(
        latitude=0.0,
        longitude=0.0,
        start_valid_time=_VALID,
        end_valid_time=_VALID + timedelta(hours=1),
    )
    assert result["results"] == []
    assert result["summary"] == {
        "verified": 0,
        "unavailable": 0,
        "ineligible": 0,
        "already_existing": 0,
        "errors": 0,
    }
    verify.assert_not_called()


@pytest.mark.parametrize("errors", [0, 1])
def test_window_cli_uses_one_service_and_reports_processing_errors(monkeypatch, capsys, errors):
    service = Mock()
    service.verify_window.return_value = {"summary": {"errors": errors, "unavailable": 1}}
    factory = Mock(return_value=service)
    monkeypatch.setattr(application, "configured_service", factory)
    assert (
        application.main(
            [
                "window",
                "--lat",
                "45.8",
                "--lon",
                "-93.1",
                "--start-valid-time",
                _iso(_VALID),
                "--end-valid-time",
                _iso(_CUTOFF),
            ]
        )
        == errors
    )
    factory.assert_called_once_with()
    service.verify_window.assert_called_once_with(
        latitude=45.8,
        longitude=-93.1,
        start_valid_time=_VALID,
        end_valid_time=_CUTOFF,
    )
    assert json.loads(capsys.readouterr().out) == service.verify_window.return_value


@pytest.mark.parametrize("start", [_iso(_CUTOFF), "2026-01-01T13:00:00"])
def test_window_cli_rejects_invalid_interval_before_storage(monkeypatch, start):
    factory = Mock(side_effect=AssertionError("Invalid window opened storage"))
    monkeypatch.setattr(application, "configured_service", factory)
    with pytest.raises(SystemExit) as exc:
        application.main(
            [
                "window",
                "--lat",
                "45.8",
                "--lon",
                "-93.1",
                "--start-valid-time",
                start,
                "--end-valid-time",
                _iso(_CUTOFF),
            ]
        )
    assert exc.value.code == 2
    factory.assert_not_called()
