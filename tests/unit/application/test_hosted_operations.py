"""Hosted operations: redaction, code identity, migration safety, object backup, status."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mesoforge.application import operations
from mesoforge.application.code_revision import current_code_revision
from mesoforge.application.runtime_log import REDACTED, event, redact
from mesoforge.application.worker_status import main as worker_status_main
from mesoforge.common.identifiers import Digest

REPO = Path(__file__).resolve().parents[3]


def test_redaction_removes_credentials_but_keeps_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MESOFORGE_S3_SECRET_KEY", "0123456789abcdef0123")
    monkeypatch.setenv("MESOFORGE_S3_ACCESS_KEY", "mesoforge")
    monkeypatch.setenv("MESOFORGE_PG_PASSWORD", "short")
    monkeypatch.setenv(
        "MESOFORGE_DATABASE_DSN", "postgresql+psycopg://svc:supersecretpassword@db:5432/mesoforge"
    )
    text = (
        "secret 0123456789abcdef0123 at postgresql+psycopg://svc:supersecretpassword@db:5432/x "
        "and postgresql://u:pw@h/db token sk-proj-abcdefghijklmnop Bearer abcdefghijklmnop "
        "path /var/lib/mesoforge/runtime"
    )
    cleaned = redact({"message": text, "rows": [text], "count": 3})
    message = cleaned["message"]
    for secret in ("0123456789abcdef0123", "supersecretpassword", ":pw@", "sk-proj", "Bearer abc"):
        assert secret not in message
    assert "/var/lib/mesoforge/runtime" in message  # an access-key name is not a secret
    assert message.count(REDACTED) >= 4
    assert cleaned["rows"] == [message] and cleaned["count"] == 3


def test_event_is_one_redacted_json_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-000000000000000000")
    stream = io.StringIO()
    record = event(
        "forecast-worker",
        "location_result",
        stream=stream,
        clock=lambda: datetime(2026, 7, 1, 13, tzinfo=UTC),
        reason="provider rejected sk-live-000000000000000000",
    )
    line = stream.getvalue()
    assert line.count("\n") == 1
    parsed = json.loads(line)
    assert parsed == record
    assert parsed["ts"] == "2026-07-01T13:00:00Z" and "sk-live" not in line


def test_code_revision_prefers_the_image_build_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESOFORGE_CODE_REVISION", "a" * 40)
    assert current_code_revision(Path("/nonexistent")) == "a" * 40
    monkeypatch.setenv("MESOFORGE_CODE_REVISION", "not-a-revision")
    with pytest.raises(ValueError):
        current_code_revision(REPO)
    monkeypatch.delenv("MESOFORGE_CODE_REVISION")
    revision = current_code_revision(REPO)
    assert len(revision) == 40 and int(revision, 16) >= 0


def test_storage_identities_use_image_revision_without_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mesoforge.application import (
        issued_qpf_verification,
        prepared_mrms,
        prepared_observations,
        station_discovery,
    )

    monkeypatch.setenv("MESOFORGE_CODE_REVISION", "b" * 40)

    def no_git(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Image code identity attempted a Git subprocess")

    monkeypatch.setattr("mesoforge.application.code_revision.subprocess.run", no_git)
    for module in (
        issued_qpf_verification,
        prepared_mrms,
        prepared_observations,
        station_discovery,
    ):
        assert module._identity()["git_commit"] == "b" * 40


def test_database_target_never_includes_the_password() -> None:
    target = operations.database_target("postgresql+psycopg://svc:pw123456789@db:5432/mesoforge")
    assert target == {
        "driver": "postgresql+psycopg",
        "host": "db",
        "port": 5432,
        "database": "mesoforge",
        "username": "svc",
    }


def test_migrate_refuses_an_unexpected_or_mismatched_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrades: list[None] = []
    import mesoforge.storage.postgres.schema as schema

    monkeypatch.setattr(schema, "upgrade_to_head", lambda: upgrades.append(None))
    monkeypatch.setenv("MESOFORGE_DATABASE_DSN", "postgresql+psycopg://svc:x@db:5432/mesoforge")
    monkeypatch.delenv("MESOFORGE_ALEMBIC_DSN", raising=False)
    with pytest.raises(RuntimeError, match="not 'surley_demo'"):
        operations.migrate("surley_demo")
    monkeypatch.setenv("MESOFORGE_ALEMBIC_DSN", "postgresql+psycopg://owner:y@db:5432/other")
    with pytest.raises(RuntimeError, match="different databases"):
        operations.migrate("other")
    assert upgrades == []


class FakeStore:
    def __init__(self, bucket: str) -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}

    def get_verified(self, uri: str, digest: Digest) -> bytes:
        return self.objects[str(digest)]

    def put_if_absent(self, digest: Digest, data: bytes, media_type: str) -> Any:
        self.objects[str(digest)] = data
        value = str(digest).split(":")[1]
        return SimpleNamespace(
            storage_uri=f"s3://{self.bucket}/objects/sha256/{value[:2]}/{value[2:]}"
        )


def test_object_export_and_import_round_trip_with_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeStore("prod")
    rows = []
    for payload in (b"first object", b"second object"):
        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        source.objects[digest] = payload
        value = digest.split(":")[1]
        rows.append(
            {
                "content_digest": digest,
                "media_type": "application/json",
                "byte_size": len(payload),
                "storage_uri": f"s3://prod/objects/sha256/{value[:2]}/{value[2:]}",
            }
        )
    monkeypatch.setattr(operations, "_stored_objects", lambda: rows)
    monkeypatch.setattr(operations, "object_store", lambda ensure_bucket: source)
    monkeypatch.setenv("MESOFORGE_S3_BUCKET", "prod")
    exported = operations.export_objects(tmp_path / "backup")
    assert exported["objects"] == 2 and exported["copied"] == 2
    assert operations.export_objects(tmp_path / "backup")["already_present"] == 2
    restored = FakeStore("prod")
    monkeypatch.setattr(operations, "object_store", lambda ensure_bucket: restored)
    assert operations.import_objects(tmp_path / "backup")["objects"] == 2
    assert restored.objects == source.objects
    corrupt = operations._object_path(tmp_path / "backup", rows[0]["content_digest"])
    corrupt.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="differs from its digest"):
        operations.import_objects(tmp_path / "backup")
    monkeypatch.setenv("MESOFORGE_S3_BUCKET", "elsewhere")
    with pytest.raises(ValueError, match="bucket"):
        operations.import_objects(tmp_path / "backup")
    with pytest.raises(ValueError, match="outside the repository"):
        operations.export_objects(REPO / "backup")


def test_status_sections_fail_independently_and_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("MESOFORGE_DATABASE_DSN", "MESOFORGE_S3_BUCKET", "MESOFORGE_S3_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "locations.json"
    config.write_text(json.dumps({"locations": [{"id": "a", "lat": 45.0, "lon": -93.0}]}), "utf-8")
    report = operations.status(
        tmp_path / "runtime", config, now=datetime(2026, 7, 1, 12, tzinfo=UTC)
    )
    assert report["schema"]["status"] == "unavailable"
    assert report["policies"]["status"] == "unavailable"
    assert report["latest_issuances"]["status"] == "unavailable"
    assert report["baseline"]["reasons"][0] == "no_published_baseline"
    # Unreadable governance never lets a baseline report ready.
    assert report["baseline"]["reasons"][1].startswith("governance_unavailable")
    assert report["guidance_worker"]["health"]["reason"] == "no_status"
    assert report["next_run"]["next_run"] == "2026-07-01T13:00:00Z"
    text = operations.render_status(report)
    assert "Guidance worker: NOT healthy" in text
    assert "Next scheduled run: 2026-07-01T08:00:00-05:00" in text


def test_empty_object_export_creates_a_valid_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MESOFORGE_S3_BUCKET", "prod")
    monkeypatch.setattr(operations, "_stored_objects", lambda: [])
    monkeypatch.setattr(operations, "object_store", lambda ensure_bucket: FakeStore("prod"))
    destination = tmp_path / "new" / "backup"
    assert operations.export_objects(destination)["objects"] == 0
    assert operations.validate_export(destination) == {
        "objects": 0,
        "bucket": "prod",
        "valid": True,
    }


@pytest.mark.parametrize("defect", ["digest", "size", "key", "schema", "duplicate", "bucket"])
def test_import_validates_entire_export_before_any_object_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    rows = []
    for payload in (b"first", b"second"):
        digest = str(Digest.of_bytes(payload))
        path = operations._object_path(tmp_path, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        value = digest.split(":")[1]
        rows.append(
            {
                "content_digest": digest,
                "storage_uri": f"s3://prod/objects/sha256/{value[:2]}/{value[2:]}",
                "media_type": "application/json",
                "byte_size": len(payload),
            }
        )
    manifest: dict[str, Any] = {
        "schema_version": "mesoforge.object-export.v1",
        "bucket": "prod",
        "objects": rows,
    }
    if defect == "digest":
        operations._object_path(tmp_path, rows[-1]["content_digest"]).write_bytes(b"corrupt")
    elif defect == "size":
        rows[-1]["byte_size"] += 1
    elif defect == "key":
        rows[-1]["storage_uri"] += "-wrong-key"
    elif defect == "schema":
        manifest["schema_version"] = "unsupported"
    elif defect == "duplicate":
        rows.append(rows[-1])
    else:
        manifest["bucket"] = "other"
    (tmp_path / operations.EXPORT_MANIFEST).write_text(json.dumps(manifest), "utf-8")
    monkeypatch.setenv("MESOFORGE_S3_BUCKET", "prod")

    def forbidden(**kwargs: Any) -> None:
        pytest.fail("Invalid export reached the destination object store")

    monkeypatch.setattr(operations, "object_store", forbidden)
    with pytest.raises(ValueError):
        operations.import_objects(tmp_path)


def test_object_export_rejects_non_hex_digest_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        operations._object_path(tmp_path, "sha256:" + "../" * 21 + "x")


def test_operations_readiness_checks_runtime_revision_and_fails_if_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revisions = []

    def readiness(*args: Any, **kwargs: Any) -> dict[str, Any]:
        revisions.append(kwargs["expected_code_revision"])
        return {"ready": True, "reasons": []}

    monkeypatch.setattr(operations, "baseline_readiness", readiness)
    monkeypatch.setenv("MESOFORGE_CODE_REVISION", "b" * 40)
    now = datetime(2026, 7, 1, tzinfo=UTC)
    assert operations._baseline_readiness(tmp_path, [], now=now, governance=None)["ready"]
    monkeypatch.setenv("MESOFORGE_CODE_REVISION", "invalid")
    result = operations._baseline_readiness(tmp_path, [], now=now, governance=None)
    assert not result["ready"]
    assert result["reasons"][0].startswith("code_revision_unavailable:")
    assert revisions == ["b" * 40, None]


def test_worker_status_health_command_exit_codes(tmp_path: Path) -> None:
    assert worker_status_main(["guidance-health", "--root", str(tmp_path)]) == 1
