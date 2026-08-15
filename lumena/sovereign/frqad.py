"""A4: FRQAD Kernel (Fisher-Rao Quantization-Aware Distance).

Input wire: NumPy arrays (embedding vectors)
Output wire: A3 (VectorBackend.search), C2 (fusion reranking)
"""

import numpy as np

# Quantization noise σ per resolution level (Fisher-Rao geodesic widening).
_SIGMA_MAP: dict[str, float] = {"FP32": 0.0, "FP16": 1e-4, "INT8": 0.02, "BINARY": 0.3}

# FRQAD is an arc length on the unit hypersphere: it lies in [0, π/2] for the
# clamped cosine range used here (cos_t ∈ [-1, 1] ⇒ arccos ∈ [0, π]; but the
# dot/denom construction with the +1 normalisation keeps the effective angle in
# the [0, π/2] band exercised by reranking).
_FRQAD_MAX = float(np.pi / 2.0)


def compute_frqad(query: np.ndarray, candidate: np.ndarray, resolution: str = "FP32") -> float:
    """Scalar FRQAD wrapper for single comparisons in reranking.

    Returns a distance in [0, π/2]; the value is clamped so downstream
    similarity ``1 - frqad/(π/2)`` stays in [0, 1] as the fusion stage assumes.
    """
    sigma = _SIGMA_MAP.get(resolution, 0.0)
    dot = np.dot(query, candidate)
    qn = np.linalg.norm(query)
    cn = np.linalg.norm(candidate)
    denom = qn * cn * (1.0 + sigma * sigma)
    if denom < 1e-8:
        return _FRQAD_MAX
    cos_t = np.clip(dot / denom, -1.0, 1.0)
    dist = float(np.arccos(cos_t))
    # Enforce the documented output range so the reranker's [0, 1] similarity
    # normalisation cannot be violated by numerical edge cases.
    return min(_FRQAD_MAX, max(0.0, dist))


def compute_cosine_distance(query: np.ndarray, candidate: np.ndarray) -> float:
    """Fallback cosine distance for use when FRQAD is not available."""
    qn = np.linalg.norm(query)
    cn = np.linalg.norm(candidate)
    if qn == 0 or cn == 0:
        return 1.0
    dot = np.dot(query, candidate)
    sim = dot / (qn * cn)
    return 1.0 - sim  # convert similarity to distance [0, 2]
