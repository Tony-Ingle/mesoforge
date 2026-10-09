"""One deployment lock excludes different runtime roots and independent processes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from mesoforge.application import worker_lock
from mesoforge.application.worker_lock import single_writer, worker_lock_root


def test_different_roots_share_configured_cross_process_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment = tmp_path / "deployment"
    first, second = deployment / "first", deployment / "second"
    monkeypatch.setenv("MESOFORGE_WORKER_LOCK_ROOT", str(deployment))
    script = (
        "import sys\nfrom pathlib import Path\n"
        "from mesoforge.application.worker_lock import single_writer, worker_lock_root\n"
        "with single_writer(worker_lock_root(Path(sys.argv[1]))) as owned:\n"
        "    print('owned' if owned else 'busy')\n"
    )

    def attempt() -> str:
        child = subprocess.run(
            [sys.executable, "-c", script, str(second)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return child.stdout.strip()

    with single_writer(worker_lock_root(first)) as owned:
        assert owned
        assert attempt() == "busy"
    assert attempt() == "owned"
    # Host daily-day receipt locks use the supplied root directly, intentionally
    # unaffected by the worker-only deployment setting.
    with single_writer(worker_lock_root(first)) as owned, single_writer(tmp_path / "day") as day:
        assert owned and day


def test_default_preserves_runtime_root_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MESOFORGE_WORKER_LOCK_ROOT", raising=False)
    assert worker_lock_root(tmp_path) == tmp_path


@pytest.mark.parametrize("value", ["", "relative", str(worker_lock._ROOT), str(Path.cwd().anchor)])
def test_configured_lock_rejects_relative_checkout_and_filesystem_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("MESOFORGE_WORKER_LOCK_ROOT", value)
    with pytest.raises(ValueError, match="absolute directory outside"):
        worker_lock_root(tmp_path)
