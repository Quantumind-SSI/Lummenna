"""Shared loader for committed benchmark result artifacts.

Used by both the FastAPI dashboard endpoints and the MCP server so that
every user-facing surface reports identical, artifact-backed numbers.

Every value is ``None`` ("not yet benchmarked") unless a committed artifact
provides it — no hardcoded fallback numbers, ever.
"""

from __future__ import annotations

import json
from pathlib import Path

from lummenna.logging import get_console_logger

logger = get_console_logger(__name__)

_EMPTY: dict = {
    "corpus": None,
    "bm25_r10": None,
    "hybrid_r10_bge": None,
    "dense_r10_minilm": None,
    "ndcg10": None,
    "latency_bm25_ms": None,
    "latency_dense_ms": None,
    "latency_hybrid_ms": None,
}


def _candidate_paths() -> list[Path]:
    return [
        Path(__file__).resolve().parent.parent
        / "benchmarks"
        / "retrieval"
        / "results"
        / "retrieval_results.json",
        Path("benchmarks/retrieval/results/retrieval_results.json"),
    ]


def _mean_latency(cfg_metrics: dict) -> float | None:
    lat = cfg_metrics.get("latency_ms")
    if isinstance(lat, dict) and lat.get("mean") is not None:
        return round(float(lat["mean"]), 1)
    if isinstance(lat, list) and lat:
        return round(sum(float(x) for x in lat) / len(lat), 1)
    return None


def load_retrieval_benchmarks() -> dict:
    """Load retrieval metrics from the committed benchmark artifact.

    Returns:
        Dict with keys ``corpus``, ``bm25_r10``, ``hybrid_r10_bge``,
        ``dense_r10_minilm``, ``ndcg10``, ``latency_bm25_ms``,
        ``latency_dense_ms``, ``latency_hybrid_ms``. Any metric without a
        committed measurement is ``None``.
    """
    benchmarks = dict(_EMPTY)
    for candidate in _candidate_paths():
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            embedders = data.get("embedders", [])
            if not embedders:
                continue
            benchmarks["corpus"] = data.get("corpus")
            bge_emb = next(
                (e for e in embedders if "BGE" in e.get("embedder_full", "").upper()),
                embedders[0],
            )
            minilm_emb = next(
                (e for e in embedders if "MiniLM" in e.get("embedder_full", "")), None
            )
            res = bge_emb.get("results", {})
            hybrid = res.get("hybrid", {})
            dense = res.get("dense_only", {})
            bm25 = res.get("bm25_only", {})

            r10 = hybrid.get("recall_10", {})
            if r10.get("mean") is not None:
                benchmarks["hybrid_r10_bge"] = round(r10["mean"], 3)

            if minilm_emb:
                minilm_r10 = (
                    minilm_emb.get("results", {}).get("dense_only", {}).get("recall_10", {})
                )
                if minilm_r10.get("mean") is not None:
                    benchmarks["dense_r10_minilm"] = round(minilm_r10["mean"], 3)

            br10 = bm25.get("recall_10", {})
            if br10.get("mean") is not None:
                benchmarks["bm25_r10"] = round(br10["mean"], 3)

            nd = hybrid.get("ndcg_10", {})
            if nd.get("mean") is not None:
                benchmarks["ndcg10"] = round(nd["mean"], 3)

            benchmarks["latency_hybrid_ms"] = _mean_latency(hybrid)
            benchmarks["latency_dense_ms"] = _mean_latency(dense)
            benchmarks["latency_bm25_ms"] = _mean_latency(bm25)
            break
        except (ValueError, OSError) as exc:
            logger.warning("benchmark_artifact_unreadable", path=str(candidate), error=str(exc))
    return benchmarks
