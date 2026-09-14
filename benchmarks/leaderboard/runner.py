"""Execution engine for the memory-framework leaderboard.

The runner owns every decision that could bias a comparison, so no adapter can
influence its own measurement conditions:

* Identical items, identical order, identical seed for every adapter.
* Ingest and query timed separately, so pre-computation is visible rather than
  hidden in somebody else's column.
* An unavailable adapter yields ``NOT_RUN`` with a published reason. There is
  no code path that converts a failure into a zero, because a zero is a claim
  that the system performed badly, and that would be a false claim.
* Ground-truth leakage is actively checked, not assumed absent.
"""

from __future__ import annotations

import contextlib
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from benchmarks.leaderboard.adapter import MemoryAdapter, Question
from benchmarks.leaderboard.datasets import MemoryDataset
from benchmarks.leaderboard.metrics import (
    abstention_correct,
    bootstrap_summary,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
)

#: Retrieval cutoffs reported for every run.
DEFAULT_K_VALUES = (1, 5, 10)

#: Questions re-run with ground truth stripped to detect leakage.
_LEAKAGE_PROBE_SIZE = 8

#: Run outcome states. NOT_RUN and ERROR are first-class results, never
#: silently coerced into a score.
STATUS_OK = "OK"
STATUS_NOT_RUN = "NOT_RUN"
STATUS_ERROR = "ERROR"


@dataclass
class AdapterRun:
    """The complete outcome of evaluating one adapter on one dataset."""

    framework: str
    status: str
    info: dict[str, Any]
    reason: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, Any] = field(default_factory=dict)
    integrity: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "framework": self.framework,
            "status": self.status,
            "reason": self.reason,
            "adapter": self.info,
            "metrics": self.metrics,
            "timings": self.timings,
            "integrity": self.integrity,
        }


def _adapter_info_dict(adapter: MemoryAdapter) -> dict[str, Any]:
    """Serialise adapter provenance, including the conflict-of-interest flag."""
    info = adapter.info()
    return {
        "framework": info.framework,
        "version": info.version,
        "authored_by": info.authored_by,
        "verified_by": info.verified_by,
        "adapter_verified": info.verified_by is not None,
        "self_authored_by_maintainer": info.is_self_authored,
        "config_summary": info.config_summary,
        "homepage": info.homepage,
    }


def _check_integrity(
    adapter: MemoryAdapter,
    questions: list[Question],
    top_k: int,
) -> dict[str, Any]:
    """Probe for ground-truth leakage, controlling for non-determinism.

    A naive leakage probe compares a with-truth call to a stripped call and
    calls any difference cheating. That is wrong for stateful systems: a
    framework that reinforces items on access (Lummenna does, via
    ``_record_access``) returns a different ranking on the *second* identical
    query, and would be falsely accused.

    Three calls per question separate the two effects:

    ===========================  ==========================================
    ``first == second``          Adapter is deterministic under repetition.
    ``second != with_truth``     Ground truth changed the result: leakage.
    ``first != second``          Non-deterministic; leakage undecidable and
                                 reported as such rather than guessed.
    ===========================  ==========================================

    Non-determinism is itself published, because it bears on whether an
    independent rerun can be expected to reproduce the score.

    Args:
        adapter: Already-ingested adapter.
        questions: Answerable questions to probe.
        top_k: Same cutoff used for the scored run.

    Returns:
        Mapping describing probe size, determinism, and leakage verdict.
    """
    probe = questions[:_LEAKAGE_PROBE_SIZE]
    if not probe:
        return {
            "probe_questions": 0,
            "deterministic": None,
            "leakage_detected": None,
            "note": "no answerable questions available to probe",
        }

    def strip(question: Question) -> Question:
        return Question(
            question_id=question.question_id,
            text=question.text,
            answer_item_ids=frozenset(),
            category=question.category,
        )

    nondeterministic = 0
    leaking = 0
    for question in probe:
        stripped = strip(question)
        first = adapter.retrieve(stripped, top_k).item_ids
        second = adapter.retrieve(stripped, top_k).item_ids
        with_truth = adapter.retrieve(question, top_k).item_ids
        if first != second:
            nondeterministic += 1
            continue
        if second != with_truth:
            leaking += 1

    decidable = len(probe) - nondeterministic
    return {
        "probe_questions": len(probe),
        "nondeterministic_questions": nondeterministic,
        "deterministic": nondeterministic == 0,
        "leakage_decidable_questions": decidable,
        "leakage_mismatches": leaking,
        # None, not False: with nothing decidable we did not clear the adapter,
        # we simply could not test it.
        "leakage_detected": (leaking > 0) if decidable > 0 else None,
    }


def _score_questions(
    adapter: MemoryAdapter,
    dataset: MemoryDataset,
    top_k: int,
    k_values: tuple[int, ...],
) -> tuple[dict[str, list[float]], list[float], dict[str, dict[str, list[float]]]]:
    """Run every question once, collecting per-question scores.

    Returns:
        ``(overall_scores, latencies, per_category_scores)`` where score dicts
        map metric name to the per-question values feeding the bootstrap.
    """
    overall: dict[str, list[float]] = {f"recall_at_{k}": [] for k in k_values}
    overall["mrr"] = []
    overall[f"ndcg_at_{max(k_values)}"] = []
    overall["abstention_accuracy"] = []
    latencies: list[float] = []
    per_category: dict[str, dict[str, list[float]]] = {}

    for question in dataset.questions:
        result = adapter.retrieve(question, top_k)
        latencies.append(result.latency_ms)
        category = per_category.setdefault(question.category, {})

        if question.is_abstention:
            score = abstention_correct(result.item_ids)
            overall["abstention_accuracy"].append(score)
            category.setdefault("abstention_accuracy", []).append(score)
            continue

        relevant = question.answer_item_ids
        for k in k_values:
            value = recall_at_k(result.item_ids, relevant, k)
            overall[f"recall_at_{k}"].append(value)
            category.setdefault(f"recall_at_{k}", []).append(value)
        rr = reciprocal_rank(result.item_ids, relevant)
        overall["mrr"].append(rr)
        category.setdefault("mrr", []).append(rr)
        ndcg_key = f"ndcg_at_{max(k_values)}"
        ndcg = ndcg_at_k(result.item_ids, relevant, max(k_values))
        overall[ndcg_key].append(ndcg)
        category.setdefault(ndcg_key, []).append(ndcg)

    return overall, latencies, per_category


def _summarise(scores: dict[str, list[float]], seed: int) -> dict[str, Any]:
    """Bootstrap every collected metric, omitting those with no observations."""
    summary: dict[str, Any] = {}
    for name, values in scores.items():
        result = bootstrap_summary(values, seed=seed)
        if result is not None:
            summary[name] = result.as_dict()
    return summary


def run_adapter(
    adapter: MemoryAdapter,
    dataset: MemoryDataset,
    *,
    top_k: int = 10,
    seed: int = 42,
    k_values: tuple[int, ...] = DEFAULT_K_VALUES,
) -> AdapterRun:
    """Evaluate one adapter against one dataset.

    Args:
        adapter: The framework under test.
        dataset: Loaded evaluation data.
        top_k: How many items to request per question.
        seed: Bootstrap seed, for reproducible intervals.
        k_values: Recall cutoffs to report.

    Returns:
        An :class:`AdapterRun`. Never raises for adapter failures: an exception
        becomes ``STATUS_ERROR`` with its traceback so one broken framework
        cannot destroy a whole leaderboard run.
    """
    info = _adapter_info_dict(adapter)
    framework = info["framework"]

    runnable, reason = adapter.available()
    if not runnable:
        return AdapterRun(framework, STATUS_NOT_RUN, info, reason=reason)

    try:
        adapter.reset()
        ingest_started = time.perf_counter()
        adapter.ingest(dataset.items)
        ingest_seconds = time.perf_counter() - ingest_started

        query_started = time.perf_counter()
        scores, latencies, per_category = _score_questions(adapter, dataset, top_k, k_values)
        query_seconds = time.perf_counter() - query_started

        integrity = _check_integrity(adapter, dataset.answerable_questions, top_k)

        metrics = _summarise(scores, seed)
        metrics["per_category"] = {
            category: _summarise(category_scores, seed)
            for category, category_scores in sorted(per_category.items())
        }
        latency_summary = bootstrap_summary(latencies, seed=seed)
        timings = {
            "ingest_seconds": round(ingest_seconds, 3),
            "ingest_items_per_second": round(len(dataset.items) / ingest_seconds, 2)
            if ingest_seconds > 0
            else None,
            "query_seconds_total": round(query_seconds, 3),
            "query_latency_ms": latency_summary.as_dict() if latency_summary else None,
        }
        return AdapterRun(
            framework, STATUS_OK, info, metrics=metrics, timings=timings, integrity=integrity
        )
    except Exception as exc:
        return AdapterRun(
            framework,
            STATUS_ERROR,
            info,
            reason=f"{type(exc).__name__}: {exc}",
            integrity={"traceback": traceback.format_exc(limit=8)},
        )
    finally:
        # Teardown failure must not mask a completed measurement.
        with contextlib.suppress(Exception):
            adapter.teardown()


def run_leaderboard(
    dataset: MemoryDataset,
    adapters: list[MemoryAdapter],
    *,
    top_k: int = 10,
    seed: int = 42,
    k_values: tuple[int, ...] = DEFAULT_K_VALUES,
) -> dict[str, Any]:
    """Evaluate every adapter against one dataset and build the report payload.

    Args:
        dataset: Loaded evaluation data, identical for all adapters.
        adapters: Frameworks to evaluate.
        top_k: Items requested per question.
        seed: Bootstrap seed.
        k_values: Recall cutoffs.

    Returns:
        A JSON-serialisable payload. ``provenance.write_artifact`` adds the
        environment envelope.
    """
    if not adapters:
        raise ValueError("run_leaderboard requires at least one adapter")
    if top_k <= 0:
        raise ValueError(f"top_k must be positive, got {top_k}")

    runs = [
        run_adapter(adapter, dataset, top_k=top_k, seed=seed, k_values=k_values)
        for adapter in adapters
    ]
    scored = [run for run in runs if run.status == STATUS_OK]
    return {
        "benchmark": "memory_leaderboard",
        "dataset": dataset.summary(),
        "protocol": {
            "top_k": top_k,
            "k_values": list(k_values),
            "seed": seed,
            "bootstrap_samples": 1000,
            "scoring": "retrieval-only; no LLM judge; evidence ids compared to dataset ground truth",
            "abstention_scored_separately": True,
        },
        "disclosure": _disclosure(runs),
        "results": [run.as_dict() for run in runs],
        "summary": {
            "adapters_total": len(runs),
            "adapters_scored": len(scored),
            "adapters_not_run": sum(1 for run in runs if run.status == STATUS_NOT_RUN),
            "adapters_errored": sum(1 for run in runs if run.status == STATUS_ERROR),
        },
    }


def _disclosure(runs: list[AdapterRun]) -> dict[str, Any]:
    """Build the conflict-of-interest block published with every result.

    Derived from the runs rather than hardcoded, so it cannot fall out of sync
    with what was actually measured.
    """
    maintainer_authored = [
        run.framework for run in runs if run.info.get("self_authored_by_maintainer")
    ]
    unverified = [run.framework for run in runs if not run.info.get("adapter_verified")]
    return {
        "maintainer": "Lummenna (QuantumindSSI)",
        "conflict_of_interest": (
            "This leaderboard is published by the maintainer of Lummenna, which is "
            "one of the ranked systems. Results are not independently audited. "
            "Treat them as a reproducible starting point, not a neutral verdict."
        ),
        "is_official": False,
        "official_status_note": (
            "No standards body endorses this leaderboard. It is not an official "
            "ranking and does not claim to be one."
        ),
        "adapters_authored_by_maintainer": sorted(maintainer_authored),
        "adapters_without_independent_verification": sorted(unverified),
        "reproduction": "python -m benchmarks.leaderboard.run --help",
    }
