#!/usr/bin/env python3
"""Start a local ``pgserver`` PostgreSQL instance and hold it open.

Operational convenience only: this sandbox has no Docker group
membership, so ``make services-up`` cannot be used. ``tests/conftest.py``
already uses the very same ``pgserver`` distribution (a real PostgreSQL
16+ binary build, not SQLite and not a mock) for its session-scoped
fixture; this script exposes one long-lived instance so an operational
run of ``scripts/run_phase2_live.py`` can point ``MESOFORGE_DATABASE_DSN``
at it.

Writes the resolved psycopg3 DSN to the path given by ``--dsn-file`` and
then blocks until interrupted.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path
from typing import Any, cast

import pgserver


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--dsn-file", type=Path, required=True)
    args = parser.parse_args(argv)

    args.data_dir.mkdir(parents=True, exist_ok=True)
    # ``pgserver`` ships no py.typed marker, so mypy cannot see its
    # public entrypoint; this is the same package tests/conftest.py uses.
    get_server = cast(Any, getattr(pgserver, "get_server"))  # noqa: B009
    server = get_server(args.data_dir, cleanup_mode=None)
    dsn = str(server.get_uri()).replace("postgresql://", "postgresql+psycopg://", 1)
    args.dsn_file.write_text(dsn + "\n", encoding="utf-8")
    print(f"[pgserver] ready: {dsn}", flush=True)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        print("[pgserver] stopping", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - operational entrypoint
    sys.exit(main())
