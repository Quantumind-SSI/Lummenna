"""C8: Real SearchPipeline.

Orchestrates: intent classify → parallel BM25 + dense retrieval → fusion → budget enforcement → TFC update → re-access reinforcement
"""

import sqlite3
import time

from lummenna.config import LummennaConfig
from lummenna.controller import TwinForceController
from lummenna.force.mnemonic.retrieval_dense import VectorChannel
from lummenna.force.mnemonic.retrieval_graph import GraphChannel, GraphHit
from lummenna.force.mnemonic.retrieval_lexical import LexicalChannel
from lummenna.force.mnemonic.store import _tenant_id_supported
from lummenna.fusion import RetrievedChunk, fuse_and_rerank
from lummenna.intent import IntentRouter
from lummenna.logging import get_console_logger

logger = get_console_logger(__name__)

_REACCESS_BOOST = 1.05


class SearchPipeline:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: LummennaConfig,
        tfc: TwinForceController | None = None,
        embedder=None,
        graph: GraphChannel | None = None,
    ):
        self.conn = conn
        self.config = config
        self.tfc = tfc or TwinForceController()
        self.intent_router = IntentRouter()
        self.lexical = LexicalChannel(conn)
        self.vector = VectorChannel(config, conn)
        if embedder is not None:
            self.embedder = embedder
        else:
            from lummenna.force.contextual.embed import get_embedder

            self.embedder = get_embedder(config, allow_mock=False)
        self.graph = graph

    def _filter_by_tenant(
        self, results: list[RetrievedChunk], tenant_id: str
    ) -> list[RetrievedChunk]:
        """Drop chunks not owned by ``tenant_id``.

        Enforces multi-tenant isolation as a post-retrieval scope: fusion ranks
        across the whole palace, then this restricts the visible set to the
        caller's tenant. No-op only when the schema predates tenant support.

        Extracted from ``execute`` so the primary path and the repair-loop path
        share one implementation — previously the same query/placeholder/filter
        block was duplicated verbatim, so tenant-scoping logic could silently
        drift between the two.
        """
        if not results or not _tenant_id_supported(self.conn):
            return results
        # Multi-tenant isolation is a Pro capability; the default scope remains
        # available to Community but is never an administrator wildcard.
        if tenant_id != "default":
            self.config.assert_licensed("multi_tenant")
        chunk_ids = [r.chunk_id for r in results]
        # placeholders is a generated "?,?,..." string; all values are bound
        # parameters, so the f-string cannot inject SQL (bandit B608).
        placeholders = "?," * (len(chunk_ids) - 1) + "?"
        valid_rows = self.conn.execute(
            f"SELECT chunk_id FROM chunk WHERE chunk_id IN ({placeholders}) AND tenant_id = ?",  # nosec B608
            chunk_ids + [tenant_id],
        ).fetchall()
        valid_ids = {r[0] for r in valid_rows}
        return [r for r in results if r.chunk_id in valid_ids]

    def _record_access(self, results: list[RetrievedChunk]) -> None:
        """Boost V(m) and update last_access_at for retrieved chunks.

        This is the re-access reinforcement mechanism that makes L1 decay
        selective: frequently-accessed memories survive, unused ones fade.
        Each retrieved chunk gets a small multiplicative V(m) boost
        (clamped to 1.0) and a fresh last_access_at timestamp.
        """
        if not results:
            return
        chunk_ids = [r.chunk_id for r in results if r.chunk_id]
        if not chunk_ids:
            return
        # placeholders is a generated "?,?,..." string; all values are bound
        # parameters, so the f-string cannot inject SQL (bandit B608).
        placeholders = "?," * (len(chunk_ids) - 1) + "?"
        now = int(time.time())
        self.conn.execute(
            "UPDATE chunk"
            " SET vm_score = MIN(vm_score * ?, 1.0),"
            "     last_access_at = ?,"
            "     access_count = access_count + 1"
            f" WHERE chunk_id IN ({placeholders})",  # nosec B608
            [_REACCESS_BOOST, now, *chunk_ids],
        )
        self.conn.commit()
        logger.info(
            "reaccess_reinforcement",
            chunks=len(chunk_ids),
            boost=_REACCESS_BOOST,
        )

    def execute(
        self,
        query: str,
        goal_tree_keywords: list[str] | None = None,
        k: int = 20,
        max_repair_attempts: int = 1,
        tenant_id: str = "default",
    ) -> list[RetrievedChunk]:
        if tenant_id != "default" and _tenant_id_supported(self.conn):
            self.config.assert_licensed("multi_tenant")
        start = time.perf_counter()

        # Stage 1: Intent classification
        query_vec = self.embedder.encode_single(query)
        if self.intent_router.lr_coef is not None:
            intent = self.intent_router.classify_with_embedding(query_vec, self.tfc)
        else:
            intent = self.intent_router.classify(query, self.tfc)

        # Stage 2: Parallel retrieval (sequential for simplicity, but real)
        lexical_hits = self.lexical.search(query, k=k)
        dense_hits = self.vector.search(query_vec, k=k)

        # Stage 2b: Graph retrieval
        graph_hits = []
        seed_ids: list[int] = []
        if self.graph is not None and dense_hits:
            seed_ids = [hit.chunk_id for hit in dense_hits[:3]]
            for seed in seed_ids:
                graph_hits.extend(self.graph.traverse_from_seed(seed, hops=2))

        # Stage 2c: Spreading activation (NetworkX) — wider context prefetch
        if (
            seed_ids
            and self.graph is not None
            and getattr(self.graph, "_nx", None) is not None
            and hasattr(self.graph, "_graph")
        ):
            from lummenna.force.mnemonic.spreading import spread_activation

            try:
                activations = spread_activation(self.graph._graph, seed_ids, self.tfc.state.a)
                existing_ids = {h.chunk_id for h in graph_hits}
                for node_id, _score in activations.items():
                    if node_id not in existing_ids:
                        graph_hits.append(GraphHit(node_id, depth=1, path=[node_id]))
            except Exception as exc:
                logger.warning("spreading_activation_failed", error=str(exc))

        # Stage 3: Fusion & rerank
        results = fuse_and_rerank(
            lexical_hits,
            dense_hits,
            goal_tree_keywords or [],
            self.conn,
            budget_candidates=200,
            query_embedding=query_vec,
            graph_hits=graph_hits or None,
            enable_frqad=self.config.enable_frqad,
            config=self.config,
            tenant_id=tenant_id,
        )

        # Primary isolation is the query-level tenant predicate inside
        # fuse_and_rerank (candidates filtered before decrypt/score). This
        # post-filter is retained as defense-in-depth: it re-verifies tenant
        # ownership of the final set and licence-gates any non-default tenant.
        results = self._filter_by_tenant(results, tenant_id)

        # Stage 4: Re-access reinforcement — retrieved chunks get a V(m) boost
        self._record_access(results)

        # Stage 5: TFC update
        self.tfc.update(
            {
                "novelty": 0.5 if not results else 0.3,
                "repetition": 0.0,
                "context_pressure": 0.0,
            }
        )

        # Stage 6: Repair loop
        if max_repair_attempts > 0:
            reason = None
            if not results:
                reason = "empty_results"
            elif all(r.final_score < 0.01 for r in results):
                reason = "low_confidence"

            if reason:
                from lummenna.repair import SearchRepair

                repair = SearchRepair(self.tfc, self)
                repaired = repair.attempt_repair(query, reason, tenant_id=tenant_id)
                if repaired:
                    results = self._filter_by_tenant(repaired, tenant_id)

        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "search_executed",
            # Log a bounded preview, never the full query: queries are raw
            # user input and frequently contain PII, and log aggregation
            # systems rarely apply the store's retention policy.
            query=query[:100],
            intent=intent,
            results=len(results),
            latency_ms=round(elapsed_ms, 2),
        )

        return results
