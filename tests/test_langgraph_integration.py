"""Tests for lumena.integrations.langgraph."""

import pytest

from lumena.config import LumenaConfig
from lumena.force.contextual.embed import MockEmbedder


@pytest.fixture
def fresh_config(tmp_path):
    cfg = LumenaConfig(
        store_path=tmp_path / "store",
        model_path=tmp_path / "models",
        vector_index="sqlite-vec",
        device="generic",
        # No real ONNX model in tmp_path; tests opt into the explicit mock
        # escape hatch (production default is False and raises).
        allow_mock_embedder=True,
    )
    cfg.store_path.mkdir(parents=True, exist_ok=True)
    cfg.model_path.mkdir(parents=True, exist_ok=True)
    return cfg


class TestLumenaCheckpointSaver:
    def test_put_and_get(self, fresh_config):
        from lumena.integrations.langgraph import LumenaCheckpointSaver

        saver = LumenaCheckpointSaver(config=fresh_config)
        config = {"configurable": {"thread_id": "t1"}}

        checkpoint = {
            "v": 1,
            "id": "cp-1",
            "ts": "2024-01-01T00:00:00Z",
            "channel_values": {"input": "hello"},
            "channel_versions": {},
            "versions_seen": {},
            "pending_sends": [],
        }
        metadata = {"source": "put", "step": 0, "writes": {}, "parents": {}}

        new_config = saver.put(config, checkpoint, metadata, {})
        assert new_config["configurable"]["checkpoint_id"] == "cp-1"

        tup = saver.get_tuple(new_config)
        assert tup is not None
        assert tup.checkpoint["id"] == "cp-1"
        assert tup.metadata["step"] == 0

    def test_list_checkpoints(self, fresh_config):
        from lumena.integrations.langgraph import LumenaCheckpointSaver

        saver = LumenaCheckpointSaver(config=fresh_config)
        config = {"configurable": {"thread_id": "t2"}}

        for i in range(3):
            cp = {
                "v": 1,
                "id": f"cp-{i}",
                "ts": f"2024-01-0{i+1}T00:00:00Z",
                "channel_values": {},
                "channel_versions": {},
                "versions_seen": {},
                "pending_sends": [],
            }
            meta = {"source": "put", "step": i, "writes": {}, "parents": {}}
            saver.put(config, cp, meta, {})

        cps = list(saver.list(config, limit=10))
        assert len(cps) == 3
        # Newest first
        assert cps[0].checkpoint["id"] == "cp-2"
        assert cps[1].checkpoint["id"] == "cp-1"
        assert cps[2].checkpoint["id"] == "cp-0"

    def test_thread_isolation(self, fresh_config):
        from lumena.integrations.langgraph import LumenaCheckpointSaver

        saver = LumenaCheckpointSaver(config=fresh_config)

        for tid in ("tA", "tB"):
            cfg = {"configurable": {"thread_id": tid}}
            cp = {
                "v": 1,
                "id": f"cp-{tid}",
                "ts": "2024-01-01T00:00:00Z",
                "channel_values": {},
                "channel_versions": {},
                "versions_seen": {},
                "pending_sends": [],
            }
            meta = {"source": "put", "step": 0, "writes": {}, "parents": {}}
            saver.put(cfg, cp, meta, {})

        tup_a = saver.get_tuple({"configurable": {"thread_id": "tA"}})
        tup_b = saver.get_tuple({"configurable": {"thread_id": "tB"}})
        assert tup_a.checkpoint["id"] == "cp-tA"
        assert tup_b.checkpoint["id"] == "cp-tB"

    def test_put_writes(self, fresh_config):
        from lumena.integrations.langgraph import LumenaCheckpointSaver

        saver = LumenaCheckpointSaver(config=fresh_config)
        config = {"configurable": {"thread_id": "t3"}}
        # Positional order must match BaseCheckpointSaver.put_writes(config,
        # writes, task_id, task_path) — LangGraph invokes it positionally.
        saver.put_writes(config, [("channel1", "value1"), ("channel2", "value2")], "task-1")

        conn = saver.conn
        rows = conn.execute(
            "SELECT COUNT(*) FROM chunk WHERE content_hash = ?",
            ("lg_write",),
        ).fetchone()
        assert rows[0] == 2

    def test_get_tuple_missing(self, fresh_config):
        from lumena.integrations.langgraph import LumenaCheckpointSaver

        saver = LumenaCheckpointSaver(config=fresh_config)
        result = saver.get_tuple({"configurable": {"thread_id": "missing"}})
        assert result is None


class TestLumenaGraphStore:
    def test_put_and_get(self, fresh_config):
        from lumena.integrations.langgraph import LumenaGraphStore

        store = LumenaGraphStore(config=fresh_config)
        store.put(("users", "alice"), "goal", {"text": "learn french"})

        item = store.get(("users", "alice"), "goal")
        assert item is not None
        assert item["key"] == "goal"
        assert "learn french" in item["value"]

    def test_delete(self, fresh_config):
        from lumena.integrations.langgraph import LumenaGraphStore

        store = LumenaGraphStore(config=fresh_config)
        store.put(("tmp",), "k1", "v1")
        store.delete(("tmp",), "k1")

        item = store.get(("tmp",), "k1")
        assert item is None

    def test_search(self, fresh_config):
        embedder = MockEmbedder(dims=fresh_config.embedding_dims)
        from lumena.integrations.langgraph import LumenaGraphStore

        store = LumenaGraphStore(config=fresh_config, embedder=embedder)
        store.put(("docs",), "doc1", "machine learning is fascinating")
        store.put(("docs",), "doc2", "cloud computing scales well")

        results = store.search(("docs",), "machine learning", limit=5)
        assert len(results) >= 1
        # At least one result should be doc1 (exact semantic match depends on embedder)
        keys = {r["key"] for r in results}
        assert "doc1" in keys

    def test_list_namespaces(self, fresh_config):
        from lumena.integrations.langgraph import LumenaGraphStore

        store = LumenaGraphStore(config=fresh_config, default_namespace="ns")
        store.put(("ns", "a"), "k1", "v1")
        store.put(("ns", "b"), "k2", "v2")

        namespaces = store.list_namespaces(prefix=("ns",))
        assert ("ns", "a") in namespaces
        assert ("ns", "b") in namespaces


class TestCheckpointSaverInterfaceCompat:
    def test_put_writes_signature_matches_langgraph(self):
        """put_writes must accept LangGraph's positional call order.

        Regression test: the saver used to define put_writes(writes, config)
        — swapped arguments with no task_id — which raised TypeError when
        LangGraph invoked checkpointer.put_writes(config, writes, task_id).
        """
        import inspect

        from lumena.integrations.langgraph import LumenaCheckpointSaver

        params = list(inspect.signature(LumenaCheckpointSaver.put_writes).parameters)
        assert params[1] == "config", "first positional arg must be config"
        assert params[2] == "writes", "second positional arg must be writes"
        assert "task_id" in params
        assert "task_path" in params

        aparams = list(inspect.signature(LumenaCheckpointSaver.aput_writes).parameters)
        assert aparams[1] == "config"
        assert aparams[2] == "writes"
        assert "task_id" in aparams

    def test_put_writes_positional_invocation(self, fresh_config):
        """Invoke put_writes exactly as LangGraph does (positionally)."""
        from lumena.integrations.langgraph import LumenaCheckpointSaver

        saver = LumenaCheckpointSaver(config=fresh_config)
        config = {"configurable": {"thread_id": "sig-test"}}
        saver.put_writes(config, [("ch", "v")], "task-42", "path/0")
        rows = saver.conn.execute(
            "SELECT content FROM chunk WHERE content_hash = 'lg_write'"
        ).fetchall()
        assert any("task-42" in r[0] for r in rows)


class TestPendingWritesRoundTrip:
    def test_pending_writes_are_returned_by_get_tuple(self, fresh_config):
        """put_writes data must round-trip through get_tuple.pending_writes.

        Regression test: writes used to be stored but never read back —
        CheckpointTuple.pending_writes was always [], breaking LangGraph's
        resume-from-interrupt semantics.
        """
        from lumena.integrations.langgraph import LumenaCheckpointSaver

        saver = LumenaCheckpointSaver(config=fresh_config)
        cfg = {"configurable": {"thread_id": "rw"}}
        cp = {"id": "cp-1", "ts": "2026-01-01T00:00:00", "channel_values": {}}
        meta = {"source": "put", "step": 0, "writes": {}, "parents": {}}
        next_cfg = saver.put(cfg, cp, meta, {})

        saver.put_writes(next_cfg, [("messages", "hello")], "task-7")

        tup = saver.get_tuple(next_cfg)
        assert tup is not None
        assert ("task-7", "messages", "hello") in [
            tuple(w) for w in tup.pending_writes
        ]

    def test_list_before_cursor(self, fresh_config):
        """list(before=...) must exclude the cursor checkpoint and newer ones."""
        from lumena.integrations.langgraph import LumenaCheckpointSaver

        saver = LumenaCheckpointSaver(config=fresh_config)
        cfg = {"configurable": {"thread_id": "lb"}}
        cursor_cfg = None
        for i in range(3):
            cp = {"id": f"cp-{i}", "ts": f"2026-01-0{i+1}T00:00:00", "channel_values": {}}
            meta = {"source": "put", "step": i, "writes": {}, "parents": {}}
            out_cfg = saver.put(cfg, cp, meta, {})
            if i == 2:
                cursor_cfg = out_cfg

        ids = [t.checkpoint["id"] for t in saver.list(cfg, before=cursor_cfg)]
        assert "cp-2" not in ids, "cursor checkpoint itself must be excluded"
        assert set(ids) == {"cp-0", "cp-1"}
