"""Lummenna adapter.

Self-authored, and marked as such in every artifact this produces. A reader
should weigh it accordingly: the maintainer tuning their own adapter while
scoring others' is the central bias risk of any vendor-run leaderboard, and the
only honest response is to publish the fact rather than assert impartiality.

Configuration is deliberately stock. No recency shaping, budget, or fusion knob
is tuned for the benchmark; the adapter runs what a user gets by default, so the
number is representative rather than a best case.
"""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from benchmarks.leaderboard.adapter import (
    AdapterInfo,
    MemoryItem,
    Question,
    RetrievalResult,
)


class LummennaAdapter:
    """Runs Lummenna's real store and search pipeline, not a reimplementation.

    Uses the same ``store_memory`` write path and ``SearchPipeline`` read path
    as the shipped product, so a result here reflects the actual system rather
    than a benchmark-only shortcut.
    """

    def __init__(self, embedding_model: str | None = None) -> None:
        self._embedding_model = embedding_model
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._conn: sqlite3.Connection | None = None
        # Typed Any: these hold Lummenna internals that are imported lazily so
        # the adapter module stays importable when lummenna is absent.
        self._pipeline: Any = None
        self._embedder: Any = None
        self._config: Any = None
        self._item_ids: dict[int, str] = {}

    def info(self) -> AdapterInfo:
        version: str | None
        try:
            from importlib import metadata

            version = metadata.version("lummenna")
        except Exception:
            version = None
        return AdapterInfo(
            framework="Lummenna",
            version=version,
            authored_by="lummenna",
            verified_by=None,
            config_summary=(
                "Stock configuration: hybrid RRF fusion (BM25 + dense + graph), "
                "default recency shaping, no benchmark-specific tuning"
            ),
            homepage="https://github.com/QuantumindSSI/lummenna",
        )

    def available(self) -> tuple[bool, str]:
        """Require a real embedder; mock vectors would make the score meaningless."""
        try:
            from lummenna.config import LummennaConfig
            from lummenna.force.contextual.embed import get_embedder
        except ImportError as exc:
            return False, f"lummenna is not importable: {exc}"

        config = LummennaConfig()
        if self._embedding_model:
            config.embedding_model = self._embedding_model
        try:
            # allow_mock=False: refuse to score hash-based random vectors.
            get_embedder(config, allow_mock=False)
        except Exception as exc:
            return False, (
                f"no real embedding model available ({exc}); "
                "provision it with LUMMENNA_AUTO_PROVISION_MODEL=1"
            )
        return True, ""

    def reset(self) -> None:
        """Create a fresh on-disk store.

        On disk rather than in memory because the product's WAL and sqlite-vec
        paths are what should be measured.
        """
        from lummenna.config import LummennaConfig
        from lummenna.data.schema import ensure_schema, get_connection
        from lummenna.force.contextual.embed import get_embedder
        from lummenna.force.mnemonic.store import reset_channel_cache
        from lummenna.search import SearchPipeline

        self.teardown()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="lummenna-leaderboard-")
        store_path = Path(self._tmpdir.name) / "store"
        store_path.mkdir(parents=True, exist_ok=True)

        config = LummennaConfig(store_path=store_path)
        if self._embedding_model:
            config.embedding_model = self._embedding_model
        self._config = config
        self._conn = get_connection(config)
        ensure_schema(self._conn)
        reset_channel_cache()
        self._embedder = get_embedder(config, allow_mock=False)
        self._pipeline = SearchPipeline(self._conn, config, embedder=self._embedder)
        self._item_ids = {}

    def ingest(self, items: list[MemoryItem]) -> None:
        """Store every item, mapping Lummenna chunk ids back to dataset ids."""
        from lummenna.force.mnemonic.store import store_memory

        if self._conn is None or self._embedder is None:
            raise RuntimeError("reset() must be called before ingest()")

        for item in items:
            embedding = self._embedder.encode_single(item.text)
            chunk_id = store_memory(
                self._conn,
                content=item.text,
                room_name=item.session_id,
                source_type="user_input",
                embedding=embedding,
                config=self._config,
            )
            # store_memory dedups identical content and returns the existing
            # id. Keep the first dataset id for that chunk so scoring stays
            # consistent; a duplicated turn is genuinely the same evidence.
            self._item_ids.setdefault(chunk_id, item.item_id)
        self._conn.commit()

    def retrieve(self, question: Question, top_k: int) -> RetrievalResult:
        """Run the real search pipeline and map results back to dataset ids."""
        if self._pipeline is None:
            raise RuntimeError("reset() must be called before retrieve()")

        started = time.perf_counter()
        results = self._pipeline.execute(question.text, k=top_k)
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        item_ids: list[str] = []
        for result in results[:top_k]:
            mapped = self._item_ids.get(result.chunk_id)
            if mapped is not None:
                item_ids.append(mapped)
        return RetrievalResult(item_ids=item_ids, latency_ms=elapsed_ms)

    def teardown(self) -> None:
        """Close the connection and delete the temporary store."""
        if self._conn is not None:
            # A close failure must not prevent the temp store being removed.
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None
        if self._tmpdir is not None:
            with contextlib.suppress(Exception):
                self._tmpdir.cleanup()
            self._tmpdir = None
        self._pipeline = None
        self._embedder = None
