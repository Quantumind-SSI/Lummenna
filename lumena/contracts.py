"""Canonical claim contracts.

This module is the single source of truth for the *documented* invariants of
Lumena — the concrete, enumerable, bounded claims made in the whitepaper
(``docs/Lumena_Whitepaper.md``) and ``README.md``. Every literal set and every
numeric range asserted in prose is defined here once, as a Pydantic-validatable
type, and imported by the algorithmic modules that must honour it.

The intent is that a documented claim like

    "Chunks carry a resolution quantifier (FP32 → FP16 → INT8 → BINARY → RELEASED)"

is not merely asserted in the docs and hoped-for in the code, but is *grounded*:
the code refuses to construct or persist a value that violates it.

See ``docs/CLAIMS_AUDIT.md`` for the claim → code → enforcement mapping.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

# ---------------------------------------------------------------------------
# Enumerable claims (closed literal sets asserted by the docs / SQL CHECKs)
# ---------------------------------------------------------------------------

# Whitepaper 3.1 / schema.sql `room.room_type` CHECK.
RoomType = Literal["domain", "project", "person", "ephemeral"]
ROOM_TYPES: frozenset[str] = frozenset(("domain", "project", "person", "ephemeral"))

# Whitepaper 3.1 / 3.5 (L3) precision ladder; schema.sql `chunk.resolution` CHECK.
Resolution = Literal["FP32", "FP16", "INT8", "BINARY", "RELEASED"]
# Ordered degradation chain: each key degrades to its value (RELEASED is terminal).
RESOLUTION_CHAIN: dict[str, str] = {
    "FP32": "FP16",
    "FP16": "INT8",
    "INT8": "BINARY",
    "BINARY": "RELEASED",
}

# Whitepaper 3.4 (reliability weighting) / schema.sql `provenance.source_type` CHECK.
SourceType = Literal[
    "user_input", "agent_reasoning", "consolidation", "import", "p2p_share"
]
SOURCE_TYPES: frozenset[str] = frozenset(
    ("user_input", "agent_reasoning", "consolidation", "import", "p2p_share")
)

# Whitepaper 3.3 stage 1: "classifies queries as factual, exploratory,
# relational, or temporal".
IntentLabel = Literal["factual", "exploratory", "relational", "temporal"]
INTENT_LABELS: frozenset[str] = frozenset(
    ("factual", "exploratory", "relational", "temporal")
)

# schema.sql `feedback_log.feedback_type` CHECK.
FeedbackType = Literal["implicit", "explicit", "repair"]

# Whitepaper 7 / config: PII strategy on the write path.
PIIRedactionMode = Literal["block", "redact", "hash"]

# ---------------------------------------------------------------------------
# Bounded scalar claims (numeric ranges asserted by the docs)
# ---------------------------------------------------------------------------

# Whitepaper 3.4: "Every chunk receives a scalar value V(m) ∈ [0, 1]".
# Also the seven factor scores are each in [0, 1].
UnitInterval = Annotated[float, Field(ge=0.0, le=1.0)]

# Cosine similarity is mathematically bounded to [-1, 1].
CosineSimilarity = Annotated[float, Field(ge=-1.0, le=1.0)]

# Whitepaper 3.2 TFC state ranges.
ConservationBias = Annotated[float, Field(ge=0.0, le=1.0)]  # `e`
AttentionTemperature = Annotated[float, Field(ge=0.0, le=1.0)]  # `a`
TemporalHorizonDays = Annotated[float, Field(gt=0.0)]  # `τ` > 0 days
ResolutionLevel = Annotated[int, Field(ge=0, le=5)]  # `r` ∈ {0, ..., 5}

# Whitepaper 3.1 / 7: embeddings are 384-dimensional (bge-small-en-v1.5).
EMBEDDING_DIMS: int = 384

# schema.sql `provenance.confidence` default 1.0; a probability-like weight.
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

# ---------------------------------------------------------------------------
# Structural / cardinality claims (whitepaper 3.1 palace topology)
# ---------------------------------------------------------------------------
# These are *soft* design-target ranges ("Rooms: 8–50 per palace", "Loci: 4–20
# per room"), not hard invariants the code must reject — the docs describe them
# as typical scales, not constraints. They are recorded here as named constants
# so they have a single source of truth and can be asserted in benchmarks, but
# they are intentionally NOT enforced as validators.
ROOMS_PER_PALACE_TYPICAL: tuple[int, int] = (8, 50)
LOCI_PER_ROOM_TYPICAL: tuple[int, int] = (4, 20)

# Whitepaper 3.3 stage 3: standard RRF constant (Cormack et al., 2009).
K_RRF: int = 60


def validate_source_type(value: str) -> SourceType:
    """Return ``value`` if it is a documented source type, else raise.

    Grounds the whitepaper 3.4 reliability-weighting claim: the V(m) reliability
    factor is only defined for the five documented source types. An unknown
    source type must fail loudly rather than silently receive a default weight.
    """
    if value not in SOURCE_TYPES:
        raise ValueError(
            f"source_type must be one of {sorted(SOURCE_TYPES)}, got {value!r}"
        )
    return value  # type: ignore[return-value]


def validate_intent_label(value: str) -> IntentLabel:
    """Return ``value`` if it is one of the four documented intent classes."""
    if value not in INTENT_LABELS:
        raise ValueError(
            f"intent label must be one of {sorted(INTENT_LABELS)}, got {value!r}"
        )
    return value  # type: ignore[return-value]


def validate_embedding_dims(n: int) -> int:
    """Return ``n`` if it matches the documented 384-dim embedding claim."""
    if n != EMBEDDING_DIMS:
        raise ValueError(
            f"embedding must be {EMBEDDING_DIMS}-dimensional (whitepaper 3.1), got {n}"
        )
    return n
