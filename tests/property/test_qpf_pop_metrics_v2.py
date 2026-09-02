from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mesoforge.verification.qpf_pop_metrics import (
    compute_brier_score,
    compute_contingency_counts,
    compute_contingency_metrics,
    compute_roc_auc,
)

pytestmark = pytest.mark.scientific


@given(
    st.lists(
        st.tuples(
            st.floats(0, 1, allow_nan=False, allow_infinity=False), st.integers(0, 1).map(float)
        ),
        min_size=1,
        max_size=200,
    )
)
def test_brier_and_auc_are_bounded(pairs: list[tuple[float, float]]) -> None:
    brier = compute_brier_score(pairs)
    assert brier is not None and 0 <= brier <= 1
    auc = compute_roc_auc(pairs)
    assert auc.value is None or 0 <= auc.value <= 1


@given(
    st.lists(
        st.tuples(
            st.floats(0, 10, allow_nan=False, allow_infinity=False),
            st.floats(0, 10, allow_nan=False, allow_infinity=False),
        ),
        max_size=100,
    )
)
def test_contingency_counts_partition_sample(pairs: list[tuple[float, float]]) -> None:
    counts = compute_contingency_counts(pairs, threshold_kg_m2=2.54)
    assert counts.total == len(pairs)
    metrics = compute_contingency_metrics(counts)
    for value in (metrics.csi, metrics.pod, metrics.far, metrics.ets):
        assert value is None or -1 <= value <= 1
