"""Integration tests: S3ArtifactObjectStore against real MinIO (plan
Section 4.9/4.10, Task 9 verify command).

MinIO connection settings come from environment variables so this test
works both against the local standalone MinIO binary used in this
development sandbox (no Docker access) and the ``minio`` service
container defined in .github/workflows/ci.yml.
"""

from __future__ import annotations

import concurrent.futures
import os
import uuid

import pytest

from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.common.identifiers import Digest
from mesoforge.storage.s3 import S3ArtifactObjectStore, content_addressed_key

pytestmark = pytest.mark.integration


def _make_store() -> S3ArtifactObjectStore:
    endpoint = os.environ.get("MESOFORGE_TEST_S3_ENDPOINT", "http://127.0.0.1:19100")
    access_key = os.environ.get("MESOFORGE_TEST_S3_ACCESS_KEY", "mesoforge_test")
    secret_key = os.environ.get("MESOFORGE_TEST_S3_SECRET_KEY", "mesoforge_test_password")
    bucket = os.environ.get("MESOFORGE_TEST_S3_BUCKET", f"mesoforge-test-{uuid.uuid4().hex[:8]}")
    return S3ArtifactObjectStore(
        bucket=bucket, endpoint_url=endpoint, access_key=access_key, secret_key=secret_key
    )


def _unique_bytes() -> bytes:
    return f"synthetic-{uuid.uuid4()}".encode()


class TestContentAddressedKey:
    def test_key_derivation(self) -> None:
        digest = Digest.of_bytes(b"hello")
        key = content_addressed_key(str(digest))
        hex_part = str(digest)[7:]
        assert key == f"objects/sha256/{hex_part[:2]}/{hex_part[2:]}"

    def test_rejects_non_sha256_digest(self) -> None:
        with pytest.raises(ValueError, match="sha256"):
            content_addressed_key("md5:" + "a" * 32)


class TestS3ArtifactObjectStore:
    def test_put_then_get_verified_round_trip(self) -> None:
        store = _make_store()
        payload = _unique_bytes()
        digest = Digest.of_bytes(payload)

        stored = store.put_if_absent(str(digest), payload, "application/octet-stream")
        assert stored.content_digest == str(digest)
        assert stored.byte_size == len(payload)
        assert "s3://" in stored.storage_uri
        assert "http" not in stored.storage_uri
        # no credentials leak into the returned URI
        assert "mesoforge_test" not in stored.storage_uri

        fetched = store.get_verified(stored.storage_uri, str(digest))
        assert fetched == payload

    def test_same_byte_idempotency_no_duplicate_key(self) -> None:
        store = _make_store()
        payload = _unique_bytes()
        digest = Digest.of_bytes(payload)

        first = store.put_if_absent(str(digest), payload, "application/octet-stream")
        second = store.put_if_absent(str(digest), payload, "application/octet-stream")
        assert first.storage_uri == second.storage_uri

    def test_exists_verified(self) -> None:
        store = _make_store()
        payload = _unique_bytes()
        digest = Digest.of_bytes(payload)
        stored = store.put_if_absent(str(digest), payload, "application/octet-stream")

        assert store.exists_verified(stored.storage_uri, str(digest)) is True
        wrong_digest = Digest.of_bytes(_unique_bytes())
        assert store.exists_verified(stored.storage_uri, str(wrong_digest)) is False

    def test_wrong_digest_raises_integrity_error(self) -> None:
        store = _make_store()
        payload = _unique_bytes()
        digest = Digest.of_bytes(payload)
        stored = store.put_if_absent(str(digest), payload, "application/octet-stream")

        wrong_digest = Digest.of_bytes(_unique_bytes())
        with pytest.raises(IntegrityError):
            store.get_verified(stored.storage_uri, str(wrong_digest))

    def test_missing_object_raises_not_found(self) -> None:
        store = _make_store()
        fake_digest = Digest.of_bytes(_unique_bytes())
        missing_uri = f"s3://{store._bucket}/{content_addressed_key(str(fake_digest))}"
        with pytest.raises(NotFound):
            store.get_verified(missing_uri, str(fake_digest))

    def test_concurrent_writers_same_content_do_not_error(self) -> None:
        store = _make_store()
        payload = _unique_bytes()
        digest = Digest.of_bytes(payload)

        def _put() -> str:
            stored = store.put_if_absent(str(digest), payload, "application/octet-stream")
            return stored.storage_uri

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            uris = list(executor.map(lambda _: _put(), range(8)))

        assert len(set(uris)) == 1
        fetched = store.get_verified(uris[0], str(digest))
        assert fetched == payload

    def test_delete_unregistered_cleans_up_temp_style_object(self) -> None:
        store = _make_store()
        payload = _unique_bytes()
        digest = Digest.of_bytes(payload)
        stored = store.put_if_absent(str(digest), payload, "application/octet-stream")

        store.delete_unregistered(stored.storage_uri)
        assert store.exists_verified(stored.storage_uri, str(digest)) is False
