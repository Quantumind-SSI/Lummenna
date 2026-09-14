"""Scoring for memory-retrieval evaluation.

Two decisions here are deliberate and worth stating, because they change what
the leaderboard measures:

**Abstention is scored separately, not folded into recall.** Memory benchmarks
include questions the transcript cannot answer. Recall is undefined when the
relevant set is empty, so averaging a made-up 0.0 or 1.0 into the recall column
silently rewards or punishes systems for a different capability. Abstention
gets its own column.

**Retrieval is scored, not generation.** Adapters return evidence ids. No LLM
judges anything, so results do not move when a judge model is upgraded, and a
framework cannot win by pairing with a stronger generator. This measures the
memory layer specifically, which is the claim under test.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

#: Resampling count for confidence intervals. Matches the existing Lummenna
#: benchmark suites so intervals are comparable across artifacts.
BOOTSTRAP_SAMPLES = 1000

#: Percentile bounds for a 95 % interval.
_CI_LOW_PCT = 2.5
_CI_HIGH_PCT = 97.5


@dataclass(frozen=True)
class MetricSummary:
    """A metric with its uncertainty.

    Attributes:
        mean: Point estimate.
        ci95_lo: Lower bound of the bootstrap 95 % interval.
        ci95_hi: Upper bound of the bootstrap 95 % interval.
        n: Number of observations the estimate is drawn from. Published so a
            reader can tell a 20-question result from a 500-question one.
    """

    mean: float
    ci95_lo: float
    ci95_hi: float
    n: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "mean": round(self.mean, 4),
            "ci95_lo": round(self.ci95_lo, 4),
            "ci95_hi": round(self.ci95_hi, 4),
            "n": self.n,
        }


def recall_at_k(retrieved: Sequence[str], relevant: frozenset[str], k: int) -> float:
    """Fraction of relevant items appearing in the top ``k`` retrieved.

    Args:
        retrieved: Ranked item ids, most relevant first.
        relevant: Ground-truth item ids. Must be non-empty.
        k: Cutoff. Must be positive.

    Returns:
        Value in [0, 1].

    Raises:
        ValueError: If ``relevant`` is empty (use :func:`abstention_correct`)
            or ``k`` is not positive.
    """
    if not relevant:
        raise ValueError("recall_at_k is undefined for an empty relevant set")
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    hits = len(set(retrieved[:k]) & relevant)
    return hits / len(relevant)


def reciprocal_rank(retrieved: Sequence[str], relevant: frozenset[str]) -> float:
    """Reciprocal of the rank of the first relevant item, else 0.0.

    Args:
        retrieved: Ranked item ids.
        relevant: Ground-truth item ids. Must be non-empty.

    Returns:
        1/rank of the first hit, or 0.0 if none of the relevant items appear.

    Raises:
        ValueError: If ``relevant`` is empty.

    Complexity:
        O(len(retrieved)); terminates at the first hit.
    """
    if not relevant:
        raise ValueError("reciprocal_rank is undefined for an empty relevant set")
    for index, item_id in enumerate(retrieved, start=1):
        if item_id in relevant:
            return 1.0 / index
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevant: frozenset[str], k: int) -> float:
    """Normalised discounted cumulative gain with binary relevance.

    Args:
        retrieved: Ranked item ids.
        relevant: Ground-truth item ids. Must be non-empty.
        k: Cutoff. Must be positive.

    Returns:
        Value in [0, 1]. 1.0 means every relevant item occupies the top ranks.

    Raises:
        ValueError: If ``relevant`` is empty or ``k`` is not positive.
    """
    if not relevant:
        raise ValueError("ndcg_at_k is undefined for an empty relevant set")
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")

    gain = 0.0
    for index, item_id in enumerate(retrieved[:k], start=1):
        if item_id in relevant:
            gain += 1.0 / math.log2(index + 1)

    # Ideal ordering: every relevant item packed into the highest ranks.
    ideal_hits = min(len(relevant), k)
    ideal = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return gain / ideal if ideal > 0 else 0.0


def abstention_correct(retrieved: Sequence[str]) -> float:
    """Score an unanswerable question.

    The correct behaviour when nothing in the transcript answers a question is
    to return nothing. A system that always returns its top-k regardless scores
    0.0 here, which is the point: confident retrieval of irrelevant evidence is
    the failure mode that produces confident hallucinated answers downstream.

    Args:
        retrieved: What the adapter returned.

    Returns:
        1.0 if nothing was returned, else 0.0.
    """
    return 1.0 if not retrieved else 0.0


def bootstrap_summary(
    values: Sequence[float],
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = 42,
) -> MetricSummary | None:
    """Summarise observations with a bootstrap 95 % confidence interval.

    Args:
        values: Per-question scores.
        samples: Resampling count. Must be positive.
        seed: Seed for reproducibility. The same inputs always yield the same
            interval, so an artifact is byte-reproducible.

    Returns:
        A :class:`MetricSummary`, or ``None`` when ``values`` is empty. Empty
        input means the metric was not measured; it is never reported as 0.0.

    Complexity:
        O(samples * len(values)), vectorised through numpy.
    """
    if not values:
        return None
    if samples <= 0:
        raise ValueError(f"samples must be positive, got {samples}")

    observations = np.asarray(values, dtype=np.float64)
    mean = float(observations.mean())

    if observations.size == 1:
        # A single observation has no spread to resample; report it as a point
        # estimate rather than a spuriously tight interval.
        return MetricSummary(mean=mean, ci95_lo=mean, ci95_hi=mean, n=1)

    rng = np.random.default_rng(seed)
    indices = rng.integers(0, observations.size, size=(samples, observations.size))
    resampled_means = observations[indices].mean(axis=1)
    return MetricSummary(
        mean=mean,
        ci95_lo=float(np.percentile(resampled_means, _CI_LOW_PCT)),
        ci95_hi=float(np.percentile(resampled_means, _CI_HIGH_PCT)),
        n=int(observations.size),
    )
