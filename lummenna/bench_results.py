"""Load and validate committed benchmark evidence for user-facing surfaces.

Benchmark values are never synthesized. Each loader returns an explicit
``available`` flag and ``None`` for measurements that cannot be traced to a
valid committed artifact.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from lummenna.logging import get_console_logger

logger = get_console_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACKAGED_ROOT = Path(__file__).resolve().parent / "benchmark_data"


def _empty_retrieval() -> dict[str, Any]:
    return {
        "available": False,
        "artifact": None,
        "artifact_sha256": None,
        "corpus": None,
        "passages": None,
        "queries": None,
        "seeds": None,
        "bootstrap_samples": None,
        "model": None,
        "recall_at_10": {"bm25": None, "dense": None, "hybrid": None},
        "ndcg_at_10": {"bm25": None, "dense": None, "hybrid": None},
        "latency_mean_ms": {"bm25": None, "dense": None, "hybrid": None},
    }


def _artifact_candidates(filename: str, *repo_parts: str) -> list[Path]:
    """Return packaged and source-tree locations for a benchmark artifact."""
    return [_PACKAGED_ROOT / filename, _REPO_ROOT.joinpath(*repo_parts)]


def _load_artifact(
    expected_benchmark: str, candidates: list[Path]
) -> tuple[dict[str, Any], Path, str] | None:
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            raw = candidate.read_bytes()
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("benchmark_artifact_unreadable", path=str(candidate), error=str(exc))
            continue
        if not isinstance(data, dict) or data.get("benchmark") != expected_benchmark:
            logger.warning(
                "benchmark_artifact_invalid",
                path=str(candidate),
                expected=expected_benchmark,
                actual=data.get("benchmark") if isinstance(data, dict) else None,
            )
            continue
        return data, candidate, hashlib.sha256(raw).hexdigest()
    return None


def _finite_number(value: object, *, minimum: float = 0.0, maximum: float | None = None):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _metric_mean(metrics: dict[str, Any], key: str, *, bounded: bool = False) -> float | None:
    metric = metrics.get(key)
    if not isinstance(metric, dict):
        return None
    value = _finite_number(metric.get("mean"), maximum=1.0 if bounded else None)
    return round(value, 4) if value is not None else None


def _positive_int(value: object) -> int | None:
    number = _finite_number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def load_retrieval_benchmarks() -> dict[str, Any]:
    """Return validated same-model retrieval measurements and provenance."""
    result = _empty_retrieval()
    loaded = _load_artifact(
        "retrieval",
        _artifact_candidates(
            "retrieval_results.json",
            "benchmarks",
            "retrieval",
            "results",
            "retrieval_results.json",
        ),
    )
    if loaded is None:
        return result

    data, path, digest = loaded
    embedders = data.get("embedders")
    if not isinstance(embedders, list):
        return result
    bge = next(
        (
            item
            for item in embedders
            if isinstance(item, dict)
            and "bge-small-en-v1.5" in str(item.get("embedder_full", "")).lower()
        ),
        None,
    )
    if bge is None or not isinstance(bge.get("results"), dict):
        return result

    measurements = bge["results"]
    channels = {
        "bm25": measurements.get("bm25_only", {}),
        "dense": measurements.get("dense_only", {}),
        "hybrid": measurements.get("hybrid", {}),
    }
    if not all(isinstance(channel, dict) for channel in channels.values()):
        return result

    result.update(
        {
            "artifact": path.name,
            "artifact_sha256": digest,
            "corpus": data.get("corpus"),
            "passages": _positive_int(data.get("num_passages")),
            "queries": _positive_int(data.get("num_queries")),
            "seeds": _positive_int(data.get("num_seeds")),
            "bootstrap_samples": _positive_int(data.get("bootstrap_samples")),
            "model": bge.get("embedder_full"),
            "recall_at_10": {
                name: _metric_mean(channel, "recall_10", bounded=True)
                for name, channel in channels.items()
            },
            "ndcg_at_10": {
                name: _metric_mean(channel, "ndcg_10", bounded=True)
                for name, channel in channels.items()
            },
            "latency_mean_ms": {
                name: _metric_mean(channel, "latency_ms") for name, channel in channels.items()
            },
        }
    )
    result["available"] = all(
        (
            isinstance(result["corpus"], str),
            result["passages"] is not None,
            result["queries"] is not None,
            result["seeds"] is not None,
            isinstance(result["model"], str),
            result["recall_at_10"]["hybrid"] is not None,
            result["latency_mean_ms"]["hybrid"] is not None,
        )
    )
    return result


def _load_e2e_summary() -> dict[str, Any]:
    empty = {
        "available": False,
        "artifact": None,
        "artifact_sha256": None,
        "model": None,
        "sessions": None,
        "queries": None,
        "recall_at_1": None,
        "recall_at_5": None,
        "latency_mean_ms": None,
    }
    loaded = _load_artifact(
        "e2e_memory_quality",
        _artifact_candidates(
            "e2e_results.json", "benchmarks", "e2e", "results", "e2e_results.json"
        ),
    )
    if loaded is None:
        return empty
    data, path, digest = loaded
    metrics = data.get("aggregate_metrics")
    if not isinstance(metrics, dict):
        return empty
    summary = {
        "available": False,
        "artifact": path.name,
        "artifact_sha256": digest,
        "model": data.get("embedder"),
        "sessions": _positive_int(data.get("num_sessions")),
        "queries": _positive_int(data.get("num_query_turns")),
        "recall_at_1": _metric_mean(metrics, "recall_1", bounded=True),
        "recall_at_5": _metric_mean(metrics, "recall_5", bounded=True),
        "latency_mean_ms": _metric_mean(metrics, "latency_ms"),
    }
    # A dict-shaped `aggregate_metrics` is not evidence; surviving validation
    # is. Without this check an artifact whose metrics are all out-of-range or
    # NaN reports available=True with every value None, and the dashboard
    # presents "measured" alongside a column of N/A.
    summary["available"] = all(
        (
            isinstance(summary["model"], str),
            summary["sessions"] is not None,
            summary["queries"] is not None,
            summary["recall_at_1"] is not None,
            summary["latency_mean_ms"] is not None,
        )
    )
    return summary


def _load_beir_summary() -> dict[str, Any]:
    empty: dict[str, Any] = {
        "available": False,
        "artifact": None,
        "artifact_sha256": None,
        "datasets": [],
        "passages_per_dataset": None,
        "queries_per_dataset": None,
        "model": None,
        "hybrid_recall_at_10_macro": None,
        "hybrid_ndcg_at_10_macro": None,
    }
    loaded = _load_artifact(
        "beir",
        _artifact_candidates(
            "beir_results.json", "benchmarks", "beir", "results", "beir_results.json"
        ),
    )
    if loaded is None:
        return empty
    data, path, digest = loaded
    dataset_results = data.get("results")
    if not isinstance(dataset_results, list):
        return empty

    recall_values: list[float] = []
    ndcg_values: list[float] = []
    datasets: list[str] = []
    model: str | None = None
    for dataset in dataset_results:
        if not isinstance(dataset, dict):
            continue
        embedders = dataset.get("embedders")
        if not isinstance(embedders, list):
            continue
        bge = next(
            (
                item
                for item in embedders
                if isinstance(item, dict)
                and "bge-small-en-v1.5" in str(item.get("embedder_full", "")).lower()
            ),
            None,
        )
        if bge is None:
            continue
        bge_results = bge.get("results")
        if not isinstance(bge_results, dict):
            continue
        hybrid = bge_results.get("hybrid", {})
        if not isinstance(hybrid, dict):
            continue
        recall = _metric_mean(hybrid, "recall_10", bounded=True)
        ndcg = _metric_mean(hybrid, "ndcg_10", bounded=True)
        if recall is None or ndcg is None:
            continue
        datasets.append(str(dataset.get("dataset", "unknown")))
        recall_values.append(recall)
        ndcg_values.append(ndcg)
        model = str(bge.get("embedder_full") or bge.get("embedder") or "") or None

    if not datasets:
        return empty
    return {
        "available": True,
        "artifact": path.name,
        "artifact_sha256": digest,
        "datasets": datasets,
        "passages_per_dataset": _positive_int(data.get("num_passages")),
        "queries_per_dataset": _positive_int(data.get("num_queries")),
        "model": model,
        "hybrid_recall_at_10_macro": round(sum(recall_values) / len(recall_values), 4),
        "hybrid_ndcg_at_10_macro": round(sum(ndcg_values) / len(ndcg_values), 4),
    }


def load_evaluation_summary() -> dict[str, Any]:
    """Return artifact-backed evaluation status without implying a ranking."""
    retrieval = load_retrieval_benchmarks()
    beir = _load_beir_summary()
    e2e = _load_e2e_summary()
    return {
        "status": "subset_evidence_only"
        if any((retrieval["available"], beir["available"], e2e["available"]))
        else "not_measured",
        "official_leaderboard_rank": None,
        "external_system_comparison_available": False,
        "retrieval": retrieval,
        "beir_subset": beir,
        "e2e_memory": e2e,
    }
