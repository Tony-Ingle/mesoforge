"""HRRR guidance port protocols (plan Section 2.2/2.3, Task 4).

Pure ``Protocol`` declarations only -- no ``requests``/network import.
Concrete adapters live in ``guidance/sources/hrrr.py`` (network
infrastructure permitted there); ``guidance/acquisition.py`` depends on
these shapes only, so unit tests can inject a scripted fake transport.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class HttpResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]
    content: bytes


@runtime_checkable
class HttpTransport(Protocol):
    """Minimal HTTP transport shape used by HRRR acquisition. Only
    ``get`` and ``head`` are needed -- Phase 1 never issues POST/PUT to
    a provider."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> HttpResponse: ...

    def head(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> HttpResponse: ...


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...
