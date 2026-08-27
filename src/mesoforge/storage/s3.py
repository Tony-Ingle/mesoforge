"""S3-compatible immutable artifact object store (plan Section 4.9/4.10).

Implements the ``ArtifactObjectStore`` protocol against boto3; MinIO is
the local/CI service. Bytes are content-addressed at
``objects/sha256/<first-2-hex>/<remaining-62-hex>``. Uploads go to a
temporary key first, then are server-side copied to the final
content-addressed key without ever overwriting an existing key --
promotion never re-uploads bytes twice over the network.
"""

from __future__ import annotations

import hashlib
import uuid
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
        final_key = content_addressed_key(content_digest)

        try:
            head = self._client.head_object(Bucket=self._bucket, Key=final_key)
            return S3StoredObject(
                content_digest=content_digest,
                storage_uri=self._storage_uri(final_key),
                media_type=head.get("ContentType", media_type),
                byte_size=head["ContentLength"],
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey"}:
                raise

        temp_key = f"tmp/{uuid.uuid4()}"
        self._client.put_object(
            Bucket=self._bucket, Key=temp_key, Body=data, ContentType=media_type
        )
        try:
            # Server-side copy to the final content-addressed key. Using
            # copy (not a second upload) avoids sending bytes over the
            # network twice; CopySource references the temp object we
            # just wrote.
            self._client.copy_object(
                Bucket=self._bucket,
                Key=final_key,
                CopySource={"Bucket": self._bucket, "Key": temp_key},
                ContentType=media_type,
            )
        finally:
            self._client.delete_object(Bucket=self._bucket, Key=temp_key)

        return S3StoredObject(
            content_digest=content_digest,
            storage_uri=self._storage_uri(final_key),
            media_type=media_type,
            byte_size=len(data),
        )

    def get_verified(self, storage_uri: str, expected_digest: str) -> bytes:
        key = self._key_from_uri(storage_uri)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
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
