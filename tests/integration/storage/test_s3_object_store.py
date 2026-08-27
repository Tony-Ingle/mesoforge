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


class TestS3ArtifactObjectStoreFailClosedConcurrency:
    """Finding 2 (Codex review t_9bb13e2b): put_if_absent must hash
    incoming bytes itself (never trust a claimed digest), never overwrite
    an existing content-addressed key under concurrent writers, and
    verify an existing object before reporting it as a successful put."""

    def test_rejects_claimed_digest_that_does_not_match_bytes(self) -> None:
        store = _make_store()
        payload_a = _unique_bytes()
        payload_b = _unique_bytes()
        digest_of_a = Digest.of_bytes(payload_a)

        # Caller claims digest_of_a's key but actually supplies payload_b's
        # bytes -- this must be rejected, not silently stored under the
        # wrong content-addressed key.
        with pytest.raises(IntegrityError):
            store.put_if_absent(str(digest_of_a), payload_b, "application/octet-stream")

    def test_same_key_different_bytes_second_writer_does_not_overwrite(self) -> None:
        store = _make_store()
        payload_a = _unique_bytes()
        digest = Digest.of_bytes(payload_a)

        first = store.put_if_absent(str(digest), payload_a, "application/octet-stream")

        # A second writer racing with a *different* payload but claiming
        # the *same* digest (a corrupt/malicious caller) must never be
        # able to overwrite the first writer's bytes at that key. Since
        # put_if_absent now hashes its own input, a genuinely different
        # payload can only be submitted under its own (different) digest
        # -- claiming the first digest with different bytes is rejected
        # by the digest-match check before any write is attempted.
        payload_b = _unique_bytes()
        with pytest.raises(IntegrityError):
            store.put_if_absent(str(digest), payload_b, "application/octet-stream")

        # The original bytes at the key are untouched.
        fetched = store.get_verified(first.storage_uri, str(digest))
        assert fetched == payload_a

    def test_concurrent_writers_different_payloads_each_land_under_own_digest(self) -> None:
        store = _make_store()
        payloads = [_unique_bytes() for _ in range(6)]
        digests = [Digest.of_bytes(p) for p in payloads]

        def _put(index: int) -> str:
            stored = store.put_if_absent(
                str(digests[index]), payloads[index], "application/octet-stream"
            )
            return stored.storage_uri

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            uris = list(executor.map(_put, range(len(payloads))))

        # Distinct payloads land at distinct content-addressed keys, and
        # each round-trips to exactly its own bytes -- no cross-writer
        # corruption under concurrency.
        assert len(set(uris)) == len(payloads)
        for uri, digest, payload in zip(uris, digests, payloads, strict=True):
            assert store.get_verified(uri, str(digest)) == payload

    def test_concurrent_writers_same_content_race_to_create_without_error(self) -> None:
        store = _make_store()
        payload = _unique_bytes()
        digest = Digest.of_bytes(payload)

        def _put() -> str:
            stored = store.put_if_absent(str(digest), payload, "application/octet-stream")
            return stored.storage_uri

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            uris = list(executor.map(lambda _: _put(), range(16)))

        # All 16 concurrent writers agree on one final key/content, and
        # every one of them (winner and losers alike) observes bytes
        # that verify against the shared digest -- the conditional
        # If-None-Match create makes this race-free.
        assert len(set(uris)) == 1
        assert store.get_verified(uris[0], str(digest)) == payload

    def test_existing_object_is_hash_verified_before_reporting_success(self) -> None:
        store = _make_store()
        payload = _unique_bytes()
        digest = Digest.of_bytes(payload)
        first = store.put_if_absent(str(digest), payload, "application/octet-stream")

        # Calling put_if_absent again with the identical (correct) bytes
        # must succeed and re-verify, not just trust that a same-named
        # key already exists.
        second = store.put_if_absent(str(digest), payload, "application/octet-stream")
        assert second.storage_uri == first.storage_uri
        assert second.byte_size == len(payload)
