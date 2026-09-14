"""Stress test: 100k+ chunk ingestion, multi-threaded query throughput,
and L3 budget eviction under RSS pressure.

Run with:
    python -m benchmarks.stress.run

Embedder honesty: by default the real embedding model is required, because
ingest throughput measured without real embedding cost misstates real-world
performance. `--mock-embedder` is available for storage-only stress testing,
but the resulting artifact is explicitly labeled
"mock (embedding cost EXCLUDED)" and must never be cited as end-to-end
ingest throughput.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

from benchmarks.provenance import write_artifact
from lummenna.config import LummennaConfig
from lummenna.data.schema import get_connection, init_db
from lummenna.force.contextual.embed import MockEmbedder
from lummenna.force.mnemonic.forgetting_l3_budget import budget_curated_eviction
from lummenna.force.mnemonic.store import store_memory
from lummenna.search import SearchPipeline

EMBEDDER_MODEL = "all-MiniLM-L6-v2"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _get_real_embedder(dims: int):
    """Return a real embedder or raise (no silent mock substitution)."""
    errors = []
    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(EMBEDDER_MODEL)

        class _Real:
            def encode(self, texts):
                return np.asarray(
                    model.encode(texts, normalize_embeddings=True, show_progress_bar=False),
                    dtype=np.float32,
                )

            def encode_single(self, text):
                return self.encode([text])[0]

        print(f"[INFO] Stress benchmark using real embedder: {EMBEDDER_MODEL}")
        return _Real()
    except Exception as exc:
        errors.append(f"sentence-transformers: {exc}")

    try:
        from lummenna.force.contextual.embed import LocalEmbedder

        model_dir = LummennaConfig().model_path / EMBEDDER_MODEL
        if model_dir.exists():
            print(f"[INFO] Stress benchmark using local ONNX embedder: {model_dir}")
            return LocalEmbedder(model_dir, dims=dims)
        errors.append(f"local ONNX: no model directory at {model_dir}")
    except Exception as exc:
        errors.append(f"local ONNX: {exc}")

    raise RuntimeError(
        "No real embedder available. Refusing to run the stress benchmark "
        "with mock embeddings by default (throughput would exclude embedding "
        "cost). Pass --mock-embedder for a storage-only run — the artifact "
        f"will be labeled accordingly. Errors: {'; '.join(errors)}"
    )


def _make_config(store_path: Path) -> LummennaConfig:
    return LummennaConfig(
        store_path=store_path,
        model_path=store_path / "models",
        vector_index="sqlite-vec",
        device="generic",
        memory_limit_mb=256,
        context_budget=2048,
    )


def _ingest_chunks(conn: sqlite3.Connection, config: LummennaConfig, embedder, count: int):
    t0 = time.perf_counter()
    for i in range(count):
        content = f"stress_test_chunk_{i:06d} " + "word " * 20
        store_memory(
            conn,
            content=content,
            room_name="stress",
            config=config,
            embedding=embedder.encode_single(content),
        )
        if i % 10000 == 0 and i > 0:
            elapsed = time.perf_counter() - t0
            print(f"  Ingested {i} chunks in {elapsed:.1f}s ({i/elapsed:.0f} chunks/s)")
    total = time.perf_counter() - t0
    print(f"  Total ingestion: {count} chunks in {total:.1f}s ({count/total:.0f} chunks/s)")
    return total


def _query_throughput(conn: sqlite3.Connection, config: LummennaConfig, embedder, queries: int, workers: int):
    queries_list = [f"stress_test_chunk_{i % 100:03d}" for i in range(queries)]

    def _run(q: str) -> float:
        # Each worker gets its own connection because sqlite3 is not thread-safe
        tconn = get_connection(config)
        pipeline = SearchPipeline(tconn, config, embedder=embedder)
        t0 = time.perf_counter()
        pipeline.execute(q, k=10)
        elapsed = time.perf_counter() - t0
        tconn.close()
        return elapsed

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        latencies = list(ex.map(_run, queries_list))
    total = time.perf_counter() - t0
    latencies_ms = [lat * 1000 for lat in latencies]
    latencies_ms.sort()
    p50 = latencies_ms[len(latencies_ms) // 2]
    p99 = latencies_ms[int(len(latencies_ms) * 0.99)]
    print(f"  Query throughput: {queries} queries in {total:.1f}s ({queries/total:.0f} qps)")
    print(f"  Latency p50={p50:.1f}ms p99={p99:.1f}ms")
    return total, p50, p99


def _trigger_l3_eviction(conn: sqlite3.Connection, config: LummennaConfig):
    """Trigger L3 eviction by lowering the memory limit so chunk count exceeds 85%% threshold."""
    # After ingestion we know the chunk count. Compute a limit that forces eviction.
    row = conn.execute("SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL").fetchone()
    active_chunks = row[0] if row else 0
    # estimated_mb = active_chunks * 1800 / (1024*1024)
    # target_ram_mb = memory_limit_mb * 0.85
    # Solve for memory_limit_mb where estimated_mb == target_ram_mb
    estimated_mb = (active_chunks * 1800) / (1024 * 1024)
    forced_limit_mb = max(16, int(estimated_mb / 0.85 * 0.5))  # 50%% of the 85%% threshold
    print(f"  Triggering L3 eviction (chunks={active_chunks}, est_mb={estimated_mb:.1f}, forced_limit={forced_limit_mb}MB)...")
    # Temporarily override config.memory_limit_mb
    original_limit = config.memory_limit_mb
    config.memory_limit_mb = forced_limit_mb
    try:
        evicted = budget_curated_eviction(conn, config)
    finally:
        config.memory_limit_mb = original_limit
    print(f"  L3 evicted {evicted} chunks")
    return evicted


def main() -> int:
    parser = argparse.ArgumentParser(description="Lummenna stress benchmark")
    parser.add_argument("--chunks", type=int, default=100_000, help="Number of chunks to ingest")
    parser.add_argument("--queries", type=int, default=1_000, help="Number of query requests")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent query workers")
    parser.add_argument(
        "--mock-embedder",
        action="store_true",
        help=(
            "Storage-only stress run with fake embeddings. The artifact is "
            "labeled 'mock (embedding cost EXCLUDED)' and must not be cited "
            "as end-to-end ingest throughput."
        ),
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmpdir:
        store = Path(tmpdir) / "store"
        store.mkdir()
        config = _make_config(store)
        conn = get_connection(config)
        init_db(conn)
        if args.mock_embedder:
            print("[WARN] --mock-embedder: embedding cost EXCLUDED from all timings")
            embedder = MockEmbedder(dims=config.embedding_dims)
            embedder_label = "mock (embedding cost EXCLUDED)"
        else:
            embedder = _get_real_embedder(config.embedding_dims)
            embedder_label = EMBEDDER_MODEL

        print(f"--- Stress Test: {args.chunks} chunks, {args.queries} queries ---")

        tracemalloc.start()
        ingest_time = _ingest_chunks(conn, config, embedder, args.chunks)

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"  Memory current={current/1024/1024:.1f}MB peak={peak/1024/1024:.1f}MB")

        chunk_count = conn.execute("SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL").fetchone()[0]
        print(f"  Active chunks in DB: {chunk_count}")

        query_time, p50, p99 = _query_throughput(conn, config, embedder, args.queries, args.workers)

        evicted = _trigger_l3_eviction(conn, config)

        result = {
            "benchmark": "stress",
            "embedder": embedder_label,
            "chunks_ingested": args.chunks,
            "ingest_time_s": round(ingest_time, 2),
            "ingest_rate_hz": round(args.chunks / ingest_time, 1),
            "query_count": args.queries,
            "query_time_s": round(query_time, 2),
            "query_rate_hz": round(args.queries / query_time, 1),
            "latency_p50_ms": round(p50, 2),
            "latency_p99_ms": round(p99, 2),
            "peak_traced_memory_mb": round(peak / 1024 / 1024, 1),
            "l3_evicted": evicted,
        }
        print(f"\nResults: {result}")

        artifact = RESULTS_DIR / "stress_results.json"
        # Throughput figures are hardware-bound; record the host so no surface
        # has to assert a platform the artifact does not contain.
        digest = write_artifact(artifact, result)
        print(f"[INFO] Results written to {artifact} (sha256={digest})")
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
