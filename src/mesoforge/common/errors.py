"""Typed error hierarchy shared across MesoForge domain and storage packages.

All errors here are constructed and raised by MesoForge code; they never
wrap or leak infrastructure-specific exception types (e.g. psycopg or
boto3 exceptions) across the storage-interface boundary.
"""

from __future__ import annotations

# Exception names below intentionally omit the "Error" suffix to match the
# exact names mandated by the Phase 0 plan (Section 4.9): NotFound,
# Conflict, LineageViolation, IntegrityError (mixed), InvalidIdentifier.
# See pyproject.toml [tool.ruff.lint.per-file-ignores] for the N818 waiver.


class MesoForgeError(Exception):
    """Base class for all MesoForge domain and storage errors."""


class InvalidIdentifier(MesoForgeError):
    """Raised when a typed identifier string fails validation."""


class NotFound(MesoForgeError):
    """Raised when a requested record does not exist."""


class Conflict(MesoForgeError):
    """Raised when an operation would violate an identity or immutability
    invariant (e.g. reusing a grid ID with a changed definition)."""


class LineageViolation(MesoForgeError):
    """Raised when a proposed activity input/output would create an
    invalid provenance graph (e.g. a cycle, or a missing input)."""


class IntegrityError(MesoForgeError):
    """Raised when retrieved bytes fail checksum verification against
    their expected content digest."""
