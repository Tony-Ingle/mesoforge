"""Phase 2 QPF/PoP verification metrics (plan Section 6.2, Task 12):
event contingency counts (CSI/POD/FAR/frequency bias/ETS), PoP Brier
score with fixed decile reliability bins, and the Brier
reliability/resolution/uncertainty decomposition.

Pure float64 computation over already-matched (forecast, observed)
pairs -- no storage/network import.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_DECILE_BIN_EDGES: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)


@dataclass(frozen=True, slots=True)
class MetricResult[T]:
    value: T | None
    null_reason: str | None

    def __post_init__(self) -> None:
        if (self.value is None) == (self.null_reason is None):
            raise ValueError("exactly one of value and null_reason must be present")


def _validate_qpf_pairs(pairs: list[tuple[float, float]]) -> None:
    for forecast, observed in pairs:
        if not math.isfinite(forecast) or not math.isfinite(observed):
            raise ValueError("QPF inputs must be finite")
        if forecast < 0.0 or observed < 0.0:
            raise ValueError("QPF inputs must be nonnegative")


def _validate_probability_pairs(pairs: list[tuple[float, float]]) -> None:
    for probability, outcome in pairs:
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError("forecast probability must be finite and in [0, 1]")
        if not math.isfinite(outcome) or outcome not in (0.0, 1.0):
            raise ValueError("observed outcome must be exactly binary (0 or 1)")


@dataclass(frozen=True, slots=True)
class ContingencyCounts:
    hits: int
    misses: int
    false_alarms: int
    correct_negatives: int

    @property
    def total(self) -> int:
        return self.hits + self.misses + self.false_alarms + self.correct_negatives


def compute_contingency_counts(
    pairs: list[tuple[float, float]], *, threshold_kg_m2: float
) -> ContingencyCounts:
    """Section 6.2: observed and forecast events both use ``>=
    threshold``. Hits: forecast yes/observed yes. Misses: forecast no/
    observed yes. False alarms: forecast yes/observed no. Correct
    negatives: both no."""
    if not math.isfinite(threshold_kg_m2) or threshold_kg_m2 < 0.0:
        raise ValueError("QPF threshold must be finite and nonnegative")
    _validate_qpf_pairs(pairs)
    hits = misses = false_alarms = correct_negatives = 0
    for forecast, observed in pairs:
        forecast_yes = forecast >= threshold_kg_m2
        observed_yes = observed >= threshold_kg_m2
        if forecast_yes and observed_yes:
            hits += 1
        elif not forecast_yes and observed_yes:
            misses += 1
        elif forecast_yes and not observed_yes:
            false_alarms += 1
        else:
            correct_negatives += 1
    return ContingencyCounts(
        hits=hits, misses=misses, false_alarms=false_alarms, correct_negatives=correct_negatives
    )


@dataclass(frozen=True, slots=True)
class ContingencyMetrics:
    counts: ContingencyCounts
    csi: float | None
    csi_null_reason: str | None
    pod: float | None
    pod_null_reason: str | None
    far: float | None
    far_null_reason: str | None
    frequency_bias: float | None
    frequency_bias_null_reason: str | None
    ets: float | None
    ets_null_reason: str | None


def compute_contingency_metrics(counts: ContingencyCounts) -> ContingencyMetrics:
    """Section 6.2: ``CSI=H/(H+M+F)``, ``POD=H/(H+M)``,
    ``FAR=F/(H+F)``, frequency bias ``(H+F)/(H+M)``, random hits
    ``Hr=(H+F)*(H+M)/N``, ``ETS=(H-Hr)/(H+M+F-Hr)``. A zero
    denominator yields ``null`` with exact reason
    ``zero_denominator:<metric>``; counts are always emitted."""
    h, m, f = counts.hits, counts.misses, counts.false_alarms
    n = counts.total

    csi_denom = h + m + f
    csi = h / csi_denom if csi_denom > 0 else None
    csi_reason = None if csi_denom > 0 else "zero_denominator:csi"

    pod_denom = h + m
    pod = h / pod_denom if pod_denom > 0 else None
    pod_reason = None if pod_denom > 0 else "zero_denominator:pod"

    far_denom = h + f
    far = f / far_denom if far_denom > 0 else None
    far_reason = None if far_denom > 0 else "zero_denominator:far"

    freq_bias = (h + f) / pod_denom if pod_denom > 0 else None
    freq_bias_reason = None if pod_denom > 0 else "zero_denominator:frequency_bias"

    if n > 0:
        hr = (h + f) * (h + m) / n
        ets_denom = h + m + f - hr
        if ets_denom != 0:
            ets = (h - hr) / ets_denom
            ets_reason = None
        else:
            ets = None
            ets_reason = "zero_denominator:ets"
    else:
        ets = None
        ets_reason = "zero_denominator:ets"

    return ContingencyMetrics(
        counts=counts,
        csi=csi,
        csi_null_reason=csi_reason,
        pod=pod,
        pod_null_reason=pod_reason,
        far=far,
        far_null_reason=far_reason,
        frequency_bias=freq_bias,
        frequency_bias_null_reason=freq_bias_reason,
        ets=ets,
        ets_null_reason=ets_reason,
    )


def compute_brier_score(pairs: list[tuple[float, float]]) -> float | None:
    """Section 6.2: ``Brier = mean((p - o)^2)`` where ``o`` is the
    binary event outcome (0 or 1) and ``p`` the forecast probability.
    ``None`` for zero samples."""
    _validate_probability_pairs(pairs)
    if not pairs:
        return None
    squared_errors = [(p - o) ** 2 for p, o in pairs]
    return sum(squared_errors) / len(squared_errors)


@dataclass(frozen=True, slots=True)
class DecileBin:
    lower: float
    upper: float
    count: int
    pbar: float | None
    obar: float | None


def compute_decile_reliability_bins(pairs: list[tuple[float, float]]) -> tuple[DecileBin, ...]:
    """Section 6.2: fixed decile bins ``[0,.1), ..., [.8,.9), [.9,1.0]``
    -- probability exactly 1 belongs to the final bin. Empty bins have
    count 0 and ``pbar=None``/``obar=None`` (serialized JSON null).
    ``pbar`` is the mean of actual forecast probabilities in the bin
    (not the bin center); ``obar`` is the bin's observed event
    fraction."""
    _validate_probability_pairs(pairs)
    bins: list[list[tuple[float, float]]] = [[] for _ in range(10)]
    for p, o in pairs:
        if p == 1.0:
            index = 9
        else:
            index = min(int(p * 10), 9)
        bins[index].append((p, o))

    result: list[DecileBin] = []
    for i in range(10):
        lower = _DECILE_BIN_EDGES[i]
        upper = _DECILE_BIN_EDGES[i + 1]
        members = bins[i]
        if not members:
            result.append(DecileBin(lower=lower, upper=upper, count=0, pbar=None, obar=None))
            continue
        pbar = sum(p for p, _ in members) / len(members)
        obar = sum(o for _, o in members) / len(members)
        result.append(DecileBin(lower=lower, upper=upper, count=len(members), pbar=pbar, obar=obar))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class BrierDecomposition:
    reliability: float
    resolution: float
    uncertainty: float


def compute_brier_decomposition(
    bins: tuple[DecileBin, ...], *, total_sample: int
) -> MetricResult[BrierDecomposition]:
    """Section 6.2: ``REL=sum(nk/N*(pbar_k-obar_k)^2)``,
    ``RES=sum(nk/N*(obar_k-obar)^2)``, ``UNC=obar*(1-obar)``. Only
    computed when sample >=100 and both event classes occur; otherwise
    ``None`` (caller serializes null plus reason)."""
    if len(bins) != 10 or total_sample != sum(bin_.count for bin_ in bins):
        raise ValueError("total_sample must equal the sum of the ten bin counts")
    if total_sample < 100:
        return MetricResult(value=None, null_reason="insufficient_sample")

    total_events = sum(bin_.count * (bin_.obar or 0.0) for bin_ in bins)
    obar = total_events / total_sample
    if obar <= 0.0 or obar >= 1.0:
        return MetricResult(value=None, null_reason="single_class")

    reliability = sum(
        (bin_.count / total_sample) * ((bin_.pbar or 0.0) - (bin_.obar or 0.0)) ** 2
        for bin_ in bins
        if bin_.count > 0
    )
    resolution = sum(
        (bin_.count / total_sample) * ((bin_.obar or 0.0) - obar) ** 2
        for bin_ in bins
        if bin_.count > 0
    )
    uncertainty = obar * (1.0 - obar)

    return MetricResult(
        value=BrierDecomposition(
            reliability=reliability, resolution=resolution, uncertainty=uncertainty
        ),
        null_reason=None,
    )


def compute_roc_auc(pairs: list[tuple[float, float]]) -> MetricResult[float]:
    """Section 6.2: ROC AUC only when sample >=100 and both classes
    occur, using the Mann-Whitney definition
    ``P(score_event > score_nonevent) + 0.5*P(tie)``."""
    _validate_probability_pairs(pairs)
    if len(pairs) < 100:
        return MetricResult(value=None, null_reason="insufficient_sample")
    event_scores = [p for p, o in pairs if o == 1.0]
    nonevent_scores = [p for p, o in pairs if o == 0.0]
    if not event_scores or not nonevent_scores:
        return MetricResult(value=None, null_reason="single_class")

    wins = 0.0
    for event_score in event_scores:
        for nonevent_score in nonevent_scores:
            if event_score > nonevent_score:
                wins += 1.0
            elif event_score == nonevent_score:
                wins += 0.5
    return MetricResult(value=wins / (len(event_scores) * len(nonevent_scores)), null_reason=None)


def finite_or_none(value: float | None) -> float | None:
    """Guard against NaN/Infinity ever reaching JSON serialization
    (Section 6.2: 'Never emit NaN/Infinity')."""
    if value is None:
        return None
    if not math.isfinite(value):
        return None
    return value
