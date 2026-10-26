"""Tests for artifact-backed dashboard evaluation data."""

import json

from lummenna import bench_results


def test_committed_evaluation_summary_is_scoped_and_unranked():
    summary = bench_results.load_evaluation_summary()

    assert summary["status"] == "subset_evidence_only"
    assert summary["official_leaderboard_rank"] is None
    assert summary["external_system_comparison_available"] is False

    retrieval = summary["retrieval"]
    assert retrieval["available"] is True
    assert retrieval["corpus"] == "synthetic"
    assert retrieval["passages"] == 1000
    assert retrieval["queries"] == 50
    assert retrieval["model"] == "BAAI/bge-small-en-v1.5"
    assert retrieval["recall_at_10"]["hybrid"] == 0.1487
    assert retrieval["latency_mean_ms"]["hybrid"] == 13.9371
    assert len(retrieval["artifact_sha256"]) == 64

    beir = summary["beir_subset"]
    assert beir["datasets"] == ["nfcorpus", "scifact", "fiqa", "arguana", "scidocs"]
    assert beir["hybrid_ndcg_at_10_macro"] == 0.4394

    e2e = summary["e2e_memory"]
    assert e2e["sessions"] == 7
    assert e2e["queries"] == 28
    assert e2e["recall_at_1"] == 0.9643


def test_retrieval_loader_rejects_wrong_artifact_type(tmp_path, monkeypatch):
    artifact = tmp_path / "retrieval_results.json"
    artifact.write_text(json.dumps({"benchmark": "not-retrieval", "embedders": []}))
    monkeypatch.setattr(bench_results, "_artifact_candidates", lambda *_args: [artifact])

    result = bench_results.load_retrieval_benchmarks()

    assert result["available"] is False
    assert result["recall_at_10"]["hybrid"] is None


def test_retrieval_loader_rejects_out_of_range_metrics(tmp_path, monkeypatch):
    artifact = tmp_path / "retrieval_results.json"
    artifact.write_text(
        json.dumps(
            {
                "benchmark": "retrieval",
                "corpus": "fixture",
                "num_passages": 10,
                "num_queries": 2,
                "num_seeds": 1,
                "bootstrap_samples": 10,
                "embedders": [
                    {
                        "embedder_full": "BAAI/bge-small-en-v1.5",
                        "results": {
                            "bm25_only": {"recall_10": {"mean": -1}},
                            "dense_only": {"recall_10": {"mean": 2}},
                            "hybrid": {
                                "recall_10": {"mean": "nan"},
                                "latency_ms": {"mean": -5},
                            },
                        },
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(bench_results, "_artifact_candidates", lambda *_args: [artifact])

    result = bench_results.load_retrieval_benchmarks()

    assert result["available"] is False
    assert result["recall_at_10"] == {"bm25": None, "dense": None, "hybrid": None}
    assert result["latency_mean_ms"]["hybrid"] is None


def test_e2e_loader_requires_metrics_to_survive_validation(tmp_path, monkeypatch):
    """A dict-shaped `aggregate_metrics` is not evidence on its own.

    Reporting available=True while every metric validated to None would put
    "measured" next to a column of N/A on the dashboard.
    """
    artifact = tmp_path / "e2e_results.json"
    artifact.write_text(
        json.dumps(
            {
                "benchmark": "e2e_memory_quality",
                "embedder": "fixture-model",
                "num_sessions": 3,
                "num_query_turns": 9,
                "aggregate_metrics": {
                    "recall_1": {"mean": 7.5},  # out of [0, 1]
                    "recall_5": {"mean": "nan"},
                    "latency_ms": {"mean": -2},  # negative
                },
            }
        )
    )
    monkeypatch.setattr(bench_results, "_artifact_candidates", lambda *_args: [artifact])

    result = bench_results._load_e2e_summary()

    assert result["available"] is False
    assert result["recall_at_1"] is None
    assert result["latency_mean_ms"] is None


def test_e2e_loader_accepts_a_valid_artifact(tmp_path, monkeypatch):
    artifact = tmp_path / "e2e_results.json"
    artifact.write_text(
        json.dumps(
            {
                "benchmark": "e2e_memory_quality",
                "embedder": "fixture-model",
                "num_sessions": 3,
                "num_query_turns": 9,
                "aggregate_metrics": {
                    "recall_1": {"mean": 0.5},
                    "recall_5": {"mean": 0.8},
                    "latency_ms": {"mean": 4.25},
                },
            }
        )
    )
    monkeypatch.setattr(bench_results, "_artifact_candidates", lambda *_args: [artifact])

    result = bench_results._load_e2e_summary()

    assert result["available"] is True
    assert result["recall_at_1"] == 0.5
    assert result["latency_mean_ms"] == 4.25


def test_beir_loader_rejects_malformed_result_shape(tmp_path, monkeypatch):
    artifact = tmp_path / "beir_results.json"
    artifact.write_text(
        json.dumps(
            {
                "benchmark": "beir",
                "results": [
                    {
                        "dataset": "fixture",
                        "embedders": [
                            {
                                "embedder_full": "BAAI/bge-small-en-v1.5",
                                "results": [],
                            }
                        ],
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(bench_results, "_artifact_candidates", lambda *_args: [artifact])

    result = bench_results._load_beir_summary()

    assert result["available"] is False
