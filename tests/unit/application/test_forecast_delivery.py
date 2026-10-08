"""Saved-issuance presentation is independent of forecast generation and delivery."""

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from mesoforge.application import forecast_delivery as delivery
from mesoforge.presentation.forecast_document import ForecastCoverageError, build_forecast_document
from mesoforge.presentation.forecast_pdf import render_forecast_pdf
from tests.unit.presentation.test_forecast_product import LOCATION, five_day_saved, rolling_saved


def test_same_issuance_render_is_read_only_repeatable_and_cannot_send_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = five_day_saved(count=36)
    original = deepcopy(saved)
    reader = Mock(return_value=saved)
    monkeypatch.setattr(delivery, "read_issued_forecast", reader)
    no_journal = Mock(side_effect=AssertionError("Rendering accessed writable storage"))
    monkeypatch.setattr(delivery, "configured_journal", no_journal)
    path = tmp_path / "fixture.pdf"
    args = [
        "--issued-id",
        saved["issued_forecast_id"],
        "--location",
        "grasston",
        "--pdf",
        str(path),
    ]
    assert delivery.main(["render", *args]) == 0
    first = path.read_bytes()
    assert delivery.main(["render", *args]) == 0
    assert path.read_bytes() == first
    assert saved == original
    assert (
        delivery.main(["send", *args, "--recipient", "user@example.test", "--confirm-reviewed"])
        == 1
    )
    no_journal.assert_not_called()
    assert reader.call_count == 3


def test_current_36_hour_issuance_cannot_be_labeled_five_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = five_day_saved(count=36)
    monkeypatch.setattr(delivery, "read_issued_forecast", lambda _: saved)
    forbidden = Mock(side_effect=AssertionError("Unsupported forecast tried delivery"))
    monkeypatch.setattr(delivery, "configured_journal", forbidden)
    with pytest.raises(ForecastCoverageError, match="36 hours"):
        build_forecast_document(saved, location=LOCATION)
    path = tmp_path / "not-created.pdf"
    assert (
        delivery.main(
            [
                "render",
                "--issued-id",
                saved["issued_forecast_id"],
                "--location",
                "grasston",
                "--product",
                "5-day",
                "--pdf",
                str(path),
            ]
        )
        == 1
    )
    assert not path.exists()
    forbidden.assert_not_called()


def test_rolling_product_reads_existing_issuance_without_forecast_or_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = rolling_saved()
    original = deepcopy(saved)
    reader = Mock(return_value=saved)
    monkeypatch.setattr(delivery, "read_issued_forecast", reader)
    journal = Mock(side_effect=AssertionError("Rendering accessed delivery storage"))
    monkeypatch.setattr(delivery, "configured_journal", journal)
    path = tmp_path / "five-day-outlook.pdf"
    args = [
        "render",
        "--issued-id",
        saved["issued_forecast_id"],
        "--location",
        "grasston",
        "--product",
        "120-hour",
        "--pdf",
        str(path),
    ]
    assert delivery.main(args) == 0
    first = path.read_bytes()
    assert delivery.main(args) == 0
    assert first == path.read_bytes() and saved == original
    journal.assert_not_called()
    assert reader.call_count == 2
    document = build_forecast_document(saved, location=LOCATION, hours=120)
    subject, text, _ = delivery.email_content(document)
    assert "MesoForge 5-Day Weather Outlook" in subject
    assert "120 hours" in text
    assert "Oct 08, 2026 10:00 CDT" in text and "Oct 13, 2026 10:00 CDT" in text


def test_review_gate_checks_real_evidence_expiry_and_exact_bytes() -> None:
    document = build_forecast_document(five_day_saved(), location=LOCATION)
    pdf = render_forecast_pdf(document)
    before = datetime.fromisoformat(document["valid_start"])
    with pytest.raises(ValueError, match="Synthetic"):
        delivery.reviewed_pdf(document, pdf, now=before)
    # Synthetic unit-only real marker exercises gate logic; never a stored production case.
    document["fixture"] = False
    pdf = render_forecast_pdf(document)
    assert delivery.reviewed_pdf(document, pdf, now=before)["pages"] == 2
    with pytest.raises(ValueError, match="dated after"):
        delivery.reviewed_pdf(document, pdf, now=before - timedelta(hours=1))
    with pytest.raises(ValueError, match="differs"):
        delivery.reviewed_pdf(document, pdf + b"modified", now=before)
    with pytest.raises(ValueError, match="expired"):
        delivery.reviewed_pdf(document, pdf, now=datetime.fromisoformat(document["valid_end"]))
    with pytest.raises(ValueError, match="timezone"):
        delivery.reviewed_pdf(document, pdf, now=datetime(2026, 1, 1))


def test_message_is_derived_from_saved_document_without_internal_identifiers() -> None:
    document = build_forecast_document(five_day_saved(), location=LOCATION)
    subject, plain, rich = delivery.email_content(document)
    assert "Grasston" in subject and "5-Day" in subject
    assert document["headline"] in plain
    assert document["issued_forecast_id"] not in plain
    assert "sha256:" not in plain
    assert "<html>" in rich
    assert "America/Chicago" in plain


def test_operational_outlook_uses_exact_36_hour_window_and_product_identity() -> None:
    from mesoforge.application.email_delivery import delivery_identity
    from mesoforge.common.identifiers import IssuedForecastId

    saved = five_day_saved(reference="2026-10-08T02:00:00Z", count=36)
    document = build_forecast_document(saved, location=LOCATION, hours=36)
    subject, plain, _ = delivery.email_content(document)
    assert "36-Hour Weather Outlook" in subject and "5-Day" not in subject
    assert "Oct 07, 2026 21:00 CDT" in plain
    assert "Oct 09, 2026 09:00 CDT" in plain
    assert document["issued_forecast_id"] not in plain
    pdf = render_forecast_pdf(document)
    assert delivery.validate_pdf(pdf, document)["pages"] == 2
    identifier = IssuedForecastId(saved["issued_forecast_id"])
    assert delivery_identity(identifier, "user@example.test", document["document_policy"]) != (
        delivery_identity(
            identifier, "user@example.test", "mesoforge-five-local-day-presentation.v1"
        )
    )


def test_failure_output_does_not_echo_storage_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sample_credential = "never-echo-this-secret"
    monkeypatch.setattr(
        delivery, "read_issued_forecast", Mock(side_effect=RuntimeError(sample_credential))
    )
    saved = five_day_saved()
    assert (
        delivery.main(
            [
                "render",
                "--issued-id",
                saved["issued_forecast_id"],
                "--location",
                "grasston",
                "--pdf",
                str(tmp_path / "out.pdf"),
            ]
        )
        == 1
    )
    assert sample_credential not in capsys.readouterr().out


@pytest.mark.parametrize("previous,code", [("accepted", 0), ("failed", 1), ("ambiguous", 1)])
def test_duplicate_failure_is_not_reported_as_delivery_success(
    tmp_path,
    monkeypatch,
    previous,
    code,
):
    document = build_forecast_document(five_day_saved(), location=LOCATION)
    path = tmp_path / "unit-fixture.pdf"
    path.write_bytes(render_forecast_pdf(document))
    monkeypatch.setattr(delivery, "saved_document", lambda *_: document)
    monkeypatch.setattr(delivery, "reviewed_pdf", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(delivery, "configured_journal", Mock())
    monkeypatch.setattr(delivery.SmtpSettings, "environment", Mock())
    mocked = Mock(
        return_value={"status": "duplicate_suppressed", "previous_outcome": {"status": previous}}
    )
    monkeypatch.setattr(delivery, "deliver", mocked)
    assert (
        delivery.main(
            [
                "send",
                "--issued-id",
                document["issued_forecast_id"],
                "--location",
                "grasston",
                "--pdf",
                str(path),
                "--recipient",
                "test@example.test",
                "--confirm-reviewed",
            ]
        )
        == code
    )
    mocked.assert_called_once()
