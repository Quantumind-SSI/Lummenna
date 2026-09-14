"""Tests for per-connection retrieval-channel caching on the write path.

Constructing a vector channel loads the sqlite-vec extension, issues two
``CREATE TABLE IF NOT EXISTS`` statements and probes the KNN grammar; the
lexical channel runs ``PRAGMA optimize``. Doing that per write put schema
changes inside the caller's data transaction and repeated the extension load on
every store.
"""

import sqlite3
import threading

import numpy as np
import pytest

import lummenna.force.mnemonic.retrieval_dense as retrieval_dense
import lummenna.force.mnemonic.retrieval_lexical as retrieval_lexical
from lummenna.config import LummennaConfig
from lummenna.data.schema import init_db
from lummenna.force.mnemonic.store import (
    _CHANNEL_CACHE_MAX,
    _get_lexical_channel,
    _get_vector_channel,
    reset_channel_cache,
    store_memory,
)


@pytest.fixture(autouse=True)
def clean_cache():
    reset_channel_cache()
    yield
    reset_channel_cache()


@pytest.fixture
def conn():
    # check_same_thread=False mirrors lummenna.data.schema.get_connection: the
    # scheduler and request threads share one connection in production, so the
    # cache must be exercised under the same conditions.
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def config():
    return LummennaConfig(vector_index="sqlite-vec", device="generic")


def unit_vector(seed: int, dims: int = 384) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=dims).astype(np.float32)
    return v / np.linalg.norm(v)


class TestChannelReuse:
    def test_vector_channel_is_reused_for_one_connection(self, conn, config):
        first = _get_vector_channel(conn, config)
        second = _get_vector_channel(conn, config)

        assert first is second

    def test_lexical_channel_is_reused_for_one_connection(self, conn):
        assert _get_lexical_channel(conn) is _get_lexical_channel(conn)

    def test_distinct_connections_get_distinct_channels(self, config):
        first_conn = sqlite3.connect(":memory:")
        second_conn = sqlite3.connect(":memory:")
        init_db(first_conn)
        init_db(second_conn)
        try:
            assert _get_vector_channel(first_conn, config) is not _get_vector_channel(
                second_conn, config
            )
        finally:
            first_conn.close()
            second_conn.close()

    def test_reset_forces_reconstruction(self, conn, config):
        first = _get_vector_channel(conn, config)
        reset_channel_cache()

        assert _get_vector_channel(conn, config) is not first


class TestStorePathUsesTheCache:
    def test_repeated_stores_build_one_vector_channel(self, conn, config, monkeypatch):
        builds = []
        original = retrieval_dense.VectorChannel.__init__

        def counting_init(self, cfg, connection):
            builds.append(1)
            original(self, cfg, connection)

        monkeypatch.setattr(retrieval_dense.VectorChannel, "__init__", counting_init)

        for i in range(25):
            store_memory(
                conn,
                content=f"memory number {i}",
                room_name="room",
                embedding=unit_vector(i),
                config=config,
            )

        assert len(builds) == 1

    def test_repeated_stores_build_one_lexical_channel(self, conn, config, monkeypatch):
        builds = []
        original = retrieval_lexical.LexicalChannel.__init__

        def counting_init(self, connection):
            builds.append(1)
            original(self, connection)

        monkeypatch.setattr(retrieval_lexical.LexicalChannel, "__init__", counting_init)

        for i in range(10):
            store_memory(
                conn,
                content=f"lexical memory {i}",
                room_name="room",
                embedding=unit_vector(i),
                config=config,
            )

        assert len(builds) == 1

    def test_caching_does_not_change_stored_data(self, conn, config):
        for i in range(12):
            store_memory(
                conn,
                content=f"durable memory {i}",
                room_name="room",
                embedding=unit_vector(i),
                config=config,
            )

        chunks = conn.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
        vectors = conn.execute("SELECT COUNT(*) FROM vec_fallback").fetchone()[0]
        indexed = conn.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0]

        assert chunks == vectors == indexed == 12

    def test_store_without_embedding_still_indexes_lexically(self, conn, config):
        store_memory(conn, content="no vector here", room_name="room", config=config)

        assert conn.execute("SELECT COUNT(*) FROM chunk").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0] == 1


class TestCacheBounds:
    def test_cache_is_bounded(self, config):
        """A process opening short-lived connections must not grow the cache."""
        connections = []
        try:
            for _ in range(_CHANNEL_CACHE_MAX + 5):
                connection = sqlite3.connect(":memory:")
                init_db(connection)
                connections.append(connection)
                _get_vector_channel(connection, config)

            from lummenna.force.mnemonic.store import _vector_channels

            assert len(_vector_channels) <= _CHANNEL_CACHE_MAX
        finally:
            for connection in connections:
                connection.close()

    def test_concurrent_lookups_converge_on_one_channel(self, conn, config):
        """Racing threads must all end up with the same cached instance."""
        seen: list[object] = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            seen.append(_get_vector_channel(conn, config))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len({id(channel) for channel in seen}) == 1
