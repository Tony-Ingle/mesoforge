"""Central operational disk reserve for heavy Guidance work, not forecast science."""

from __future__ import annotations

import math
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GIB = 1024**3
DEFAULT_MIN_FREE_BYTES = 20 * GIB
DEFAULT_WARN_FREE_BYTES = 30 * GIB


def require_runtime_capacity() -> None:
    """Recheck the hosted filesystem immediately before immutable publication.

    Lower-level in-memory/replay tools without a configured hosted root retain
    their existing contract. This never deletes anything or measures remote S3.
    """
    configured = os.environ.get("MESOFORGE_PROSPECTIVE_ROOT")
    if configured:
        report = DiskPolicy.from_environment().report(shutil.disk_usage(Path(configured)).free)
        if not report["heavy_work_admitted"]:
            raise OSError("Runtime disk reserve reached before persistence")


@dataclass(frozen=True)
class DiskPolicy:
    """Refuse below the floor; warn through the upper reserve, without deleting data."""

    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES
    warn_free_bytes: int = DEFAULT_WARN_FREE_BYTES

    def __post_init__(self) -> None:
        if (
            type(self.min_free_bytes) is not int
            or type(self.warn_free_bytes) is not int
            or not 0 <= self.min_free_bytes <= self.warn_free_bytes
        ):
            raise ValueError("Disk reserves must be integers with 0 <= minimum <= warning")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> DiskPolicy:
        environment = os.environ if environ is None else environ

        def setting(name: str, default: int) -> int:
            value = float(environment.get(name, str(default / GIB)))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative GiB value")
            return int(value * GIB)

        return cls(
            min_free_bytes=setting("MESOFORGE_GUIDANCE_MIN_FREE_GB", DEFAULT_MIN_FREE_BYTES),
            warn_free_bytes=setting("MESOFORGE_GUIDANCE_WARN_FREE_GB", DEFAULT_WARN_FREE_BYTES),
        )

    def report(self, free_bytes: int | None) -> dict[str, Any]:
        if free_bytes is not None and (type(free_bytes) is not int or free_bytes < 0):
            raise ValueError("Free disk must be a non-negative byte count or unavailable")
        status = (
            "unavailable"
            if free_bytes is None
            else "refused"
            if free_bytes < self.min_free_bytes
            else "warning"
            if free_bytes <= self.warn_free_bytes
            else "normal"
        )
        return {
            "free_bytes": free_bytes,
            "min_free_bytes": self.min_free_bytes,
            "warn_free_bytes": self.warn_free_bytes,
            "status": status,
            "heavy_work_admitted": status in {"normal", "warning"},
            "cleanup_action": "operator_review_only" if status != "normal" else "none",
        }
