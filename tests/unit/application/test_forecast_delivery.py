"""Saved-issuance presentation is independent of forecast generation and delivery."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
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


@pytest.mark.parametrize(
    "previous,code", [("accepted", 0), ("failed", 1), ("ambiguous", 1), ("pending", 1), (None, 1)]
)
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
    monkeypatch.setattr(
        delivery, "_utc_now", lambda: datetime.fromisoformat(document["valid_start"])
    )
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


class DeliveryClock:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.elapsed = 0.0
        self.waits: list[float] = []

    def clock(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.elapsed += seconds
        self.now += timedelta(seconds=seconds)


@pytest.mark.parametrize("offset", [None, -1, 0, 125, 3600])
def test_release_wait_is_optional_and_bounded_without_real_sleep(offset: int | None) -> None:
    clock = DeliveryClock(datetime(2026, 1, 15, 14, tzinfo=UTC))
    target = clock.now + timedelta(seconds=offset) if offset is not None else None
    released = delivery.wait_for_delivery_release(
        target, clock=clock.clock, monotonic=clock.monotonic, sleep=clock.sleep
    )
    assert released == clock.now
    assert sum(clock.waits) == max(offset or 0, 0)
    assert len(clock.waits) <= 60
    assert all(0 < value <= 60 for value in clock.waits)


def test_release_requires_aware_time_and_rejects_excessive_wait_before_sleep() -> None:
    clock = DeliveryClock(datetime(2026, 1, 15, 14, tzinfo=UTC))
    for target, error in (
        (clock.now + timedelta(seconds=3601), "more than 60 minutes"),
        (clock.now.replace(tzinfo=None), "timezone-aware"),
    ):
        with pytest.raises(ValueError, match=error):
            delivery.wait_for_delivery_release(
                target, clock=clock.clock, monotonic=clock.monotonic, sleep=clock.sleep
            )
    assert not clock.waits
    with pytest.raises(ValueError, match="timezone-aware"):
        delivery.wait_for_delivery_release(
            None,
            clock=lambda: clock.now.replace(tzinfo=None),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )


def test_backwards_wall_clock_does_not_extend_finite_release_budget() -> None:
    clock = DeliveryClock(datetime(2026, 1, 15, 14, tzinfo=UTC))
    target = clock.now + timedelta(seconds=60)

    def rollback(seconds: float) -> None:
        clock.elapsed += seconds
        clock.waits.append(seconds)
        clock.now -= timedelta(seconds=seconds)

    with pytest.raises(TimeoutError, match="finite 60-minute budget"):
        delivery.wait_for_delivery_release(
            target, clock=clock.clock, monotonic=clock.monotonic, sleep=rollback
        )
    assert sum(clock.waits) == 3600
    assert len(clock.waits) == 60


def test_host_suspension_cannot_exceed_release_budget_then_send() -> None:
    clock = DeliveryClock(datetime(2026, 1, 15, 14, tzinfo=UTC))
    target = clock.now + timedelta(seconds=60)

    def oversleep(_: float) -> None:
        clock.elapsed += 3601
        clock.now += timedelta(seconds=3601)

    with pytest.raises(TimeoutError, match="finite 60-minute budget"):
        delivery.wait_for_delivery_release(
            target, clock=clock.clock, monotonic=clock.monotonic, sleep=oversleep
        )


@pytest.mark.parametrize("expire_during_wait", [False, True])
def test_send_validates_before_release_then_rechecks_expiry_without_rereading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expire_during_wait: bool,
) -> None:
    document = build_forecast_document(five_day_saved(), location=LOCATION)
    document["fixture"] = False  # Unit-only marker; never persisted or sent to real SMTP.
    start = datetime.fromisoformat(document["valid_start"])
    end = datetime.fromisoformat(document["valid_end"])
    clock = DeliveryClock(end - timedelta(minutes=5) if expire_during_wait else start)
    target = clock.now + timedelta(minutes=5)
    path = tmp_path / "unit-only.pdf"
    path.write_bytes(render_forecast_pdf(document))
    reader = Mock(return_value=document)
    monkeypatch.setattr(delivery, "saved_document", reader)
    monkeypatch.setattr(delivery, "_utc_now", clock.clock)
    wait = delivery.wait_for_delivery_release
    monkeypatch.setattr(
        delivery,
        "wait_for_delivery_release",
        lambda target, **_: wait(
            target, clock=clock.clock, monotonic=clock.monotonic, sleep=clock.sleep
        ),
    )
    review = delivery.reviewed_pdf

    def reviewed(*args, **kwargs):
        assert not clock.waits
        result = review(*args, **kwargs)
        clock.now += timedelta(seconds=30)  # Expensive render happens before the release wait.
        return result

    monkeypatch.setattr(delivery, "reviewed_pdf", reviewed)
    journal = Mock()
    monkeypatch.setattr(delivery, "configured_journal", journal)
    monkeypatch.setattr(delivery.SmtpSettings, "environment", Mock())
    sent = Mock(return_value={"status": "accepted"})
    monkeypatch.setattr(delivery, "deliver", sent)
    code = delivery.main(
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
            "--not-before",
            target.isoformat(),
        ]
    )
    reader.assert_called_once()
    assert sum(clock.waits) == 270
    assert clock.now == target
    if expire_during_wait:
        assert code == 1
        sent.assert_not_called()
        journal.assert_not_called()
    else:
        assert code == 0
        sent.assert_called_once()


@pytest.mark.parametrize("stamp", ["not-a-date", "2026-01-01T08:00:00", "2026-01-01T08:00-06:00"])
def test_release_cli_rejects_non_utc_timestamps_before_readback(
    stamp: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = Mock(side_effect=AssertionError("Invalid release accessed saved forecast"))
    monkeypatch.setattr(delivery, "saved_document", reader)
    with pytest.raises(SystemExit, match="2"):
        delivery.main(
            [
                "send",
                "--issued-id",
                five_day_saved()["issued_forecast_id"],
                "--location",
                "grasston",
                "--pdf",
                str(tmp_path / "unused.pdf"),
                "--recipient",
                "test@example.test",
                "--confirm-reviewed",
                "--not-before",
                stamp,
            ]
        )
    reader.assert_not_called()


@pytest.mark.parametrize(
    "approval,result_status,expected",
    [
        (["--approved-template", "mesoforge-five-local-day-presentation.v1"], "accepted", 0),
        (["--approved-template", "mesoforge-five-local-day-presentation.v1"], "pending", 1),
        (["--approved-template", "mesoforge-five-local-day-presentation.v1"], None, 1),
        (["--approved-template", "unapproved-template-version"], "accepted", 1),
        ([], "accepted", "parse_error"),
        (["--confirm-reviewed", "--approved-template", "version"], "accepted", "parse_error"),
    ],
)
def test_automated_template_approval_is_explicit_versioned_and_send_success_is_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approval: list[str],
    result_status: str | None,
    expected: int | str,
) -> None:
    document = build_forecast_document(five_day_saved(), location=LOCATION)
    document["fixture"] = False  # Unit-only; no real SMTP or storage.
    reader = Mock(return_value=document)
    monkeypatch.setattr(delivery, "saved_document", reader)
    monkeypatch.setattr(
        delivery, "_utc_now", lambda: datetime.fromisoformat(document["valid_start"])
    )
    path = tmp_path / "unit-only.pdf"
    path.write_bytes(render_forecast_pdf(document))
    review = Mock(wraps=delivery.reviewed_pdf)
    monkeypatch.setattr(delivery, "reviewed_pdf", review)
    journal = Mock()
    monkeypatch.setattr(delivery, "configured_journal", journal)
    monkeypatch.setattr(delivery.SmtpSettings, "environment", Mock())
    sent = Mock(return_value={"status": result_status})
    monkeypatch.setattr(delivery, "deliver", sent)
    args = [
        "send",
        "--issued-id",
        document["issued_forecast_id"],
        "--location",
        "grasston",
        "--pdf",
        str(path),
        "--recipient",
        "test@example.test",
        *approval,
    ]
    if expected == "parse_error":
        with pytest.raises(SystemExit, match="2"):
            delivery.main(args)
        reader.assert_not_called()
        sent.assert_not_called()
    else:
        assert delivery.main(args) == expected
        if "unapproved-template-version" in approval:
            review.assert_not_called()
            sent.assert_not_called()
            journal.assert_not_called()
        else:
            review.assert_called_once()
            sent.assert_called_once()
