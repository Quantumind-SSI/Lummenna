"""Regression tests for the bounded recency term in fusion reranking.

The final score is a product. An unbounded ``exp(-idle/tau)`` factor therefore
does not rank memories by age, it erases them: at tau=168h a one-year-old chunk
contributes exp(-52) is about 2.3e-23, while every other factor combined spans
barely 145x. These tests pin the invariant that relevance and value can still
outweigh age, which is the entire premise of a memory palace.
"""

import math
import time

import numpy as np
import pytest

from lummenna.config import LummennaConfig
from lummenna.data.schema import init_db
from lummenna.force.mnemonic.retrieval_dense import DenseHit
from lummenna.force.mnemonic.retrieval_lexical import LexicalHit
from lummenna.fusion import RECENCY_FLOOR, RECENCY_TAU_HOURS, fuse_and_rerank

_DIMS = 384


@pytest.fixture
def ranking_db(memory_db):
    """Schema plus the vector mirror and one room/locus to hang chunks off."""
    memory_db.execute(
        "CREATE TABLE IF NOT EXISTS vec_fallback ("
        "  chunk_id INTEGER PRIMARY KEY, embedding BLOB NOT NULL)"
    )
    memory_db.execute("INSERT INTO room(name, room_type) VALUES ('r', 'domain')")
    memory_db.execute("INSERT INTO locus(room_id, name) VALUES (1, 'l')")
    return memory_db


def _insert(conn, chunk_id, content, vm, age_hours, vector, idle_hours=None):
    """Insert one chunk with an explicit age and optional idle time."""
    now = int(time.time())
    created = now - int(age_hours * 3600)
    last_access = None if idle_hours is None else now - int(idle_hours * 3600)
    conn.execute(
        "INSERT INTO chunk(chunk_id, locus_id, room_id, content, content_hash,"
        " vm_score, resolution, created_at, last_access_at)"
        " VALUES (?,1,1,?,?,?,'FP32',?,?)",
        (chunk_id, content, f"h{chunk_id}", vm, created, last_access),
    )
    conn.execute(
        "INSERT INTO vec_fallback(chunk_id, embedding) VALUES (?,?)",
        (chunk_id, vector.astype(np.float32).tobytes()),
    )


def _aligned_vector():
    v = np.ones(_DIMS, dtype=np.float32)
    return v / np.linalg.norm(v)


def _orthogonal_vector():
    v = np.zeros(_DIMS, dtype=np.float32)
    v[0] = 1.0
    return v


class TestBoundedRecency:
    def test_old_relevant_memory_beats_new_irrelevant_one(self, ranking_db):
        """A year-old perfect match must outrank one-hour-old noise.

        This is the exact scenario the unbounded exponential inverted, by a
        factor of roughly 3e20.
        """
        query = _aligned_vector()
        _insert(ranking_db, 1, "the exact answer", vm=1.0, age_hours=8760, vector=query)
        _insert(ranking_db, 2, "unrelated noise", vm=0.0, age_hours=1, vector=_orthogonal_vector())

        results = fuse_and_rerank(
            [LexicalHit(1, -10.0, {}), LexicalHit(2, -0.001, {})],
            [DenseHit(1, 0.0, np.array([])), DenseHit(2, 2.0, np.array([]))],
            [],
            ranking_db,
            query_embedding=query,
        )

        assert results[0].chunk_id == 1
        assert results[0].final_score > results[1].final_score

    def test_recency_multiplier_is_bounded_below_by_floor(self, ranking_db):
        """Arbitrarily old memories keep a non-vanishing score."""
        query = _aligned_vector()
        _insert(ranking_db, 1, "ancient", vm=1.0, age_hours=87600, vector=query)

        results = fuse_and_rerank(
            [LexicalHit(1, -10.0, {})],
            [DenseHit(1, 0.0, np.array([]))],
            [],
            ranking_db,
            query_embedding=query,
        )

        rrf = results[0].rrf_score
        # final = rrf * (vm + 0.1) * (sim + 0.1) * recency, with recency >= floor.
        lower_bound = rrf * (1.0 + 0.1) * (0.0 + 0.1) * RECENCY_FLOOR
        assert results[0].final_score >= lower_bound
        assert results[0].final_score > 0.0

    def test_recency_still_orders_otherwise_identical_chunks(self, ranking_db):
        """Bounding the factor must not make recency inert."""
        query = _aligned_vector()
        _insert(ranking_db, 1, "same text weight", vm=0.5, age_hours=1, vector=query)
        _insert(ranking_db, 2, "same text weight", vm=0.5, age_hours=2000, vector=query)

        results = fuse_and_rerank(
            [LexicalHit(1, -1.0, {}), LexicalHit(2, -1.0, {})],
            [DenseHit(1, 0.0, np.array([])), DenseHit(2, 0.0, np.array([]))],
            [],
            ranking_db,
            query_embedding=query,
        )

        by_id = {r.chunk_id: r.final_score for r in results}
        assert by_id[1] > by_id[2]

    def test_recency_spread_is_within_one_order_of_magnitude(self, ranking_db):
        """The recency factor must not dominate the other ranking signals."""
        spread = 1.0 / RECENCY_FLOOR
        assert spread < 10.0, f"recency spread {spread:.1f}x can overwhelm relevance"

    def test_rehearsal_restores_recency(self, ranking_db):
        """Re-accessing an old memory restores its recency standing.

        ``last_access_at`` is what ``search._record_access`` updates on every
        retrieval, so a rehearsed memory must not be scored as stale.
        """
        query = _aligned_vector()
        # Both created a year ago; chunk 1 was read an hour ago, chunk 2 never.
        _insert(ranking_db, 1, "rehearsed", vm=0.5, age_hours=8760, vector=query, idle_hours=1)
        _insert(ranking_db, 2, "neglected", vm=0.5, age_hours=8760, vector=query)

        results = fuse_and_rerank(
            [LexicalHit(1, -1.0, {}), LexicalHit(2, -1.0, {})],
            [DenseHit(1, 0.0, np.array([])), DenseHit(2, 0.0, np.array([]))],
            [],
            ranking_db,
            query_embedding=query,
        )

        by_id = {r.chunk_id: r.final_score for r in results}
        assert by_id[1] > by_id[2]


class TestRecencyConfiguration:
    def test_floor_of_one_disables_recency_shaping(self, ranking_db):
        """With the floor at 1.0 the recency factor is constant.

        The two chunks occupy different ranks, so their RRF differs by
        construction. Dividing it out isolates the remaining factors, which are
        identical apart from recency.
        """
        query = _aligned_vector()
        _insert(ranking_db, 1, "new", vm=0.5, age_hours=1, vector=query)
        _insert(ranking_db, 2, "old", vm=0.5, age_hours=90000, vector=query)

        hits = ([LexicalHit(1, -1.0, {}), LexicalHit(2, -1.0, {})], [])

        flat = fuse_and_rerank(
            *hits,
            [],
            ranking_db,
            query_embedding=query,
            config=LummennaConfig(recency_floor=1.0),
        )
        flat_ratios = {r.chunk_id: r.final_score / r.rrf_score for r in flat}
        assert flat_ratios[1] == pytest.approx(flat_ratios[2], rel=1e-9)

        shaped = fuse_and_rerank(
            *hits,
            [],
            ranking_db,
            query_embedding=query,
            config=LummennaConfig(),
        )
        shaped_ratios = {r.chunk_id: r.final_score / r.rrf_score for r in shaped}
        assert shaped_ratios[1] > shaped_ratios[2]

    def test_invalid_tau_is_rejected(self, ranking_db):
        with pytest.raises(ValueError, match="recency_tau_hours"):
            fuse_and_rerank(
                [LexicalHit(1, -1.0, {})],
                [],
                [],
                ranking_db,
                config=LummennaConfig(recency_tau_hours=0.0),
            )

    def test_invalid_floor_is_rejected(self, ranking_db):
        with pytest.raises(ValueError, match="recency_floor"):
            fuse_and_rerank(
                [LexicalHit(1, -1.0, {})],
                [],
                [],
                ranking_db,
                config=LummennaConfig(recency_floor=1.5),
            )

    def test_defaults_match_module_constants(self):
        config = LummennaConfig()
        assert config.recency_tau_hours == RECENCY_TAU_HOURS
        assert config.recency_floor == RECENCY_FLOOR

    def test_boost_shape_matches_documented_formula(self):
        """floor + (1 - floor) * exp(-idle/tau), evaluated at the endpoints."""
        at_zero = RECENCY_FLOOR + (1 - RECENCY_FLOOR) * math.exp(0.0)
        assert at_zero == pytest.approx(1.0)
        at_infinity = RECENCY_FLOOR + (1 - RECENCY_FLOOR) * math.exp(-1e6)
        assert at_infinity == pytest.approx(RECENCY_FLOOR)


class TestCandidatePoolTruncation:
    def test_pool_is_truncated_by_rrf_not_insertion_order(self, memory_db):
        """A high-RRF late-inserted candidate must survive the pool cut.

        Candidates are accumulated lexical, then dense, then graph. Cutting by
        dict order drops high-ranking dense/graph hits in favour of low-ranking
        lexical ones once the channels propose more than ``budget_candidates``.
        """
        init_db(memory_db)
        memory_db.execute("INSERT INTO room(name, room_type) VALUES ('r', 'domain')")
        memory_db.execute("INSERT INTO locus(room_id, name) VALUES (1, 'l')")
        now = int(time.time())
        for cid in (1, 2, 3):
            memory_db.execute(
                "INSERT INTO chunk(chunk_id, locus_id, room_id, content, content_hash,"
                " vm_score, resolution, created_at) VALUES (?,1,1,?,?,0.5,'FP32',?)",
                (cid, f"c{cid}", f"h{cid}", now),
            )

        # Insertion order is 1, 2, 3. Chunk 3 is last to be seen but scores in
        # two channels, so it holds the highest RRF by a clear margin:
        #   chunk 1 -> 1/61                 = 0.01639
        #   chunk 2 -> 1/62                 = 0.01613
        #   chunk 3 -> 1/63 + 1/61          = 0.03227
        lexical = [LexicalHit(1, -0.3, {}), LexicalHit(2, -0.2, {}), LexicalHit(3, -0.1, {})]
        dense = [DenseHit(3, 0.0, np.array([]))]

        results = fuse_and_rerank(lexical, dense, [], memory_db, budget_candidates=1)

        assert [r.chunk_id for r in results] == [3]
