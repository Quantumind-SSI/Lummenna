"""C2: Fusion & Reranking Engine.

Input wire: A2 (BM25 hits), A3 (dense hits), A4 (FRQAD rerank), B1 (goal-tree), A9 (V(m))
Output wire: B1 (context assembly)
Secret sauce: Multi-channel RRF × V(m) × recency × FRQAD rerank
"""

import sqlite3
from typing import Annotated

import numpy as np
from pydantic import Field
from pydantic.dataclasses import dataclass

from lumena.force.mnemonic.retrieval_dense import DenseHit
from lumena.force.mnemonic.retrieval_graph import GraphHit
from lumena.force.mnemonic.retrieval_lexical import LexicalHit
from lumena.logging import get_console_logger

logger = get_console_logger(__name__)

# Standard RRF constant from Cormack et al. (2009); the whitepaper (3.3) fixes
# k_RRF = 60. Kept as a module constant so the claim has a single source of truth.
K_RRF = 60
# Graph-channel RRF contributions are down-weighted relative to lexical/dense.
GRAPH_RRF_WEIGHT = 0.8
# Recency half-life used in the exp(-age/τ) boost, in hours (168h = 7 days).
RECENCY_TAU_HOURS = 168.0


def _chunk_has_tenant_column(conn: sqlite3.Connection) -> bool:
    """Return True if the ``chunk`` table has a ``tenant_id`` column.

    Mirrors ``lumena.force.mnemonic.store._tenant_id_supported`` but is kept
    local to avoid coupling the fusion layer to the store module. Used so the
    tenant predicate degrades to a no-op on databases whose schema predates
    multi-tenant support (migration 0002).
    """
    return any(row[1] == "tenant_id" for row in conn.execute("PRAGMA table_info(chunk)").fetchall())


@dataclass(config={"arbitrary_types_allowed": True})
class RetrievedChunk:
    """A fused/reranked retrieval result.

    Grounded invariants (the quantities the whitepaper actually bounds):
      * ``vm_score`` is the V(m) value and is bounded to [0, 1].
      * ``frqad_score`` is a similarity in [-1, 1] (cosine range); the fusion
        path narrows it to [0, 1], but the container also holds raw
        dense-similarity scores (e.g. in ablation studies), which may be
        negative.
      * ``recency_hours`` is an age and cannot be negative.

    ``rrf_score`` and ``final_score`` are *ranking scores* — only their order
    matters — so they are intentionally unconstrained: a dense-only
    configuration ranks by raw cosine similarity, which is legitimately negative
    for anti-correlated vectors. Over-constraining them to be non-negative would
    be a false claim, not a grounded one.
    """

    chunk_id: int
    room_name: str
    locus_name: str
    content: str
    provenance_id: int | None
    rrf_score: float
    vm_score: Annotated[float, Field(ge=0.0, le=1.0)]
    frqad_score: Annotated[float, Field(ge=-1.0, le=1.0)]
    recency_hours: Annotated[float, Field(ge=0.0)]
    final_score: float


def fuse_and_rerank(
    lexical_hits: list[LexicalHit],
    dense_hits: list[DenseHit],
    goal_tree_keywords: list[str],
    conn: sqlite3.Connection,
    budget_candidates: int = 200,
    query_embedding: np.ndarray | None = None,
    graph_hits: list[GraphHit] | None = None,
    enable_frqad: bool | None = None,
    config=None,
    tenant_id: str = "default",
) -> list[RetrievedChunk]:
    """
    Stage 3: Reciprocal Rank Fusion + V(m) + FRQAD rerank + recency boost.

    ``tenant_id`` enforces multi-tenant isolation at the single relational
    chokepoint below: all channels (lexical, dense, graph) propose candidate
    chunk ids, but every candidate is materialised from the ``chunk`` table by
    this one query before any content is decrypted or scored. Restricting that
    query with ``AND c.tenant_id = ?`` drops cross-tenant candidates *before*
    decryption/scoring, so other tenants' plaintext is never processed in memory
    (Pro feature ``multi_tenant``; see COMMERCIAL-LICENSE.md). The default tenant
    and schemas predating tenant support are unrestricted, so single-tenant
    deployments pay nothing.
    """
    k_rrf = K_RRF  # standard RRF constant from Cormack et al. (2009)
    if budget_candidates < 1:
        raise ValueError(f"budget_candidates must be >= 1, got {budget_candidates}")

    # Build candidate pool with RRF
    rrfs: dict[int, float] = {}
    for rank, hit in enumerate(lexical_hits, 1):
        rrfs[hit.chunk_id] = rrfs.get(hit.chunk_id, 0.0) + 1.0 / (k_rrf + rank)
    for rank, dhit in enumerate(dense_hits, 1):
        rrfs[dhit.chunk_id] = rrfs.get(dhit.chunk_id, 0.0) + 1.0 / (k_rrf + rank)

    if graph_hits:
        for rank, ghit in enumerate(graph_hits, 1):
            rrfs[ghit.chunk_id] = (
                rrfs.get(ghit.chunk_id, 0.0) + 1.0 / (k_rrf + rank) * GRAPH_RRF_WEIGHT
            )

    chunk_ids = list(rrfs.keys())[:budget_candidates]
    if not chunk_ids:
        return []

    # placeholders is a generated "?,?,..." string; all values are bound
    # parameters, so the f-strings below cannot inject SQL (bandit B608).
    placeholders = "?," * (len(chunk_ids) - 1) + "?"

    # Multi-tenant isolation at the relational chokepoint: apply the tenant
    # predicate here so cross-tenant candidates are filtered in SQL before any
    # content is decrypted/scored below. No-op for the default tenant or for
    # schemas that predate tenant support (single-tenant deployments pay
    # nothing); tenant_id is a bound parameter, never string-interpolated.
    params: list[object] = list(chunk_ids)
    tenant_clause = ""
    if tenant_id != "default" and _chunk_has_tenant_column(conn):
        tenant_clause = " AND c.tenant_id = ?"
        params.append(tenant_id)

    # Only code-controlled fragments are interpolated: `placeholders` is a
    # generated "?,?,..." string and `tenant_clause` is one of two fixed
    # literals ("" or " AND c.tenant_id = ?"). All runtime values (chunk ids,
    # tenant_id) are bound parameters in `params`, so there is no injection
    # surface. Assembled as one string so the audited-safe suppression sits on
    # the execute() call line.
    candidate_sql = (
        f"SELECT c.chunk_id, r.name AS room_name, l.name AS locus_name,"  # nosec B608
        f" c.content, c.vm_score, c.provenance_root,"
        f" (strftime('%s','now') - c.created_at) / 3600.0 AS age_hours,"
        f" c.resolution, c.encrypted"
        f" FROM chunk c"
        f" LEFT JOIN room r ON r.room_id = c.room_id"
        f" LEFT JOIN locus l ON l.locus_id = c.locus_id"
        f" WHERE c.chunk_id IN ({placeholders}) AND c.valid_to IS NULL{tenant_clause}"
    )
    rows = conn.execute(candidate_sql, params).fetchall()

    # Pre-fetch query embedding for FRQAD if provided
    query_vec = query_embedding
    results: list[RetrievedChunk] = []

    # Batch-fetch all embeddings in one query to avoid N+1
    embedding_map: dict[int, np.ndarray] = {}
    if query_vec is not None:
        emb_rows = conn.execute(
            f"SELECT chunk_id, embedding FROM vec_fallback WHERE chunk_id IN ({placeholders})",  # nosec B608
            chunk_ids,
        ).fetchall()
        for cid, emb_blob in emb_rows:
            if emb_blob:
                embedding_map[cid] = np.frombuffer(emb_blob, dtype=np.float32)
        missing = [cid for cid in chunk_ids if cid not in embedding_map]
        if missing:
            try:
                miss_placeholders = "?," * (len(missing) - 1) + "?"
                emb_rows2 = conn.execute(
                    f"SELECT chunk_id, embedding FROM vec_chunks WHERE chunk_id IN ({miss_placeholders})",  # nosec B608
                    missing,
                ).fetchall()
                for cid, emb_blob in emb_rows2:
                    if emb_blob:
                        embedding_map[cid] = np.frombuffer(emb_blob, dtype=np.float32)
            except Exception as exc:
                logger.debug("vec_chunks_embedding_fetch_failed", error=str(exc))

    # Lazy-load encryption helper once
    _enc = None

    for row in rows:
        cid, room_name, locus_name, content, vm, prov, age_hours, res, encrypted = row
        if encrypted:
            if _enc is None:
                from lumena.config import LumenaConfig
                from lumena.data.schema import get_encryption

                _enc = get_encryption(config if config is not None else LumenaConfig())
            if _enc.enabled:
                try:
                    content = _enc.decrypt(content)
                except Exception as exc:
                    # Wrong or rotated key: never leak the raw Fernet token
                    # and never crash the whole search for one bad chunk.
                    logger.warning("chunk_decrypt_failed", chunk_id=cid, error=str(exc))
                    content = "[DECRYPTION FAILED — check LUMENA_ENCRYPTION_KEY]"
            else:
                # Chunk was written with encryption but no key is configured
                # now: do not leak the raw ciphertext token as if it were text.
                logger.warning("chunk_encrypted_but_no_key", chunk_id=cid)
                content = "[ENCRYPTED — set LUMENA_ENCRYPTION_KEY to read]"
        # Goal bonus
        goal_bonus = 1.0 + (
            0.2 if any(kw.lower() in (content or "").lower() for kw in goal_tree_keywords) else 0.0
        )

        # FRQAD / cosine rerank
        frqad_sim = 0.5
        if query_vec is not None:
            cand_vec = embedding_map.get(cid)
            if cand_vec is not None:
                use_frqad = enable_frqad if enable_frqad is not None else True
                try:
                    if use_frqad:
                        from lumena.sovereign.frqad import compute_frqad

                        frqad = compute_frqad(query_vec, cand_vec, res or "FP32")
                        frqad_sim = 1.0 - (frqad / (np.pi / 2))
                    else:
                        from scipy.spatial.distance import cosine

                        frqad_sim = 1.0 - cosine(query_vec, cand_vec)
                except Exception as exc:
                    logger.debug("frqad_fallback_to_cosine", error=str(exc))
                    from scipy.spatial.distance import cosine

                    frqad_sim = 1.0 - cosine(query_vec, cand_vec)
            else:
                frqad_sim = 0.5

        rrf = rrfs.get(cid, 0.0)
        recency_boost = float(np.exp(-(age_hours or 0) / RECENCY_TAU_HOURS))

        # Clamp the two similarity-derived quantities into their contractual
        # [0, 1] range: cosine/FRQAD arithmetic and rounding can push values a
        # hair outside the bounds, and a legacy vm_score could be out of range.
        vm_clamped = min(1.0, max(0.0, float(vm if vm is not None else 0.0)))
        frqad_clamped = min(1.0, max(0.0, float(frqad_sim)))
        age_hours_nn = max(0.0, float(age_hours or 0.0))

        final = rrf * (vm_clamped + 0.1) * (frqad_clamped + 0.1) * recency_boost * goal_bonus

        results.append(
            RetrievedChunk(
                chunk_id=cid,
                room_name=room_name or "unknown",
                locus_name=locus_name or "none",
                content=content,
                provenance_id=prov,
                rrf_score=rrf,
                vm_score=vm_clamped,
                frqad_score=frqad_clamped,
                recency_hours=age_hours_nn,
                final_score=final,
            )
        )

    results.sort(key=lambda x: x.final_score, reverse=True)
    logger.info(
        "fusion_rerank",
        candidates=len(results),
        top_score=results[0].final_score if results else None,
    )
    return results


def _get_embedding(conn: sqlite3.Connection, chunk_id: int) -> np.ndarray | None:
    row = conn.execute(
        "SELECT embedding FROM vec_fallback WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()
    if row and row[0]:
        return np.frombuffer(row[0], dtype=np.float32)
    try:
        row2 = conn.execute(
            "SELECT embedding FROM vec_chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        if row2 and row2[0]:
            return np.frombuffer(row2[0], dtype=np.float32)
    except Exception as exc:
        logger.debug("vec_chunks_embedding_fetch_failed", chunk_id=chunk_id, error=str(exc))
    return None
