"""Offline proof that live canaries cannot construct network transport by default."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.live.support import require_live_cycle


def test_default_guard_skips_before_transport_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESOFORGE_LIVE_TESTS", raising=False)
    monkeypatch.delenv("MESOFORGE_LIVE_NBM_CYCLE", raising=False)
    constructed = False

    def transport_factory() -> object:
        nonlocal constructed
        constructed = True
        raise AssertionError("network transport must not be constructed")

    with pytest.raises(pytest.skip.Exception, match="MESOFORGE_LIVE_TESTS=1"):
        require_live_cycle("NBM", transport_factory)

    assert constructed is False


@pytest.mark.parametrize("model", ["HRRR", "NBM", "GFS"])
def test_model_cycle_is_required_before_transport_construction(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    monkeypatch.setenv("MESOFORGE_LIVE_TESTS", "1")
    monkeypatch.delenv(f"MESOFORGE_LIVE_{model}_CYCLE", raising=False)
    constructed = False

    def transport_factory() -> object:
        nonlocal constructed
        constructed = True
        return object()

    factory: Callable[[], object] = transport_factory
    with pytest.raises(pytest.skip.Exception, match=f"MESOFORGE_LIVE_{model}_CYCLE"):
        require_live_cycle(model, factory)

    assert constructed is False
