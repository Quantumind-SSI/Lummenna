"""CLI for the memory-framework leaderboard.

Reproduce any published result::

    python -m benchmarks.leaderboard.run --dataset locomo --max-questions 200

Validate the harness offline, without downloading anything::

    python -m benchmarks.leaderboard.run --dataset fixture

Exit codes:
    0  The run completed and an artifact was written. Individual adapters may
       still be NOT_RUN; that is a recorded result, not a failure.
    1  The dataset could not be loaded, so nothing could be measured.
    2  Invalid arguments.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from benchmarks.leaderboard.adapter import MemoryAdapter
from benchmarks.leaderboard.adapters.baselines import BM25Baseline, RecencyBaseline
from benchmarks.leaderboard.adapters.lummenna_adapter import LummennaAdapter
from benchmarks.leaderboard.datasets import (
    DatasetUnavailableError,
    MemoryDataset,
    load_fixture,
    load_locomo,
    load_longmemeval,
)
from benchmarks.leaderboard.report import render_markdown
from benchmarks.leaderboard.runner import run_leaderboard
from benchmarks.provenance import capture_environment, describe_platform, write_artifact

RESULTS_DIR = Path(__file__).resolve().parent / "results"
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "smoke.json"

DATASETS = ("locomo", "longmemeval", "fixture")


def build_adapters(include_lummenna: bool = True) -> list[MemoryAdapter]:
    """Return the adapters shipped in-tree.

    Baselines come first so they anchor the table. Third-party frameworks are
    intentionally absent: see ``benchmarks/leaderboard/CONTRIBUTING.md`` for why
    adapters are contributed by the people who maintain each system.

    Args:
        include_lummenna: Set False to produce a baselines-only artifact, which
            is useful for verifying the harness is not tuned around Lummenna.

    Returns:
        Adapter instances in table order.
    """
    adapters: list[MemoryAdapter] = [BM25Baseline(), RecencyBaseline()]
    if include_lummenna:
        adapters.append(LummennaAdapter())
    return adapters


def load_dataset(name: str, max_questions: int | None, seed: int) -> MemoryDataset:
    """Load one evaluation dataset by name.

    Raises:
        DatasetUnavailableError: If the dataset cannot be loaded.
        ValueError: If ``name`` is not a known dataset.
    """
    if name == "locomo":
        return load_locomo(max_questions=max_questions, seed=seed)
    if name == "longmemeval":
        return load_longmemeval(max_questions=max_questions, seed=seed)
    if name == "fixture":
        return load_fixture(FIXTURE_PATH)
    raise ValueError(f"unknown dataset '{name}'; choose from {', '.join(DATASETS)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchmarks.leaderboard.run",
        description="Reproducible memory-framework retrieval leaderboard.",
        epilog=(
            "This leaderboard is published by the maintainer of Lummenna, one of "
            "the ranked systems. It is not official and is not independently "
            "audited. Every artifact carries that disclosure."
        ),
    )
    parser.add_argument("--dataset", choices=DATASETS, default="fixture")
    parser.add_argument(
        "--max-questions",
        type=int,
        default=None,
        help="Cap questions after a seeded shuffle. Omit to evaluate all.",
    )
    parser.add_argument("--top-k", type=int, default=10, help="Items requested per question.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--no-lummenna",
        action="store_true",
        help="Run baselines only, to check the harness is not tuned around Lummenna.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=RESULTS_DIR,
        help="Where to write the artifact and report.",
    )
    args = parser.parse_args(argv)

    if args.top_k <= 0:
        parser.error("--top-k must be positive")
    if args.max_questions is not None and args.max_questions <= 0:
        parser.error("--max-questions must be positive")

    try:
        dataset = load_dataset(args.dataset, args.max_questions, args.seed)
    except (DatasetUnavailableError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        print(
            "[ERROR] No artifact written. A leaderboard entry is never "
            "synthesised from an unavailable dataset.",
            file=sys.stderr,
        )
        return 1

    print(
        f"[INFO] dataset={dataset.name} items={len(dataset.items)} "
        f"questions={len(dataset.questions)} fingerprint={dataset.fingerprint[:16]}"
    )

    adapters = build_adapters(include_lummenna=not args.no_lummenna)
    payload = run_leaderboard(dataset, adapters, top_k=args.top_k, seed=args.seed)
    # Capture once so the artifact and the rendered report describe the same
    # environment rather than two captures taken seconds apart.
    payload["environment"] = capture_environment()

    artifact_path = args.output_dir / f"leaderboard_{dataset.name.replace(':', '_')}.json"
    digest = write_artifact(artifact_path, payload)

    report_path = artifact_path.with_suffix(".md")
    report_path.write_text(render_markdown(payload, digest), encoding="utf-8")

    summary = payload["summary"]
    print(
        f"[INFO] scored={summary['adapters_scored']} "
        f"not_run={summary['adapters_not_run']} errored={summary['adapters_errored']}"
    )
    for run in payload["results"]:
        if run["status"] != "OK":
            print(f"[INFO]   {run['framework']}: {run['status']} - {run['reason']}")
    print(f"[INFO] artifact={artifact_path} sha256={digest}")
    print(f"[INFO] report={report_path}")
    print(f"[INFO] platform={describe_platform(payload.get('environment'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
