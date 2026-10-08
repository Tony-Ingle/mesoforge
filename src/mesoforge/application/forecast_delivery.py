"""Render or deliver one saved configured-location issuance; never generate a forecast."""

from __future__ import annotations

import argparse
import html
import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from mesoforge.application.batch_forecast import load_locations
from mesoforge.application.delivery_artifacts import configured_journal
from mesoforge.application.email_delivery import MAX_ATTACHMENT_BYTES, SmtpSettings, deliver
from mesoforge.application.issuance import read_issued_forecast
from mesoforge.common.horizon import FIVE_DAY_HORIZON, LEGACY_HORIZON
from mesoforge.common.identifiers import Digest, IssuedForecastId
from mesoforge.presentation.forecast_document import (
    ForecastCoverageError,
    build_forecast_document,
)
from mesoforge.presentation.forecast_pdf import render_forecast_pdf


def validate_pdf(data: bytes, document: dict[str, Any]) -> dict[str, Any]:
    """Machine checks complement the required operator visual review before sending."""
    if not data.startswith(b"%PDF-") or len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Expected an email-sized PDF")
    reader = PdfReader(BytesIO(data), strict=True)
    if len(reader.pages) != 2:
        raise ValueError("The outlook must contain two pages")
    text = "\n".join(page.extract_text() for page in reader.pages)
    if any(word not in text for word in ("MesoForge", document["location"]["name"])):
        raise ValueError("PDF text does not identify its configured forecast")
    if any(token in text for token in ("NaN", "None", "null", '{"', "sha256:")):
        raise ValueError("PDF contains internal or invalid presentation text")
    return {"pages": len(reader.pages), "bytes": len(data), "digest": str(Digest.of_bytes(data))}


def saved_document(
    issued_id: IssuedForecastId, config: Path, location_selector: str, product: str = "36-hour"
) -> dict[str, Any]:
    issued_id = IssuedForecastId(str(issued_id))
    # The selector is an operator-supplied registry key, validated by unique membership.
    # It is not a new durable canonical identifier family.
    locations = [row for row in load_locations(config) if row.get("id") == location_selector]
    if len(locations) != 1:
        raise ValueError("Select exactly one existing configured location")
    saved = read_issued_forecast(UUID(issued_id))
    if IssuedForecastId(saved["issued_forecast_id"]) != issued_id:
        raise ValueError("Issued forecast identity mismatch")
    if product not in {"36-hour", "120-hour", "5-day"}:
        raise ValueError("Unsupported presentation product")
    return build_forecast_document(
        saved,
        location=locations[0],
        hours={
            "36-hour": LEGACY_HORIZON.duration_hours,
            "120-hour": FIVE_DAY_HORIZON.duration_hours,
            "5-day": None,
        }[product],
    )


def reviewed_pdf(document: dict[str, Any], data: bytes, *, now: datetime) -> dict[str, Any]:
    """Gate sending on real, unexpired evidence and the exact deterministic render."""
    if document["fixture"]:
        raise ValueError("Synthetic/unknown-source documents cannot be emailed by this command")
    if now.tzinfo is None:
        raise ValueError("The delivery clock must be timezone-aware")
    if datetime.fromisoformat(document["issued_at"]) > now:
        raise ValueError("An issuance dated after the delivery clock cannot be sent")
    if datetime.fromisoformat(document["valid_end"]) <= now:
        raise ValueError("The forecast has expired; delivery cannot turn replay into a forecast")
    expected = render_forecast_pdf(document)
    if data != expected:
        raise ValueError("Reviewed PDF differs from the deterministic saved-issuance render")
    return validate_pdf(data, document)


def email_content(document: dict[str, Any]) -> tuple[str, str, str]:
    zone = ZoneInfo(document["display_timezone"])
    issue_date = datetime.fromisoformat(document["issued_at"]).astimezone(zone).date()
    name = document["location"]["name"]
    title = document["product_title"]
    subject = f"MesoForge {title} — {name} — {issue_date}"
    start = datetime.fromisoformat(document["valid_start"]).astimezone(zone)
    end = datetime.fromisoformat(document["valid_end"]).astimezone(zone)
    summary = (
        f"{document['headline']}.\n\n"
        f"The complete MesoForge outlook for {name} is attached. "
        f"It covers {start:%b %d, %Y %H:%M %Z} through {end:%b %d, %Y %H:%M %Z} "
        f"in {document['display_timezone']}.\n\n"
        f"AI desk: {document['ai']['display_status']}.\n"
        "Forecasts are subject to change as new weather guidance becomes available."
    )
    rich = (
        "<html><body>"
        + "".join(
            f"<p>{html.escape(paragraph).replace(chr(10), '<br>')}</p>"
            for paragraph in summary.split("\n\n")
        )
        + "</body></html>"
    )
    return subject, summary, rich


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("render", "send"):
        command = commands.add_parser(name)
        command.add_argument("--issued-id", type=IssuedForecastId, required=True)
        command.add_argument("--location", required=True)
        command.add_argument("--config", type=Path, default=Path("configs/locations.json"))
        command.add_argument("--pdf", type=Path, required=True)
        command.add_argument(
            "--product",
            choices=("36-hour", "120-hour", "5-day"),
            default="36-hour",
            help="Saved 36/120-hour outlook, or coverage-gated five complete local calendar days",
        )
        if name == "send":
            command.add_argument("--recipient", required=True)
            command.add_argument("--confirm-reviewed", action="store_true", required=True)
    status = commands.add_parser("status")
    status.add_argument("--delivery-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result: dict[str, Any] = {
                "events": configured_journal().events(Digest(args.delivery_id))
            }
        else:
            document = saved_document(args.issued_id, args.config, args.location, args.product)
            if args.command == "render":
                pdf = render_forecast_pdf(document)
                result = validate_pdf(pdf, document)
                args.pdf.parent.mkdir(parents=True, exist_ok=True)
                args.pdf.write_bytes(pdf)
                result.update(
                    {
                        "issued_forecast_id": str(args.issued_id),
                        "fixture": document["fixture"],
                        "pdf": str(args.pdf),
                    }
                )
            else:
                with args.pdf.open("rb") as source:
                    pdf = source.read(MAX_ATTACHMENT_BYTES + 1)
                if len(pdf) > MAX_ATTACHMENT_BYTES:
                    raise ValueError("Reviewed PDF exceeds the attachment size limit")
                reviewed_pdf(document, pdf, now=datetime.now(UTC))
                subject, text, rich = email_content(document)
                result = deliver(
                    journal=configured_journal(),
                    issued_id=args.issued_id,
                    recipient=args.recipient,
                    subject=subject,
                    text=text,
                    html=rich,
                    attachment=pdf,
                    settings=SmtpSettings.environment(),
                    product_version=document["document_policy"],
                )
    except ForecastCoverageError as exc:
        print(json.dumps({"status": "unsupported_coverage", "reason": str(exc)}))
        return 1
    except Exception as exc:
        # Storage/provider exceptions can include credentials. Never print their text.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "reason": "Forecast coverage, reviewed PDF, configuration or storage "
                    "validation failed; no automatic resend is performed.",
                }
            )
        )
        return 1
    print(json.dumps(result, indent=2))
    outcome = (
        result.get("previous_outcome", {}).get("status")
        if result.get("status") == "duplicate_suppressed"
        else result.get("status")
    )
    return 0 if outcome not in {"failed", "ambiguous"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
