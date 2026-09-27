"""The code revision that identifies every artifact and stage MesoForge produces.

A development checkout reads it from git. A deployed image contains no repository, so
the exact revision is baked in at build time as ``MESOFORGE_CODE_REVISION``. Either
source must be a 40-character lowercase hex commit; anything else fails closed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from mesoforge.common.identifiers import validate_code_revision

CODE_REVISION_VARIABLE = "MESOFORGE_CODE_REVISION"


def current_code_revision(root: Path) -> str:
    """The configured image revision, else ``git rev-parse HEAD`` of ``root``."""
    configured = os.environ.get(CODE_REVISION_VARIABLE, "").strip()
    if configured:
        return validate_code_revision(configured)
    revision = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return validate_code_revision(revision)
