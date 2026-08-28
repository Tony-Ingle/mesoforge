"""Observation port protocols (plan Section 2.4, Task 8).

Mirrors ``guidance.interfaces`` for the AviationWeather.gov acquisition
path: pure ``Protocol`` shapes, no network import.
"""

from __future__ import annotations

from mesoforge.guidance.interfaces import Clock, HttpResponse, HttpTransport, Sleeper

__all__ = ["Clock", "HttpResponse", "HttpTransport", "Sleeper"]
