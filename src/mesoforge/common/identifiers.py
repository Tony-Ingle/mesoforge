"""Immutable, validated identifier and digest string types (plan Section 4.1).

Each type is a validated ``str`` subclass so it can be used directly as a
dict key, compared with ``==`` against a plain string, and serialized by
Pydantic without a custom encoder. Construction always validates; there is
no unchecked bypass.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, ClassVar

from mesoforge.common.errors import InvalidIdentifier

_CANONICAL_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DIGEST_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_KEBAB_DOT_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class _PrefixedUuidId(str):
    """Base for ``art_``/``act_``/``run_``-prefixed canonical-UUID identifiers."""

    prefix: ClassVar[str]

    def __new__(cls, value: str) -> _PrefixedUuidId:
        if not isinstance(value, str):
            raise InvalidIdentifier(f"{cls.__name__} must be a string, got {type(value)!r}")
        if not value.startswith(cls.prefix):
            raise InvalidIdentifier(f"{cls.__name__} must start with {cls.prefix!r}, got {value!r}")
        remainder = value[len(cls.prefix) :]
        if not _CANONICAL_UUID_RE.match(remainder):
            raise InvalidIdentifier(
                f"{cls.__name__} must be {cls.prefix!r} followed by a canonical "
                f"lowercase UUID, got {value!r}"
            )
        return super().__new__(cls, value)

    @classmethod
    def generate(cls) -> _PrefixedUuidId:
        return cls(f"{cls.prefix}{uuid.uuid4()}")

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        from pydantic_core import core_schema

        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class ArtifactId(_PrefixedUuidId):
    """Record identity for an artifact manifest: ``art_<uuid4>``."""

    prefix = "art_"


class ActivityId(_PrefixedUuidId):
    """Record identity for an activity manifest: ``act_<uuid4>``."""

    prefix = "act_"


class RunId(_PrefixedUuidId):
    """Record identity for a run manifest: ``run_<uuid4>``."""

    prefix = "run_"


class Digest(str):
    """A content digest, exactly ``sha256:<64 lowercase hex>``."""

    def __new__(cls, value: str) -> Digest:
        if not isinstance(value, str):
            raise InvalidIdentifier(f"Digest must be a string, got {type(value)!r}")
        algorithm, _, hex_part = value.partition(":")
        if algorithm != "sha256" or not hex_part or not _DIGEST_HEX_RE.match(hex_part):
            raise InvalidIdentifier(
                f"Digest must be 'sha256:' followed by 64 lowercase hex characters, got {value!r}"
            )
        return super().__new__(cls, value)

    @classmethod
    def of_bytes(cls, data: bytes) -> Digest:
        return cls(f"sha256:{hashlib.sha256(data).hexdigest()}")

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        from pydantic_core import core_schema

        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class ConfigurationSnapshotId(str):
    """Exactly the configuration digest rendered as ``cfg_sha256_<64 hex>``."""

    def __new__(cls, value: str) -> ConfigurationSnapshotId:
        if not isinstance(value, str):
            raise InvalidIdentifier(
                f"ConfigurationSnapshotId must be a string, got {type(value)!r}"
            )
        prefix = "cfg_sha256_"
        if not value.startswith(prefix):
            raise InvalidIdentifier(
                f"ConfigurationSnapshotId must start with {prefix!r}, got {value!r}"
            )
        hex_part = value[len(prefix) :]
        if not _DIGEST_HEX_RE.match(hex_part):
            raise InvalidIdentifier(
                f"ConfigurationSnapshotId must be {prefix!r} followed by 64 lowercase "
                f"hex characters, got {value!r}"
            )
        return super().__new__(cls, value)

    @classmethod
    def from_digest(cls, digest: Digest) -> ConfigurationSnapshotId:
        algorithm, _, hex_part = str(digest).partition(":")
        if algorithm != "sha256":
            raise InvalidIdentifier(f"Only sha256 digests are supported, got {digest!r}")
        return cls(f"cfg_sha256_{hex_part}")

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        from pydantic_core import core_schema

        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class _KebabDotId(str):
    """Base for lowercase kebab/dot catalog identifiers."""

    def __new__(cls, value: str) -> _KebabDotId:
        if not isinstance(value, str):
            raise InvalidIdentifier(f"{cls.__name__} must be a string, got {type(value)!r}")
        if not _KEBAB_DOT_RE.match(value):
            raise InvalidIdentifier(
                f"{cls.__name__} must match ^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$, got {value!r}"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        from pydantic_core import core_schema

        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class GridId(_KebabDotId):
    """Globally unique identifier for an immutable grid definition."""


class VariableId(_KebabDotId):
    """Identifier for a variable definition."""


class VerticalDefinitionId(_KebabDotId):
    """Identifier for a vertical coordinate definition."""
