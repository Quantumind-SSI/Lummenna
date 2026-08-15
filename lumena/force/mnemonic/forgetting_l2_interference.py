"""A7: Interference-Based Forgetting (L2).

Input wire: A1 (schema), A6 (store trigger)
Output wire: A9 (V(m) recalculation), A11 (consolidation)
Secret sauce: Locus occupancy causes Weakening
"""

import sqlite3

import numpy as np

from lumena.logging import get_console_logger

logger = get_console_logger(__name__)

# Whitepaper 3.5 (L2): cosine similarity above this threshold triggers weakening.
INTERFERENCE_THRESHOLD = 0.85
# Penalty applied to the older chunk's Vm, scaled by similarity.
INTERFERENCE_PENALTY_COEFF = 0.15


def check_locus_interference(
    conn: sqlite3.Connection,
    room_id: int,
    locus_id: int,
    new_chunk_id: int,
    new_embedding: np.ndarray,
) -> int:
    """
    When a new memory occupies a locus, check existing residents.
    If cosine similarity > ``INTERFERENCE_THRESHOLD`` (0.85), older chunks suffer
    a Vm penalty of ``INTERFERENCE_PENALTY_COEFF * sim`` (0.15·sim).

    Similarity is clamped to the mathematically valid cosine range [-1, 1] and
    the resulting Vm is clamped to [0, 1], grounding the claim that interference
    can only weaken (never amplify or invert) a memory's value.
    """
    rows = conn.execute(
        """SELECT chunk_id, vm_score FROM chunk
           WHERE locus_id = ? AND chunk_id != ? AND valid_to IS NULL""",
        (locus_id, new_chunk_id),
    ).fetchall()

    weakened = 0
    for old_chunk_id, old_vm in rows:
        old_emb = _get_embedding(conn, old_chunk_id)
        if old_emb is None:
            continue
        sim = float(
            np.dot(new_embedding, old_emb)
            / (np.linalg.norm(new_embedding) * np.linalg.norm(old_emb) + 1e-8)
        )
        # Cosine similarity is mathematically bounded to [-1, 1]; clamp against
        # float error so the penalty stays within its documented magnitude.
        sim = min(1.0, max(-1.0, sim))
        if sim > INTERFERENCE_THRESHOLD:
            penalty = INTERFERENCE_PENALTY_COEFF * sim
            new_vm = min(1.0, max(0.0, old_vm - penalty))
            conn.execute("UPDATE chunk SET vm_score = ? WHERE chunk_id = ?", (new_vm, old_chunk_id))
            weakened += 1
            logger.info(
                "interference_weakened",
                old_chunk=old_chunk_id,
                new_chunk=new_chunk_id,
                similarity=sim,
                new_vm=new_vm,
            )
    return weakened


def _get_embedding(conn: sqlite3.Connection, chunk_id: int) -> np.ndarray | None:
    """Fetch embedding from vec_fallback, then vec_chunks as a fallback."""
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
    except Exception:
        logger.warning(
            "interference_embedding_not_found",
            chunk_id=chunk_id,
        )
    return None
