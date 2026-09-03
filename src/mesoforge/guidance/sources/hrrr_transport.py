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
from dataclasses import dataclass

from mesoforge.guidance.interfaces import HttpResponse


@dataclass(slots=True)
class RequestsHttpResponse:
    """A concrete, statically-typed ``HttpResponse``.

    ``requests.Response`` satisfies the protocol structurally at runtime,
    but its ``headers``/``content`` are not typed precisely enough for
    the protocol to be checkable statically. Normalizing into this small
    record at the transport boundary means every downstream consumer --
    retry engine, range-integrity validation, availability parsing --
    works against exactly the declared shape.

    Not frozen: ``guidance.interfaces.HttpResponse`` declares plain
    (settable) protocol members, and a frozen dataclass exposes them as
    read-only, which does not satisfy the protocol. Nothing mutates an
    instance; construction is still the only write.
    """

    status_code: int
    headers: Mapping[str, str]
    content: bytes


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
    ) -> HttpResponse:
        response = self._session.get(url, headers=dict(headers or {}), timeout=timeout)
        return RequestsHttpResponse(
            status_code=int(response.status_code),
            headers=dict(response.headers),
            content=bytes(response.content),
        )

    def head(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> HttpResponse:
        response = self._session.head(url, headers=dict(headers or {}), timeout=timeout)
        return RequestsHttpResponse(
            status_code=int(response.status_code),
            headers=dict(response.headers),
            content=bytes(response.content),
        )
