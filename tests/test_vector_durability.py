"""Durability and mirror-consistency tests for the vector backends.

``vec_fallback`` is the source of truth for embeddings: the fusion rerank, L2
interference detection and L3 eviction all read it. A backend that does not
maintain it silently disables those three subsystems, and a backend that keeps
its index only in memory loses every vector written during a session.
"""

import sqlite3

import numpy as np
import pytest

from lummenna.config import LummennaConfig
from lummenna.data.schema import init_db
from lummenna.force.mnemonic.retrieval_dense import (
    SqliteVecBackend,
    USearchBackend,
    VectorChannel,
)

_DIMS = 32


def unit_vector(seed: int, dims: int = _DIMS) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=dims).astype(np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def store(tmp_path):
    """A connection plus the usearch index path beside it."""
    conn = sqlite3.connect(tmp_path / "lummenna.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield conn, tmp_path / "vectors.usearch"
    conn.close()


def mirror_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM vec_fallback").fetchone()[0]


class TestUSearchDurability:
    def test_vectors_survive_restart_without_flush(self, store):
        """An unclean shutdown must not lose vectors.

        The previous implementation never saved after ``add`` and memory-mapped
        the file read-only, so every vector added in a session was lost on exit
        and retrieval silently degraded to lexical-only.
        """
        conn, path = store
        vector = unit_vector(1)

        first = USearchBackend(conn, path, _DIMS)
        first.add(42, vector)
        assert len(first.search(vector, 5)) == 1

        # No flush: simulate the process dying.
        second = USearchBackend(conn, path, _DIMS)
        assert [h.chunk_id for h in second.search(vector, 5)] == [42]

    def test_vectors_survive_restart_after_flush(self, store):
        conn, path = store
        vector = unit_vector(2)

        first = USearchBackend(conn, path, _DIMS)
        first.add(7, vector)
        first.flush()
        assert path.exists()

        second = USearchBackend(conn, path, _DIMS)
        assert [h.chunk_id for h in second.search(vector, 5)] == [7]

    def test_flush_is_idempotent(self, store):
        conn, path = store
        backend = USearchBackend(conn, path, _DIMS)
        backend.add(1, unit_vector(3))
        backend.flush()
        backend.flush()
        assert path.exists()

    def test_corrupt_index_file_rebuilds_from_mirror(self, store):
        """A damaged cache must not cost data, because SQLite holds the truth."""
        conn, path = store
        vector = unit_vector(4)
        backend = USearchBackend(conn, path, _DIMS)
        backend.add(9, vector)
        backend.flush()

        path.write_bytes(b"not a usearch index")

        rebuilt = USearchBackend(conn, path, _DIMS)
        assert [h.chunk_id for h in rebuilt.search(vector, 5)] == [9]

    def test_mirror_dimension_mismatch_is_skipped(self, store):
        """A stale-dimension row must be logged and ignored, not crash startup."""
        conn, path = store
        conn.execute(
            "CREATE TABLE IF NOT EXISTS vec_fallback ("
            "  chunk_id INTEGER PRIMARY KEY, embedding BLOB NOT NULL)"
        )
        conn.execute(
            "INSERT INTO vec_fallback(chunk_id, embedding) VALUES (?,?)",
            (1, np.ones(8, dtype=np.float32).tobytes()),
        )
        conn.commit()

        backend = USearchBackend(conn, path, _DIMS)
        assert backend.search(unit_vector(5), 5) == []


class TestUSearchMaintainsMirror:
    def test_add_writes_the_mirror(self, store):
        """Without this, FRQAD rerank, L2 and L3 all silently no-op."""
        conn, path = store
        backend = USearchBackend(conn, path, _DIMS)
        backend.add(1, unit_vector(6))

        assert mirror_count(conn) == 1

    def test_remove_clears_the_mirror(self, store):
        conn, path = store
        backend = USearchBackend(conn, path, _DIMS)
        backend.add(1, unit_vector(7))
        backend.remove(1)

        assert mirror_count(conn) == 0
        assert backend.search(unit_vector(7), 5) == []

    def test_add_is_idempotent_for_the_same_chunk(self, store):
        conn, path = store
        backend = USearchBackend(conn, path, _DIMS)
        backend.add(1, unit_vector(8))
        backend.add(1, unit_vector(9))

        assert mirror_count(conn) == 1
        assert len(backend.search(unit_vector(9), 5)) == 1

    def test_degrade_retains_a_searchable_vector(self, store):
        """Degradation re-quantizes; it must not delete the vector."""
        conn, path = store
        backend = USearchBackend(conn, path, _DIMS)
        original = unit_vector(10)
        backend.add(5, original)

        backend.degrade(5, "INT8")

        assert mirror_count(conn) == 1
        assert [h.chunk_id for h in backend.search(original, 5)] == [5]
        stored = np.frombuffer(
            conn.execute("SELECT embedding FROM vec_fallback WHERE chunk_id = 5").fetchone()[0],
            dtype=np.float32,
        )
        assert stored.size == _DIMS
        assert not np.array_equal(stored, original)


class TestBackendParity:
    """Both backends must honour the same contract."""

    @pytest.fixture(params=["sqlite-vec", "usearch"])
    def channel(self, request, tmp_path):
        conn = sqlite3.connect(tmp_path / "lummenna.db")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        config = LummennaConfig(
            vector_index=request.param,
            embedding_dims=_DIMS,
            store_path=tmp_path,
        )
        yield VectorChannel(config, conn), conn
        conn.close()

    def test_add_then_search_finds_the_chunk(self, channel):
        vector_channel, _ = channel
        vector = unit_vector(11)
        vector_channel.add(3, vector)

        assert 3 in {h.chunk_id for h in vector_channel.search(vector, 5)}

    def test_add_populates_the_mirror(self, channel):
        vector_channel, conn = channel
        vector_channel.add(3, unit_vector(12))

        assert mirror_count(conn) == 1

    def test_flush_is_available_on_every_backend(self, channel):
        vector_channel, _ = channel
        vector_channel.add(1, unit_vector(13))
        vector_channel.flush()

    def test_remove_is_reflected_in_the_mirror(self, channel):
        vector_channel, conn = channel
        vector_channel.add(1, unit_vector(14))
        vector_channel.remove(1)

        assert mirror_count(conn) == 0


class TestSqliteVecBackend:
    def test_flush_is_a_noop(self, store):
        conn, _ = store
        backend = SqliteVecBackend(conn, _DIMS)
        backend.add(1, unit_vector(15))
        backend.flush()

        assert mirror_count(conn) == 1
