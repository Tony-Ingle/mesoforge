"""Unit tests for mesoforge.verification.qpf_pop_metrics (plan Section
6.2, Task 12): hand-calculated contingency counts/metrics, Brier
score, decile bins, decomposition, ROC AUC.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mesoforge.verification.qpf_pop_metrics import (
    MetricResult,
    compute_brier_decomposition,
    compute_brier_score,
    compute_contingency_counts,
    compute_contingency_metrics,
    compute_decile_reliability_bins,
    compute_roc_auc,
    finite_or_none,
)


class TestComputeContingencyCounts:
    @pytest.mark.parametrize(
        "pairs", [[(-0.1, 0.0)], [(0.0, -0.1)], [(float("nan"), 0.0)], [(0.0, float("inf"))]]
    )
    def test_rejects_invalid_qpf(self, pairs: list[tuple[float, float]]) -> None:
        with pytest.raises(ValueError):
            compute_contingency_counts(pairs, threshold_kg_m2=0.254)

    @pytest.mark.parametrize("threshold", [-0.1, float("nan"), float("inf")])
    def test_rejects_invalid_threshold(self, threshold: float) -> None:
        with pytest.raises(ValueError):
            compute_contingency_counts([], threshold_kg_m2=threshold)

    def test_hand_calculated_counts(self) -> None:
        pairs = [
            (1.0, 1.0),  # hit
            (0.0, 1.0),  # miss
            (1.0, 0.0),  # false alarm
            (0.0, 0.0),  # correct negative
        ]
        counts = compute_contingency_counts(pairs, threshold_kg_m2=0.254)
        assert counts.hits == 1
        assert counts.misses == 1
        assert counts.false_alarms == 1
        assert counts.correct_negatives == 1

    def test_threshold_is_inclusive(self) -> None:
        pairs = [(0.254, 0.254)]
        counts = compute_contingency_counts(pairs, threshold_kg_m2=0.254)
        assert counts.hits == 1


class TestComputeContingencyMetrics:
    def test_false_alarms_without_observed_events_still_compute_ets(self) -> None:
        counts = compute_contingency_counts(
            [(1.0, 0.0)] * 2 + [(0.0, 0.0)] * 8, threshold_kg_m2=0.254
        )
        metrics = compute_contingency_metrics(counts)
        assert metrics.ets == pytest.approx(0.0)
        assert metrics.ets_null_reason is None

    def test_hand_calculated_metrics(self) -> None:
        # H=8, M=2, F=3, CN=7, N=20
        counts = compute_contingency_counts(
            [(1.0, 1.0)] * 8 + [(0.0, 1.0)] * 2 + [(1.0, 0.0)] * 3 + [(0.0, 0.0)] * 7,
            threshold_kg_m2=0.5,
        )
        metrics = compute_contingency_metrics(counts)
        assert metrics.csi == pytest.approx(8 / 13)
        assert metrics.pod == pytest.approx(8 / 10)
        assert metrics.far == pytest.approx(3 / 11)
        assert metrics.frequency_bias == pytest.approx(11 / 10)
        # Hr = (H+F)*(H+M)/N = 11*10/20 = 5.5
        # ETS = (H - Hr) / (H+M+F-Hr) = (8-5.5)/(13-5.5) = 2.5/7.5
        assert metrics.ets == pytest.approx(2.5 / 7.5)

    def test_zero_denominator_csi(self) -> None:
        counts = compute_contingency_counts([], threshold_kg_m2=0.254)
        metrics = compute_contingency_metrics(counts)
        assert metrics.csi is None
        assert metrics.csi_null_reason == "zero_denominator:csi"

    def test_counts_always_emitted_even_when_metrics_null(self) -> None:
        counts = compute_contingency_counts([], threshold_kg_m2=0.254)
        metrics = compute_contingency_metrics(counts)
        assert metrics.counts.total == 0

    def test_no_events_yields_null_pod_far(self) -> None:
        counts = compute_contingency_counts([(0.0, 0.0), (0.0, 0.0)], threshold_kg_m2=0.254)
        metrics = compute_contingency_metrics(counts)
        assert metrics.pod is None
        assert metrics.pod_null_reason == "zero_denominator:pod"


class TestComputeBrierScore:
    @pytest.mark.parametrize(
        "pairs",
        [[(-0.1, 0.0)], [(1.1, 1.0)], [(0.5, 0.2)], [(float("nan"), 0.0)], [(0.5, float("inf"))]],
    )
    def test_rejects_invalid_probability_or_binary_outcome(
        self, pairs: list[tuple[float, float]]
    ) -> None:
        with pytest.raises(ValueError):
            compute_brier_score(pairs)

    def test_hand_calculated_score(self) -> None:
        pairs = [(0.8, 1.0), (0.2, 0.0), (0.5, 1.0)]
        score = compute_brier_score(pairs)
        expected = ((0.8 - 1.0) ** 2 + (0.2 - 0.0) ** 2 + (0.5 - 1.0) ** 2) / 3
        assert score == pytest.approx(expected)

    def test_perfect_forecast_zero_score(self) -> None:
        pairs = [(1.0, 1.0), (0.0, 0.0)]
        assert compute_brier_score(pairs) == pytest.approx(0.0)

    def test_empty_is_none(self) -> None:
        assert compute_brier_score([]) is None

    def test_bounded_zero_to_one(self) -> None:
        pairs = [(0.9, 0.0), (0.1, 1.0)]
        score = compute_brier_score(pairs)
        assert score is not None
        assert 0.0 <= score <= 1.0


class TestComputeDecileReliabilityBins:
    @pytest.mark.parametrize("probability", [-0.01, 1.01, float("nan"), float("inf")])
    def test_rejects_invalid_probabilities(self, probability: float) -> None:
        with pytest.raises(ValueError):
            compute_decile_reliability_bins([(probability, 0.0)])

    def test_rejects_nonbinary_outcome(self) -> None:
        with pytest.raises(ValueError):
            compute_decile_reliability_bins([(0.5, 0.5)])

    def test_probability_one_belongs_to_final_bin(self) -> None:
        bins = compute_decile_reliability_bins([(1.0, 1.0)])
        assert bins[9].count == 1
        assert bins[8].count == 0

    def test_probability_zero_belongs_to_first_bin(self) -> None:
        bins = compute_decile_reliability_bins([(0.0, 0.0)])
        assert bins[0].count == 1

    def test_empty_bins_have_null_pbar_obar(self) -> None:
        bins = compute_decile_reliability_bins([(0.05, 1.0)])
        assert bins[0].count == 1
        assert bins[5].count == 0
        assert bins[5].pbar is None
        assert bins[5].obar is None

    def test_pbar_is_mean_of_actual_probabilities_not_bin_center(self) -> None:
        bins = compute_decile_reliability_bins([(0.31, 1.0), (0.35, 0.0)])
        # both land in bin index 3: [0.3, 0.4)
        assert bins[3].count == 2
        assert bins[3].pbar == pytest.approx((0.31 + 0.35) / 2)
        assert bins[3].pbar != pytest.approx(0.35)  # not the bin center

    def test_ten_bins_total(self) -> None:
        assert len(compute_decile_reliability_bins([])) == 10


class TestComputeBrierDecomposition:
    def test_rejects_total_sample_inconsistent_with_bins(self) -> None:
        bins = compute_decile_reliability_bins([(0.8, 1.0)] * 60 + [(0.2, 0.0)] * 60)
        with pytest.raises(ValueError, match="total_sample"):
            compute_brier_decomposition(bins, total_sample=121)

    def test_none_below_sample_threshold(self) -> None:
        bins = compute_decile_reliability_bins([(0.5, 1.0)] * 50)
        assert compute_brier_decomposition(bins, total_sample=50).value is None

    def test_none_when_all_one_class(self) -> None:
        bins = compute_decile_reliability_bins([(0.9, 1.0)] * 150)
        assert compute_brier_decomposition(bins, total_sample=150).value is None

    def test_computes_with_sufficient_mixed_sample(self) -> None:
        pairs = [(0.8, 1.0)] * 60 + [(0.2, 0.0)] * 60
        bins = compute_decile_reliability_bins(pairs)
        result = compute_brier_decomposition(bins, total_sample=120)
        decomposition = result.value
        assert decomposition is not None
        assert decomposition.reliability >= 0.0
        assert decomposition.resolution >= 0.0
        assert 0.0 <= decomposition.uncertainty <= 0.25


class TestComputeRocAuc:
    def test_none_below_sample_threshold(self) -> None:
        pairs = [(0.8, 1.0)] * 50 + [(0.2, 0.0)] * 49
        assert compute_roc_auc(pairs).value is None

    def test_none_when_single_class(self) -> None:
        pairs = [(0.8, 1.0)] * 150
        assert compute_roc_auc(pairs).value is None

    def test_perfect_separation_yields_one(self) -> None:
        pairs = [(0.9, 1.0)] * 60 + [(0.1, 0.0)] * 60
        auc = compute_roc_auc(pairs).value
        assert auc is not None
        assert auc == pytest.approx(1.0)

    def test_random_scores_near_half(self) -> None:
        pairs = [(0.5, 1.0)] * 60 + [(0.5, 0.0)] * 60
        auc = compute_roc_auc(pairs).value
        assert auc is not None
        assert auc == pytest.approx(0.5)


class TestFiniteOrNone:
    def test_passes_through_finite(self) -> None:
        assert finite_or_none(1.5) == 1.5

    def test_none_stays_none(self) -> None:
        assert finite_or_none(None) is None

    def test_nan_becomes_none(self) -> None:
        assert finite_or_none(float("nan")) is None

    def test_infinity_becomes_none(self) -> None:
        assert finite_or_none(float("inf")) is None


class TestExplicitNullReasons:
    def test_auc_reports_insufficient_sample_and_single_class(self) -> None:
        assert compute_roc_auc([(0.5, 0.0)]).null_reason == "insufficient_sample"
        assert compute_roc_auc([(0.5, 0.0)] * 100).null_reason == "single_class"

    def test_decomposition_reports_preconditions(self) -> None:
        bins = compute_decile_reliability_bins([(0.5, 0.0)])
        result = compute_brier_decomposition(bins, total_sample=1)
        assert result.null_reason == "insufficient_sample"
        bins = compute_decile_reliability_bins([(0.5, 0.0)] * 100)
        assert compute_brier_decomposition(bins, total_sample=100).null_reason == "single_class"

    def test_metric_result_is_frozen_and_strict(self) -> None:
        result = MetricResult(value=None, null_reason="insufficient_sample")
        with pytest.raises(FrozenInstanceError):
            result.value = 1.0  # type: ignore[misc]
