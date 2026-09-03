"""Concrete real-world ``Clock``/``Sleeper`` implementations.

``guidance.interfaces`` declares these as protocols so unit and
acceptance tests can inject deterministic fakes. Production wiring needs
one real implementation of each; they live here rather than in a test
support module so that an operational run composes only production code.

Deliberately trivial: any policy (backoff schedules, deadlines, cutoffs)
belongs to the caller, not to the clock.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime


class SystemClock:
    """The real wall clock, always returning an aware UTC instant.

    Every MesoForge timestamp contract is UTC-aware, so this never
    returns a naive datetime and never uses local time.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)


class SystemSleeper:
    """Real ``time.sleep``, used for provider retry/backoff pacing."""

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"sleep seconds must be nonnegative, got {seconds!r}")
        time.sleep(seconds)
