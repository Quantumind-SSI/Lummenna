"""A10: Budget-Curated Forgetting (L3).

Input wire: SQLite schema, config.memory_limit_mb
Output wire: A12 (optical degradation / release scheduler), D10 (actual vector mutation)
Secret sauce: Net-value-per-byte eviction

NOTE: Trigger uses chunk-count estimation rather than raw process RSS,
because RSS includes Python heap, embedding models, and ONNX runtime
overhead that are irrelevant to Lummenna's data footprint.
"""

import sqlite3
from typing import Literal, cast

import numpy as np

from lummenna.data.locks import maintenance_guard
from lummenna.logging import get_console_logger
from lummenna.sovereign.wear import WearAwareBatcher

logger = get_console_logger(__name__)

BYTES_PER_CHUNK_ESTIMATE = 1800

# Optical-degradation resolution ladder (whitepaper 3.1 / 3.5 L3).
Resolution = Literal["FP32", "FP16", "INT8", "BINARY", "RELEASED"]
_RESOLUTION_CHAIN: dict[Resolution, Resolution] = {
    "FP32": "FP16",
    "FP16": "INT8",
    "INT8": "BINARY",
    "BINARY": "RELEASED",
}


def budget_curated_eviction(
    conn: sqlite3.Connection,
    config,
    target_ram_mb: float | None = None,
    batcher: WearAwareBatcher | None = None,
):
    """
    When estimated data-memory footprint exceeds trigger, evict lowest V(m)/byte candidates.
    Eviction = optical degradation (FP32→FP16→INT8→BINARY→RELEASED).
    """
    memory_limit_mb = getattr(config, "memory_limit_mb", 512)
    if memory_limit_mb <= 0:
        raise ValueError(f"memory_limit_mb must be > 0, got {memory_limit_mb}")
    if target_ram_mb is None:
        target_ram_mb = memory_limit_mb * 0.85
    elif target_ram_mb < 0:
        # 0 is legal: "evict down to an empty footprint". Negative is nonsensical.
        raise ValueError(f"target_ram_mb must be >= 0, got {target_ram_mb}")

    # Eviction selects the lowest-value chunks, then steps each one down the
    # resolution ladder. Two processes running this against the same pre-pass
    # state select the same chunks and each apply one step, so a single pass
    # worth of memory pressure degrades FP32 straight to INT8. The discarded
    # precision cannot be recovered, so the selection and the degradation must
    # happen inside one window.
    with maintenance_guard(conn, "budget_curated_eviction") as acquired:
        if not acquired:
            return 0

        # Chunk-count proxy for data-memory footprint (deterministic, testable, excludes interpreter overhead)
        row = conn.execute("SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL").fetchone()
        active_chunks = row[0] if row else 0
        estimated_mb = (active_chunks * BYTES_PER_CHUNK_ESTIMATE) / (1024 * 1024)

        if estimated_mb < target_ram_mb:
            return 0

        needed_eviction_mb = estimated_mb - (memory_limit_mb * 0.75)
        bytes_per_chunk = BYTES_PER_CHUNK_ESTIMATE
        chunks_to_evict = max(1, int((needed_eviction_mb * 1024 * 1024) / bytes_per_chunk))

        rows = conn.execute(
            """SELECT chunk_id, vm_score, resolution,
                      (julianday('now') - julianday(datetime(created_at, 'unixepoch'))) AS age_days
               FROM chunk
               WHERE valid_to IS NULL AND optical_level < 2
               ORDER BY (vm_score * (1.0 / (age_days + 1.0))) ASC
               LIMIT ?""",
            (chunks_to_evict,),
        ).fetchall()

        evicted = 0
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        # One code path for both batched and immediate writes: the statements
        # are identical, only their destination differs. Keeping them unified
        # means the batched path cannot drift from the tested one.
        for chunk_id, _vm, res, _age in rows:
            new_res = _next_resolution(res)
            if new_res == "RELEASED":
                statements: list[tuple[str, tuple]] = [
                    (
                        "UPDATE chunk SET resolution = 'RELEASED', optical_level = 2, "
                        "valid_to = unixepoch() WHERE chunk_id = ?",
                        (chunk_id,),
                    )
                ]
            else:
                statements = [
                    (
                        "UPDATE chunk SET resolution = ?, optical_level = 1 WHERE chunk_id = ?",
                        (new_res, chunk_id),
                    )
                ]
            statements.extend(_vector_statements(conn, chunk_id, new_res, tables))

            if batcher:
                batcher.queue.extend(statements)
            else:
                for sql, params in statements:
                    conn.execute(sql, params)
            evicted += 1

        if not batcher:
            conn.commit()
        logger.info(
            "budget_eviction",
            evicted=evicted,
            estimated_mb=round(estimated_mb, 1),
            active_chunks=active_chunks,
            target_mb=target_ram_mb,
        )
        return evicted


def _vector_statements(
    conn: sqlite3.Connection,
    chunk_id: int,
    new_res: Resolution,
    tables: set[str],
) -> list[tuple[str, tuple]]:
    """Vector-side writes for one step down the optical ladder.

    Degradation re-quantizes the stored vector; it does not discard it.
    Deleting the vector on the first step (FP32 to FP16) makes the chunk
    permanently invisible to semantic search and to L2 interference, collapsing
    a four-step ladder into a one-step cliff. Only ``RELEASED``, the terminal
    state, removes the vector.

    Falls back to deletion only when the vector genuinely cannot be degraded:
    no mirror table, no stored vector, or an unavailable/failing quantizer.

    Args:
        conn: Open connection to the store.
        chunk_id: Chunk being degraded.
        new_res: Target resolution from :func:`_next_resolution`.
        tables: Table names present in the schema, probed once by the caller.

    Returns:
        ``(sql, params)`` pairs to execute or queue, in order.

    Complexity:
        One SELECT and O(embedding_dims) quantization work per chunk.
    """
    has_fallback = "vec_fallback" in tables
    has_vec_chunks = "vec_chunks" in tables

    def deletions() -> list[tuple[str, tuple]]:
        out: list[tuple[str, tuple]] = []
        if has_fallback:
            out.append(("DELETE FROM vec_fallback WHERE chunk_id = ?", (chunk_id,)))
        if has_vec_chunks:
            out.append(("DELETE FROM vec_chunks WHERE chunk_id = ?", (chunk_id,)))
        return out

    if new_res == "RELEASED" or not has_fallback:
        return deletions()

    try:
        from lummenna.sovereign.optical import QUANTIZERS

        quantizer = QUANTIZERS.get(new_res)
    except Exception as exc:
        logger.warning("optical_quantizers_unavailable", error=str(exc))
        return deletions()
    if quantizer is None:
        return deletions()

    row = conn.execute(
        "SELECT embedding FROM vec_fallback WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()
    if row is None or not row[0]:
        return deletions()

    try:
        original = np.frombuffer(row[0], dtype=np.float32)
        blob = np.asarray(quantizer(original), dtype=np.float32).tobytes()
    except Exception as exc:
        logger.warning(
            "vector_quantize_failed", chunk_id=chunk_id, resolution=new_res, error=str(exc)
        )
        return deletions()

    statements: list[tuple[str, tuple]] = [
        ("UPDATE vec_fallback SET embedding = ? WHERE chunk_id = ?", (blob, chunk_id))
    ]
    if has_vec_chunks:
        # vec0 virtual tables do not support in-place UPDATE of the vector
        # column; replace the row instead.
        statements.append(("DELETE FROM vec_chunks WHERE chunk_id = ?", (chunk_id,)))
        statements.append(
            ("INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?,?)", (chunk_id, blob))
        )
    return statements


def _next_resolution(current: str) -> Resolution:
    """Return the next-lower resolution in the optical-degradation ladder.

    Any unknown/absent resolution collapses to ``RELEASED``, so eviction always
    makes monotonic progress toward release (the L3 termination guarantee).
    """
    result: Resolution = _RESOLUTION_CHAIN.get(cast(Resolution, current), "RELEASED")
    return result
