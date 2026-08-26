"""Tests for GraphChannel wired into SearchPipeline and fusion."""

import hashlib

import numpy as np

from lummenna.force.mnemonic.retrieval_dense import DenseHit
from lummenna.force.mnemonic.retrieval_graph import GraphChannel, GraphHit
from lummenna.force.mnemonic.retrieval_lexical import LexicalChannel, LexicalHit
from lummenna.fusion import fuse_and_rerank
from lummenna.search import SearchPipeline


def _setup_db(conn):
    """Insert test room, locus, chunks, embeddings, and FTS index."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vec_fallback (
            chunk_id INTEGER PRIMARY KEY,
            embedding BLOB NOT NULL
        )
    """)
    cur = conn.execute("INSERT INTO room (name, room_type) VALUES (?, ?)", ("test_room", "domain"))
    room_id = cur.lastrowid
    cur = conn.execute("INSERT INTO locus (room_id, name) VALUES (?, ?)", (room_id, "test_locus"))
    locus_id = cur.lastrowid

    for i in range(1, 4):
        content = f"chunk content {i}"
        conn.execute(
            """INSERT INTO chunk (chunk_id, locus_id, room_id, content, content_hash, created_at, vm_score, resolution)
               VALUES (?, ?, ?, ?, ?, unixepoch(), 0.5, 'FP32')""",
            (i, locus_id, room_id, content, hashlib.sha256(content.encode()).hexdigest()),
        )
        vec = np.random.randn(384).astype(np.float32)
        conn.execute(
            "INSERT INTO vec_fallback (chunk_id, embedding) VALUES (?, ?)",
            (i, vec.tobytes()),
        )
        LexicalChannel(conn).index_chunk(i, content)

    conn.commit()
    return room_id, locus_id


def test_fuse_and_rerank_includes_graph_hits(memory_db):
    _setup_db(memory_db)
    lexical = [LexicalHit(chunk_id=1, rank=1.0, match_info=b"")]
    dense = []
    graph_hits = [GraphHit(chunk_id=2, depth=1, path=[1, 2])]

    results = fuse_and_rerank(lexical, dense, [], memory_db, graph_hits=graph_hits)
    ids = [r.chunk_id for r in results]

    assert 1 in ids
    assert 2 in ids


def test_graph_hits_score_boost(memory_db):
    _setup_db(memory_db)
    lexical = [LexicalHit(chunk_id=1, rank=1.0, match_info=b"")]
    dense = []

    results_no_graph = fuse_and_rerank(lexical, dense, [], memory_db, graph_hits=None)
    rrf_no_graph = next((r.rrf_score for r in results_no_graph if r.chunk_id == 1), 0.0)

    graph_hits = [GraphHit(chunk_id=1, depth=1, path=[1])]
    results_with_graph = fuse_and_rerank(lexical, dense, [], memory_db, graph_hits=graph_hits)
    rrf_with_graph = next((r.rrf_score for r in results_with_graph if r.chunk_id == 1), 0.0)

    assert rrf_with_graph > rrf_no_graph


def test_traverse_from_seed_missing_node_does_not_raise(memory_db):
    """Regression: a seed absent from the graph snapshot must not crash.

    The NetworkX graph is a snapshot built at construction, while dense-channel
    seeds come from the vector index. When the two drift (e.g. a chunk stored
    after the snapshot, or tombstoned since), ``DiGraph.neighbors`` would raise
    ``NetworkXError: The node <id> is not in the digraph`` and abort search /
    assemble. Traversal must instead degrade to zero graph-channel hits.
    """
    _setup_db(memory_db)
    graph = GraphChannel(memory_db)
    # Only the NetworkX path exercises the previously-unguarded neighbors() call.
    if graph._nx is None or not hasattr(graph, "_graph"):
        import pytest

        pytest.skip("networkx unavailable; sqlite fallback path is already guarded")

    # 999 exists in neither the chunk table nor the graph snapshot — exactly the
    # vector/graph drift that triggered the production error.
    assert 999 not in graph._graph
    hits = graph.traverse_from_seed(999, hops=2)
    assert hits == []

    # A valid in-snapshot seed still traverses normally (no regression).
    valid_hits = graph.traverse_from_seed(1, hops=2)
    assert isinstance(valid_hits, list)


def test_search_pipeline_with_graph_boosts_results(
    monkeypatch, memory_db, test_config, mock_embedder
):
    _setup_db(memory_db)

    monkeypatch.setattr(
        "lummenna.search.LexicalChannel.search",
        lambda self, query, k=20: [LexicalHit(chunk_id=1, rank=1.0, match_info=b"")],
    )
    monkeypatch.setattr(
        "lummenna.search.VectorChannel.search",
        lambda self, vec, k=20: [DenseHit(chunk_id=1, score=1.0, vector=np.array([]))],
    )

    pipe_no_graph = SearchPipeline(memory_db, test_config, embedder=mock_embedder)
    results_no_graph = pipe_no_graph.execute("query")
    ids_no_graph = [r.chunk_id for r in results_no_graph]

    graph = GraphChannel(memory_db)
    pipe_with_graph = SearchPipeline(memory_db, test_config, embedder=mock_embedder, graph=graph)
    results_with_graph = pipe_with_graph.execute("query")
    ids_with_graph = [r.chunk_id for r in results_with_graph]

    assert ids_no_graph == [1]
    assert 1 in ids_with_graph
    assert 2 in ids_with_graph or 3 in ids_with_graph
