"""
Unified Benchmark Orchestrator for Lummenna.

Runs ALL benchmark suites sequentially and generates a unified Markdown
report at benchmarks/results/report.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

BENCHMARKS_DIR = Path(__file__).parent
RESULTS_DIR = BENCHMARKS_DIR / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# Every suite in benchmarks/ — kept in sync with the README table.
SUITES = [
    "retrieval",
    "forgetting",
    "perf",
    "navigation",
    "e2e",
    "beir",
    "ablation",
    "optical",
    "tfc",
    "stress",
    "cross_system",
]

# Suites whose result artifact does not follow the "<name>_results.json" pattern.
RESULT_FILENAMES = {
    "optical": "optical_degradation.json",
    "tfc": "tfc_sensitivity.json",
}


def _fmt(value: Any, spec: str) -> str:
    """Format a numeric value, or return 'N/A' for missing/non-numeric data."""
    if isinstance(value, (int, float)):
        return format(value, spec)
    return "N/A"


def run_suite(name: str) -> dict[str, Any]:
    script = BENCHMARKS_DIR / name / "run.py"
    if not script.exists():
        print(f"[ERROR] Suite script not found: {script}")
        return {"suite": name, "status": "missing", "results": None}

    print(f"\n{'='*60}")
    print(f"Running benchmark suite: {name}")
    print(f"{'='*60}")

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(BENCHMARKS_DIR.parent),
        capture_output=False,
    )

    result_filename = RESULT_FILENAMES.get(name, f"{name}_results.json")
    json_path = BENCHMARKS_DIR / name / "results" / result_filename
    data: dict[str, Any] = {"suite": name, "status": "unknown", "results": None}
    if result.returncode == 0 and json_path.exists():
        try:
            with open(json_path, encoding="utf-8") as f:
                data["results"] = json.load(f)
            data["status"] = "success"
        except Exception as exc:
            data["status"] = f"json_error: {exc}"
    else:
        data["status"] = f"failed (exit code {result.returncode})"

    return data


def generate_report(suite_results: list[dict[str, Any]]) -> Path:
    lines = [
        "# Lummenna Benchmark Suite Report",
        "",
        f"**Date:** {__import__('datetime').datetime.now().isoformat()}",
        "",
        "## Summary",
        "",
        "| Suite | Status | Key Metrics |",
        "|---|---|---|",
    ]

    for sr in suite_results:
        status = sr["status"]
        res = sr.get("results", {})
        if sr["suite"] == "retrieval" and isinstance(res, dict):
            metrics = []
            embedders = res.get("embedders", [])
            first = embedders[0].get("results", {}) if embedders else {}
            for cfg in ["bm25_only", "dense_only", "hybrid"]:
                m = first.get(cfg, {})
                r10 = m.get("recall_10", {}).get("mean", "N/A")
                nd10 = m.get("ndcg_10", {}).get("mean", "N/A")
                metrics.append(f"{cfg}: R@10={r10} nDCG@10={nd10}")
            key = "; ".join(metrics)
            if embedders:
                key = f"[{embedders[0].get('embedder', '?')}] " + key
        elif sr["suite"] == "forgetting" and isinstance(res, dict):
            key = (
                f"alive={res.get('final_state', {}).get('alive', 'N/A')}, "
                f"forgotten={res.get('final_state', {}).get('forgotten', 'N/A')} "
                f"({res.get('final_state', {}).get('pct_released', 'N/A')}% released)"
            )
        elif sr["suite"] == "perf" and isinstance(res, dict):
            metrics = []
            for r in res.get("per_size", []):
                lat = r.get("query_latency_ms", {})
                metrics.append(
                    f"{r.get('corpus_size', '?')}: p50={lat.get('p50_ms', 'N/A')}ms "
                    f"DB={r.get('db_size_mb', 'N/A')}MB"
                )
            key = "; ".join(metrics)
        elif sr["suite"] == "navigation" and isinstance(res, dict):
            summary = res.get("summary", {})
            routing = summary.get("routing_and_pruning", {})
            oracle_vs = summary.get("oracle_vs_global", {})
            routed_vs = summary.get("routed_vs_global", {})
            key = (
                f"routing={_fmt(routing.get('routing_accuracy'), '.1%')}, "
                f"prune={_fmt(routing.get('mean_pruning_ratio'), '.1%')}, "
                f"oracle_speedup={oracle_vs.get('latency_speedup', {}).get('ratio_of_means', 'N/A')}x, "
                f"routed_speedup={routed_vs.get('latency_speedup', {}).get('ratio_of_means', 'N/A')}x"
            )
        elif sr["suite"] == "e2e" and isinstance(res, dict):
            agg = res.get("aggregate_metrics", {})
            r10 = agg.get("recall_10", {})
            sim = agg.get("semantic_similarity", {})
            lat = agg.get("latency_ms", {})
            key = (
                f"R@10={_fmt(r10.get('mean'), '.4f')}, "
                f"sim={_fmt(sim.get('mean'), '.4f')}, "
                f"lat={_fmt(lat.get('mean'), '.1f')}ms"
            )
        elif sr["suite"] == "beir" and isinstance(res, dict):
            datasets = res.get("datasets", [])
            results_list = res.get("results", [])
            n_datasets = len(datasets)
            key_parts = [f"{n_datasets} datasets"]
            for dsr in results_list[:3]:
                ds_name = dsr.get("dataset", "?")
                key_parts.append(ds_name)
            key = ", ".join(key_parts)
        else:
            key = "N/A"

        lines.append(f"| {sr['suite']} | {status} | {key} |")

    lines.extend([
        "",
        "## Detailed Results",
        "",
    ])

    for sr in suite_results:
        lines.append(f"### {sr['suite']}")
        lines.append("")
        if isinstance(sr.get("results"), dict):
            lines.append("```json")
            lines.append(json.dumps(sr["results"], indent=2))
            lines.append("```")
        else:
            lines.append(f"Status: {sr['status']}")
        lines.append("")

    report_path = RESULTS_DIR / "report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return report_path


def main() -> int:
    suite_results: list[dict[str, Any]] = []
    for name in SUITES:
        suite_results.append(run_suite(name))

    report_path = generate_report(suite_results)
    print(f"\n[INFO] Unified report written to {report_path}")

    all_ok = all(r["status"] == "success" for r in suite_results)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
