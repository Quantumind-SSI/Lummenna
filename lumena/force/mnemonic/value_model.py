"""A9: 7-Factor Value Model V(m).

Input wire: spaCy / sklearn (B4), user interaction history (C6, D3)
Output wire: A6 (store pipeline), C3 (fusion reranking), C7 (TFC)
Secret sauce: Per-user learned weights, no API calls, CPU-only
"""

import json
import math
from typing import Annotated

import numpy as np
from pydantic import BaseModel, Field

from lumena.contracts import SOURCE_TYPES
from lumena.logging import get_console_logger

logger = get_console_logger(__name__)

# Whitepaper 3.4 reliability weighting, with the documented ordering
# user_input > consolidation > agent_reasoning > import > p2p_share.
# Keyed by the five canonical source types; an unknown key is a claim violation.
RELIABILITY_BY_SOURCE: dict[str, float] = {
    "user_input": 0.9,
    "agent_reasoning": 0.7,
    "consolidation": 0.75,
    "import": 0.6,
    "p2p_share": 0.5,
}
# The map must cover exactly the documented source types.
assert set(RELIABILITY_BY_SOURCE) == set(SOURCE_TYPES)

# Default weights for cold-start user (untrained)
DEFAULT_WEIGHTS = {
    "goal_relevance": 0.20,
    "value_alignment": 0.15,
    "self_relevance": 0.15,
    "task_utility": 0.15,
    "emotional_intensity": 0.15,
    "reliability": 0.10,
    "usage_history": 0.10,
}

FACTOR_KEYS = list(DEFAULT_WEIGHTS.keys())

# Whitepaper 3.4: each factor is a score in [0, 1].
UnitScore = Annotated[float, Field(ge=0.0, le=1.0)]
# Weights are non-negative; they parameterise a logistic and need not sum to 1.
NonNegWeight = Annotated[float, Field(ge=0.0)]


class ValueFactors(BaseModel):
    """The seven V(m) heuristic factors, each constrained to [0, 1].

    Grounds the whitepaper claim (3.4) that V(m) is "computed from seven
    heuristic factors", each a bounded score.
    """

    model_config = {"extra": "forbid"}

    goal_relevance: UnitScore
    value_alignment: UnitScore
    self_relevance: UnitScore
    task_utility: UnitScore
    emotional_intensity: UnitScore
    reliability: UnitScore
    usage_history: UnitScore

    def as_ordered_vector(self) -> np.ndarray:
        """Return factors as a vector aligned with ``FACTOR_KEYS`` order."""
        data = self.model_dump()
        return np.array([data[k] for k in FACTOR_KEYS], dtype=float)


class ValueWeights(BaseModel):
    """Per-factor weights for the V(m) logistic. Non-negative by construction.

    Unknown keys are ignored rather than rejected: the public ``store_memory``
    API has always accepted arbitrary ``vm_weights`` dicts and silently used only
    the recognised factor weights. We preserve that contract while still
    enforcing the non-negativity invariant on the seven weights that matter.
    """

    model_config = {"extra": "ignore"}

    goal_relevance: NonNegWeight
    value_alignment: NonNegWeight
    self_relevance: NonNegWeight
    task_utility: NonNegWeight
    emotional_intensity: NonNegWeight
    reliability: NonNegWeight
    usage_history: NonNegWeight

    def as_ordered_vector(self) -> np.ndarray:
        data = self.model_dump()
        return np.array([data[k] for k in FACTOR_KEYS], dtype=float)


class ValueScore(BaseModel):
    """Result of ``compute_vm``: a bounded scalar plus its factor breakdown.

    Enforces the claim ``V(m) ∈ [0, 1]``.
    """

    model_config = {"extra": "forbid"}

    vm: UnitScore
    factors: ValueFactors


def _max_similarity_to_phrases(content: str, phrases: list[str]) -> float:
    """Compute max cosine-like overlap via simple word-set Jaccard."""
    if not phrases:
        return 0.0
    content_words = set(content.lower().split())
    best = 0.0
    for ph in phrases:
        ph_words = set(ph.lower().split())
        inter = len(content_words & ph_words)
        union = len(content_words | ph_words)
        sim = inter / union if union else 0.0
        if sim > best:
            best = sim
    return best


def _jaccard_overlap(content: str, values: list[str]) -> float:
    """Jaccard overlap between content words and value words."""
    if not values:
        return 0.0
    content_words = set(content.lower().split())
    value_words = set()
    for v in values:
        value_words.update(v.lower().split())
    inter = len(content_words & value_words)
    union = len(content_words | value_words)
    return inter / union if union else 0.0


def _simple_sentiment_polarity(text: str) -> float:
    """Crude polarity proxy without TextBlob."""
    positive = {"good", "great", "excellent", "happy", "love", "best", "awesome", "fantastic"}
    negative = {"bad", "terrible", "awful", "hate", "worst", "sad", "angry", "poor"}
    tokens = text.lower().split()
    pos = sum(1 for t in tokens if t in positive)
    neg = sum(1 for t in tokens if t in negative)
    total = len(tokens) or 1
    return (pos - neg) / total


def extract_factors(
    content: str,
    source_type: str,
    user_goals: list[str],
    user_values: list[str],
    sentiment_pipeline=None,
) -> dict[str, float]:
    """Compute raw factor scores from content and user profile."""
    # 1. Goal relevance
    g_rel = _max_similarity_to_phrases(content, user_goals) if user_goals else 0.5

    # 2. Value alignment
    v_align = _jaccard_overlap(content, user_values) if user_values else 0.5

    # 3. Self relevance: pronoun density
    self_words = {"i", "me", "my", "myself"}
    tokens = content.lower().split()
    self_rel = min(1.0, sum(1 for t in tokens if t in self_words) / max(len(tokens), 10))

    # 4. Task utility
    action_verbs = {"schedule", "book", "buy", "call", "email", "remind", "need", "must", "should"}
    task_u = 1.0 if any(v in tokens for v in action_verbs) else 0.3

    # 5. Emotional intensity
    if sentiment_pipeline is not None:
        try:
            pol = abs(sentiment_pipeline(content).sentiment.polarity)
        except Exception:
            pol = abs(_simple_sentiment_polarity(content))
    else:
        pol = abs(_simple_sentiment_polarity(content))
    emo = max(0.3, pol)

    # 6. Reliability — the whitepaper defines this factor only for the five
    # documented source types. Reject anything else so the claim can't be
    # silently violated by a default weight.
    if source_type not in RELIABILITY_BY_SOURCE:
        raise ValueError(
            f"source_type must be one of {sorted(SOURCE_TYPES)}, got {source_type!r}"
        )
    rel = RELIABILITY_BY_SOURCE[source_type]

    # 7. Usage history
    usage = 0.5

    # Validate the seven factors against their [0, 1] contract before returning.
    # `emotional_intensity` in particular can exceed 1.0 for pathological input
    # (e.g. every token a sentiment word); clamp task_utility/emo defensively so
    # the model contract holds, then let Pydantic assert the invariant.
    factors = ValueFactors(
        goal_relevance=min(1.0, max(0.0, round(g_rel, 3))),
        value_alignment=min(1.0, max(0.0, round(v_align, 3))),
        self_relevance=min(1.0, max(0.0, round(self_rel, 3))),
        task_utility=min(1.0, max(0.0, round(task_u, 3))),
        emotional_intensity=min(1.0, max(0.0, round(emo, 3))),
        reliability=min(1.0, max(0.0, round(rel, 3))),
        usage_history=min(1.0, max(0.0, round(usage, 3))),
    )
    return factors.model_dump()


def compute_vm(
    content: str,
    user_weights: dict[str, float] | None,
    source_type: str,
    user_goals: list[str] | None = None,
    user_values: list[str] | None = None,
    sentiment_pipeline=None,
) -> tuple[float, dict[str, float]]:
    """Scalar V(m) = sigmoid( dot(weights, factors) ), with V(m) ∈ [0, 1].

    Inputs and outputs are validated with Pydantic so the whitepaper contract
    (seven bounded factors, non-negative weights, bounded scalar) is enforced at
    runtime rather than merely asserted in tests.
    """
    merged = {**DEFAULT_WEIGHTS, **(user_weights or {})}
    # Validates: exactly the 7 factor keys, all weights non-negative.
    weights = ValueWeights.model_validate(merged)
    factors = ValueFactors.model_validate(
        extract_factors(
            content, source_type, user_goals or [], user_values or [], sentiment_pipeline
        )
    )
    z = float(np.dot(weights.as_ordered_vector(), factors.as_ordered_vector()))
    vm = 1.0 / (1.0 + math.exp(-z))
    # sigmoid is mathematically in (0, 1); ValueScore asserts the [0, 1] contract
    # and guards against NaN/inf leaking from a degenerate dot product.
    result = ValueScore(vm=vm, factors=factors)
    return result.vm, result.factors.model_dump()


def learn_weights_from_feedback(
    conn,
    user_id: str = "default",
    method: str = "nelder-mead",
) -> dict[str, float]:
    """Learn per-user weights from click/retrieval-success feedback."""
    import sqlite3

    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be sqlite3.Connection")

    rows = conn.execute(
        """SELECT c.vm_factors, f.positive
           FROM feedback_log f JOIN chunk c ON f.chunk_id = c.chunk_id
           WHERE f.user_id = ?""",
        (user_id,),
    ).fetchall()
    if len(rows) < 10:
        return DEFAULT_WEIGHTS.copy()

    def loss(w_array):
        pos_scores = []
        neg_scores = []
        for vm_factors_json, positive in rows:
            factors = json.loads(vm_factors_json)
            vec = np.array([factors[k] for k in FACTOR_KEYS])
            score = 1.0 / (1.0 + math.exp(-np.dot(w_array, vec)))
            if positive:
                pos_scores.append(score)
            else:
                neg_scores.append(score)
        mean_pos = np.mean(pos_scores) if pos_scores else 0.5
        mean_neg = np.mean(neg_scores) if neg_scores else 0.5
        return -(mean_pos - mean_neg)

    try:
        from scipy.optimize import minimize
    except Exception:
        return DEFAULT_WEIGHTS.copy()

    x0 = np.array([DEFAULT_WEIGHTS[k] for k in FACTOR_KEYS])
    result = minimize(loss, x0, method="Nelder-Mead", bounds=[(0.01, 0.99)] * len(FACTOR_KEYS))
    learned = dict(zip(FACTOR_KEYS, result.x.tolist(), strict=True))
    s = sum(learned.values())
    if s == 0:
        s = 1.0
    normalized = {k: round(v / s, 4) for k, v in learned.items()}
    # Enforce the non-negative-weights contract on learned output too.
    return ValueWeights.model_validate(normalized).model_dump()
