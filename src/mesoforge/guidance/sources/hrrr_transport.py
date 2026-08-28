"""Real-network ``requests``-backed HTTP transport for HRRR acquisition
(plan Section 2.2/2.3).

Isolated in its own module (separate from ``guidance.sources.hrrr``'s
pure URL/index functions) so ``guidance.acquisition`` -- which imports
only the pure functions -- never transitively imports ``requests``,
keeping the default-CI-exercised acquisition orchestration path
network-import-free even though it composes with a real transport in
production wiring. Used only for production wiring and opt-in live
smoke tests (``tests/live/``); never imported by default-CI unit/
acceptance tests.
"""

from __future__ import annotations

from collections.abc import Mapping


class RequestsHrrrHttpTransport:
    """Concrete, real-network ``guidance.interfaces.HttpTransport``."""

    def __init__(self) -> None:
        import requests

        self._session = requests.Session()

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> object:
        return self._session.get(url, headers=dict(headers or {}), timeout=timeout)

    def head(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> object:
        return self._session.head(url, headers=dict(headers or {}), timeout=timeout)
