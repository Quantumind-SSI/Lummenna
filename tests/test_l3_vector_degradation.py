"""Tests that L3 eviction degrades vectors rather than discarding them.

The optical ladder is FP32 to FP16 to INT8 to BINARY to RELEASED. Deleting the
vector on the first step turns that four-step ladder into a one-step cliff: the
chunk keeps its full text but becomes permanently invisible to semantic search
and to L2 interference, while freeing no more memory than requantisation would.
Only RELEASED, the terminal state, may remove the vector.
"""

import sqlite3
import time

import numpy as np
import pytest

from lummenna.data.schema import init_db
from lummenna.force.mnemonic.forgetting_l3_budget import _vector_statements

_DIMS = 32


@pytest.fixture
def vec_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vec_fallback ("
        "  chunk_id INTEGER PRIMARY KEY, embedding BLOB NOT NULL)"
    )
    conn.execute("INSERT INTO room(name, room_type) VALUES ('r', 'domain')")
    conn.execute("INSERT INTO locus(room_id, name) VALUES (1, 'l')")
    yield conn
    conn.close()


def seed_chunk(conn, chunk_id: int, vector: np.ndarray | None = None) -> np.ndarray:
    conn.execute(
        "INSERT INTO chunk(chunk_id, locus_id, room_id, content, content_hash,"
        " vm_score, resolution, created_at) VALUES (?,1,1,?,?,0.01,'FP32',?)",
        (chunk_id, f"c{chunk_id}", f"h{chunk_id}", int(time.time())),
    )
    if vector is None:
        vector = np.linspace(-1.0, 1.0, _DIMS, dtype=np.float32)
    conn.execute(
        "INSERT INTO vec_fallback(chunk_id, embedding) VALUES (?,?)",
        (chunk_id, vector.tobytes()),
    )
    conn.commit()
    return vector


def apply(conn, statements) -> None:
    for sql, params in statements:
        conn.execute(sql, params)
    conn.commit()


def stored_vector(conn, chunk_id: int) -> np.ndarray | None:
    row = conn.execute(
        "SELECT embedding FROM vec_fallback WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()
    return None if row is None else np.frombuffer(row[0], dtype=np.float32)


class TestIntermediateStepsPreserveTheVector:
    @pytest.mark.parametrize("resolution", ["FP16", "INT8", "BINARY"])
    def test_degradation_keeps_a_vector(self, vec_db, resolution):
        seed_chunk(vec_db, 1)

        apply(vec_db, _vector_statements(vec_db, 1, resolution, {"vec_fallback"}))

        result = stored_vector(vec_db, 1)
        assert result is not None, f"{resolution} discarded the vector"
        assert result.size == _DIMS

    @pytest.mark.parametrize("resolution", ["FP16", "INT8", "BINARY"])
    def test_degradation_actually_changes_the_vector(self, vec_db, resolution):
        original = seed_chunk(vec_db, 1)

        apply(vec_db, _vector_statements(vec_db, 1, resolution, {"vec_fallback"}))

        assert not np.array_equal(stored_vector(vec_db, 1), original)

    def test_no_delete_statement_is_emitted_for_intermediate_steps(self, vec_db):
        seed_chunk(vec_db, 1)

        statements = _vector_statements(vec_db, 1, "FP16", {"vec_fallback"})

        assert not any("DELETE FROM vec_fallback" in sql for sql, _ in statements)


class TestReleaseRemovesTheVector:
    def test_released_deletes_the_vector(self, vec_db):
        seed_chunk(vec_db, 1)

        apply(vec_db, _vector_statements(vec_db, 1, "RELEASED", {"vec_fallback"}))

        assert stored_vector(vec_db, 1) is None


class TestFallbackPaths:
    def test_missing_mirror_table_emits_nothing(self, vec_db):
        assert _vector_statements(vec_db, 1, "FP16", set()) == []

    def test_absent_vector_falls_back_to_deletion(self, vec_db):
        """A chunk with no stored vector cannot be degraded, only cleaned up."""
        statements = _vector_statements(vec_db, 999, "FP16", {"vec_fallback"})

        assert all("DELETE" in sql for sql, _ in statements)

    def test_vec_chunks_row_is_replaced_not_dropped(self, vec_db):
        """vec0 virtual tables reject in-place UPDATE of the vector column."""
        seed_chunk(vec_db, 1)

        statements = _vector_statements(vec_db, 1, "FP16", {"vec_fallback", "vec_chunks"})
        kinds = [sql.split()[0] for sql, _ in statements]

        assert kinds == ["UPDATE", "DELETE", "INSERT"]


class TestEndToEndEviction:
    def test_eviction_retains_vectors_for_degraded_chunks(self, vec_db):
        from lummenna.config import LummennaConfig
        from lummenna.force.mnemonic.forgetting_l3_budget import budget_curated_eviction

        for chunk_id in range(1, 801):
            seed_chunk(vec_db, chunk_id)

        before = vec_db.execute("SELECT COUNT(*) FROM vec_fallback").fetchone()[0]
        evicted = budget_curated_eviction(vec_db, LummennaConfig(memory_limit_mb=1))
        after = vec_db.execute("SELECT COUNT(*) FROM vec_fallback").fetchone()[0]

        assert evicted > 0
        assert after == before, "degradation must not delete vectors"

        degraded = vec_db.execute(
            "SELECT COUNT(*) FROM chunk WHERE resolution = 'FP16'"
        ).fetchone()[0]
        assert degraded == evicted

    def test_batched_and_immediate_paths_emit_identical_statements(self, vec_db):
        from lummenna.config import LummennaConfig
        from lummenna.force.mnemonic.forgetting_l3_budget import budget_curated_eviction
        from lummenna.sovereign.wear import WearAwareBatcher

        for chunk_id in range(1, 801):
            seed_chunk(vec_db, chunk_id)

        batcher = WearAwareBatcher(vec_db)
        evicted = budget_curated_eviction(
            vec_db, LummennaConfig(memory_limit_mb=1), batcher=batcher
        )

        assert evicted > 0
        queued = [sql for sql, _ in batcher.queue]
        assert any("UPDATE vec_fallback" in sql for sql in queued)
        assert not any("DELETE FROM vec_fallback" in sql for sql in queued)
