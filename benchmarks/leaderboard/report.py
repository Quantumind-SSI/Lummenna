"""Markdown rendering for leaderboard artifacts.

The report is generated from the artifact, never hand-written, so a published
table cannot drift from the measurements behind it. Every rendering decision
here exists to stop a reader drawing a stronger conclusion than the data
supports:

* The conflict-of-interest disclosure is the first thing in the document, not a
  footnote.
* Confidence intervals sit beside every mean. Overlapping intervals are the
  normal case at these sample sizes and the table should make that obvious.
* NOT_RUN and ERROR get their own visible rows. A framework that could not be
  measured must not silently vanish and leave a tidier-looking ranking.
* Unverified and self-authored adapters are flagged inline.
"""

from __future__ import annotations

from typing import Any

from benchmarks.provenance import describe_platform

_NOT_MEASURED = "n/a"


def _fmt(metric: dict[str, Any] | None, places: int = 3) -> str:
    """Render a metric as ``mean [lo, hi]``, or ``n/a`` when unmeasured."""
    if not isinstance(metric, dict) or metric.get("mean") is None:
        return _NOT_MEASURED
    mean = metric["mean"]
    low = metric.get("ci95_lo")
    high = metric.get("ci95_hi")
    if low is None or high is None:
        return f"{mean:.{places}f}"
    return f"{mean:.{places}f} [{low:.{places}f}, {high:.{places}f}]"


def _adapter_flags(adapter: dict[str, Any]) -> str:
    """Return inline provenance warnings for an adapter."""
    flags = []
    if adapter.get("self_authored_by_maintainer"):
        flags.append("self-authored")
    if not adapter.get("adapter_verified"):
        flags.append("unverified")
    return f" ({', '.join(flags)})" if flags else ""


def render_markdown(payload: dict[str, Any], digest: str | None = None) -> str:
    """Render a complete leaderboard report.

    Args:
        payload: The artifact payload from ``run_leaderboard``, optionally
            already carrying an ``environment`` block.
        digest: SHA-256 of the written artifact, for citation.

    Returns:
        Markdown text.
    """
    dataset = payload.get("dataset", {})
    protocol = payload.get("protocol", {})
    disclosure = payload.get("disclosure", {})
    results = payload.get("results", [])

    lines: list[str] = [
        "# Memory Framework Retrieval Leaderboard",
        "",
        "## Read this first",
        "",
        f"> {disclosure.get('conflict_of_interest', '')}",
        "",
        f"> {disclosure.get('official_status_note', '')}",
        "",
        "This measures **retrieval only**: adapters return evidence ids, which are",
        "compared to dataset ground truth. No LLM judges anything, so no score",
        "depends on which generator a framework is paired with.",
        "",
    ]

    lines += _dataset_section(dataset, protocol, digest)
    lines += _results_section(results, protocol)
    lines += _not_run_section(results)
    lines += _category_section(results)
    lines += _provenance_section(payload, disclosure)
    return "\n".join(lines) + "\n"


def _dataset_section(
    dataset: dict[str, Any], protocol: dict[str, Any], digest: str | None
) -> list[str]:
    return [
        "## Evaluation set",
        "",
        f"- **Dataset:** `{dataset.get('name')}` ({dataset.get('source')})",
        f"- **Fingerprint:** `{dataset.get('fingerprint')}`",
        f"- **Items:** {dataset.get('num_items')} "
        f"| **Questions:** {dataset.get('num_questions')} "
        f"({dataset.get('num_answerable')} answerable, "
        f"{dataset.get('num_abstention')} abstention)",
        f"- **Protocol:** top_k={protocol.get('top_k')}, seed={protocol.get('seed')}, "
        f"{protocol.get('bootstrap_samples')} bootstrap samples",
        f"- **Artifact SHA-256:** `{digest or 'not recorded'}`",
        "",
        "Two runs are comparable only if their dataset fingerprints match.",
        "",
    ]


def _results_section(results: list[dict[str, Any]], protocol: dict[str, Any]) -> list[str]:
    k_values = protocol.get("k_values", [1, 5, 10])
    max_k = max(k_values) if k_values else 10
    scored = [run for run in results if run.get("status") == "OK"]

    lines = ["## Results", ""]
    if not scored:
        lines += [
            "**No framework produced a score in this run.**",
            "",
            "Nothing is reported rather than estimated. See the next section for why.",
            "",
        ]
        return lines

    header = (
        ["Framework"]
        + [f"Recall@{k}" for k in k_values]
        + [
            "MRR",
            f"nDCG@{max_k}",
            "Abstention",
            "Query ms",
        ]
    )
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))

    # Rank by the largest recall cutoff, which is the least noisy at small n.
    def sort_key(run: dict[str, Any]) -> float:
        metric = run.get("metrics", {}).get(f"recall_at_{max_k}")
        return metric.get("mean", -1.0) if isinstance(metric, dict) else -1.0

    for run in sorted(scored, key=sort_key, reverse=True):
        metrics = run.get("metrics", {})
        timings = run.get("timings", {})
        row = [run.get("framework", "?") + _adapter_flags(run.get("adapter", {}))]
        row += [_fmt(metrics.get(f"recall_at_{k}")) for k in k_values]
        row.append(_fmt(metrics.get("mrr")))
        row.append(_fmt(metrics.get(f"ndcg_at_{max_k}")))
        row.append(_fmt(metrics.get("abstention_accuracy")))
        latency = timings.get("query_latency_ms")
        row.append(f"{latency['mean']:.2f}" if isinstance(latency, dict) else _NOT_MEASURED)
        lines.append("| " + " | ".join(row) + " |")

    lines += [
        "",
        "Values are `mean [95% CI]`. **Overlapping intervals mean the difference is",
        "not statistically resolved at this sample size.** Rank order alone is not",
        "evidence of superiority.",
        "",
    ]
    leaked = [
        run["framework"]
        for run in scored
        if run.get("integrity", {}).get("leakage_detected") is True
    ]
    if leaked:
        lines += [
            f"> **Integrity warning:** ground-truth leakage detected for: "
            f"{', '.join(leaked)}. Those results are not trustworthy.",
            "",
        ]

    nondeterministic = [
        run["framework"] for run in scored if run.get("integrity", {}).get("deterministic") is False
    ]
    if nondeterministic:
        lines += [
            f"> **Reproducibility note:** {', '.join(nondeterministic)} returned different",
            "> results for a repeated identical query. These systems reinforce or re-rank",
            "> items on access, so scores depend on query order and an independent rerun",
            "> may differ. This is a property of the system, not a harness fault, but it",
            "> means their numbers carry more variance than the intervals alone suggest.",
            "",
        ]
    return lines


def _not_run_section(results: list[dict[str, Any]]) -> list[str]:
    unscored = [run for run in results if run.get("status") != "OK"]
    if not unscored:
        return []
    lines = [
        "## Not measured",
        "",
        "These frameworks produced no score. They are listed rather than omitted,",
        "because a missing row is indistinguishable from a bad one.",
        "",
        "| Framework | Status | Reason |",
        "|---|---|---|",
    ]
    for run in unscored:
        reason = str(run.get("reason", "")).replace("|", "\\|")[:160]
        lines.append(f"| {run.get('framework')} | `{run.get('status')}` | {reason} |")
    lines.append("")
    return lines


def _category_section(results: list[dict[str, Any]]) -> list[str]:
    scored = [run for run in results if run.get("status") == "OK"]
    categories: set[str] = set()
    for run in scored:
        categories.update(run.get("metrics", {}).get("per_category", {}).keys())
    if not categories or len(categories) < 2:
        return []

    lines = [
        "## By question type",
        "",
        "Aggregate scores hide the differences that matter. Memory systems tend to",
        "separate on temporal reasoning and knowledge updates, not on simple recall.",
        "",
        "| Framework | Category | Recall@10 | MRR | n |",
        "|---|---|---|---|---|",
    ]
    for run in scored:
        per_category = run.get("metrics", {}).get("per_category", {})
        for category in sorted(per_category):
            metrics = per_category[category]
            recall = metrics.get("recall_at_10")
            count = recall.get("n") if isinstance(recall, dict) else None
            if count is None:
                abstention = metrics.get("abstention_accuracy")
                count = abstention.get("n") if isinstance(abstention, dict) else "?"
            lines.append(
                f"| {run.get('framework')} | {category} | {_fmt(recall)} "
                f"| {_fmt(metrics.get('mrr'))} | {count} |"
            )
    lines.append("")
    return lines


def _provenance_section(payload: dict[str, Any], disclosure: dict[str, Any]) -> list[str]:
    environment = payload.get("environment")
    lines = [
        "## Provenance",
        "",
        f"- **Platform:** {describe_platform(environment)}",
    ]
    if isinstance(environment, dict):
        repository = environment.get("repository", {})
        packages = environment.get("packages", {})
        lines.append(
            f"- **Commit:** `{repository.get('commit') or 'not recorded'}`"
            + (" (working tree dirty)" if repository.get("dirty") else "")
        )
        installed = {name: version for name, version in packages.items() if version}
        if installed:
            rendered = ", ".join(
                f"{name}=={version}" for name, version in sorted(installed.items())
            )
            lines.append(f"- **Packages:** {rendered}")
        lines.append(f"- **Captured:** {environment.get('captured_at_utc')}")

    self_authored = disclosure.get("adapters_authored_by_maintainer") or []
    unverified = disclosure.get("adapters_without_independent_verification") or []
    lines += [
        "",
        "### Adapter provenance",
        "",
        f"- Authored by the leaderboard maintainer: "
        f"{', '.join(self_authored) if self_authored else 'none'}",
        f"- Without independent verification: "
        f"{', '.join(unverified) if unverified else 'none'}",
        "",
        f"Reproduce: `{disclosure.get('reproduction', '')}`",
        "",
    ]
    return lines
