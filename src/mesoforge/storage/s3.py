"""S3-compatible immutable artifact object store (plan Section 4.9/4.10).

Implements the ``ArtifactObjectStore`` protocol against boto3; MinIO is
the local/CI service. Bytes are content-addressed at
``objects/sha256/<first-2-hex>/<remaining-62-hex>``.

Codex review (t_9bb13e2b, finding 2) found the previous implementation
trusted the caller's claimed ``content_digest`` without hashing ``data``,
performed an unconditional (non-atomic) ``copy_object`` promotion that
could overwrite an existing content-addressed key under concurrent
writers, and never verified bytes already present at the final key
before returning them as if they matched. All three are fixed below:

- ``put_if_absent`` always computes ``sha256(data)`` itself and rejects a
  caller-claimed digest that does not match the actual bytes (fail
  closed on a lying/corrupt caller rather than silently trusting it);
- promotion uses a conditional ``PutObject`` with ``If-None-Match: *``
  directly to the final content-addressed key (S3-compatible atomic
  "create if absent"; MinIO and AWS S3 both support this precondition).
  Two concurrent writers racing to create the same key: exactly one
  ``PutObject`` succeeds, the loser gets HTTP 412 Precondition Failed and
  is treated as "already exists", then re-verifies via ``get_verified``
  that the winner's bytes actually match this writer's own payload
  digest before returning -- surfacing an ``IntegrityError`` rather than
  silently reporting success if two writers ever disagree on the bytes
  for the same digest (which should be cryptographically impossible for
  honest callers, but must not be silently trusted);
- when the object already exists (the pre-check ``head_object`` path),
  the existing bytes are hash-verified against the expected digest
  before being reported as a successful ``put_if_absent``, rather than
  trusting the previously stored ``ContentLength``/metadata alone.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

from mesoforge.common.errors import IntegrityError, NotFound


def content_addressed_key(content_digest: str) -> str:
    algorithm, _, hex_part = content_digest.partition(":")
    if algorithm != "sha256" or len(hex_part) != 64:
        raise ValueError(f"expected a sha256:<64 hex> digest, got {content_digest!r}")
    return f"objects/sha256/{hex_part[:2]}/{hex_part[2:]}"


@dataclass(frozen=True)
class S3StoredObject:
    content_digest: str
    storage_uri: str
    media_type: str
    byte_size: int


def _is_precondition_failed(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"PreconditionFailed", "412"} or status == 412


def _is_not_found(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey"} or status == 404


class S3ArtifactObjectStore:
    def __init__(self, *, bucket: str, endpoint_url: str, access_key: str, secret_key: str) -> None:
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self._bucket)

    def _storage_uri(self, key: str) -> str:
        return f"s3://{self._bucket}/{key}"

    def put_if_absent(self, content_digest: str, data: bytes, media_type: str) -> S3StoredObject:
        # Never trust a claimed digest: hash the actual bytes ourselves.
        # A caller-supplied content_digest that disagrees with the real
        # hash of `data` is a programming error upstream (callers are
        # expected to pass Digest.of_bytes(data)); fail closed rather
        # than storing corrupt/mislabeled content under the wrong key.
        actual_digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
        if actual_digest != content_digest:
            raise IntegrityError(
                f"put_if_absent called with content_digest {content_digest!r} but the "
                f"supplied bytes actually hash to {actual_digest!r}; refusing to store "
                "under a key that does not match the payload"
            )

        final_key = content_addressed_key(content_digest)
        storage_uri = self._storage_uri(final_key)

        # Fast path: object already exists. Verify its bytes match the
        # expected digest before reporting success -- never trust stored
        # ContentLength/metadata alone as proof the content is correct.
        try:
            self._client.head_object(Bucket=self._bucket, Key=final_key)
            existing = self.get_verified(storage_uri, content_digest)
            return S3StoredObject(
                content_digest=content_digest,
                storage_uri=storage_uri,
                media_type=media_type,
                byte_size=len(existing),
            )
        except ClientError as exc:
            if not _is_not_found(exc):
                raise

        # Atomic no-overwrite create: PutObject with If-None-Match: "*"
        # succeeds only if the key does not already exist server-side.
        # This is the S3-compatible conditional-write primitive (both
        # AWS S3 and MinIO support it) that makes promotion race-free
        # without a separate temp-key + copy_object dance, and without
        # ever risking an unconditional overwrite of existing bytes.
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=final_key,
                Body=data,
                ContentType=media_type,
                IfNoneMatch="*",
            )
            return S3StoredObject(
                content_digest=content_digest,
                storage_uri=storage_uri,
                media_type=media_type,
                byte_size=len(data),
            )
        except ClientError as exc:
            if not _is_precondition_failed(exc):
                raise
            # Lost the race: another writer created this key first.
            # Re-verify that the winner's bytes actually match our own
            # payload digest before treating this as success -- two
            # different payloads can never legitimately share a sha256
            # content digest, so this should always pass for honest
            # callers, but it must not be silently assumed.
            winner_bytes = self.get_verified(storage_uri, content_digest)
            return S3StoredObject(
                content_digest=content_digest,
                storage_uri=storage_uri,
                media_type=media_type,
                byte_size=len(winner_bytes),
            )

    def get_verified(self, storage_uri: str, expected_digest: str) -> bytes:
        key = self._key_from_uri(storage_uri)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                raise NotFound(f"no object at {storage_uri!r}") from exc
            raise
        data = response["Body"].read()
        actual_digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
        if actual_digest != expected_digest:
            raise IntegrityError(
                f"checksum mismatch retrieving {storage_uri!r}: expected "
                f"{expected_digest!r}, got {actual_digest!r}"
            )
        return data

    def exists_verified(self, storage_uri: str, expected_digest: str) -> bool:
        try:
            self.get_verified(storage_uri, expected_digest)
        except (NotFound, IntegrityError):
            return False
        return True

    def delete_unregistered(self, storage_uri: str) -> None:
        key = self._key_from_uri(storage_uri)
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def _key_from_uri(self, storage_uri: str) -> str:
        prefix = f"s3://{self._bucket}/"
        if not storage_uri.startswith(prefix):
            raise ValueError(
                f"storage_uri {storage_uri!r} does not belong to bucket {self._bucket!r}"
            )
        return storage_uri[len(prefix) :]
