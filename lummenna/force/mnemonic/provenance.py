"""A5: Bi-Temporal Provenance Engine.

Input wire: SQLite schema (A1)
Output wire: A6 (store), C5 (context assembly), A11 (compliance purge)
Secret sauce: Engram-inspired bi-temporal model + supersession chains
"""

import sqlite3
from dataclasses import dataclass

from lummenna.contracts import SOURCE_TYPES
from lummenna.logging import get_console_logger

logger = get_console_logger(__name__)


@dataclass
class ProvenanceRecord:
    provenance_id: int
    chunk_id: int
    source_type: str
    source_ref: str | None
    confidence: float
    extraction_method: str | None
    parent_provenance: int | None


def create_provenance(
    conn: sqlite3.Connection,
    chunk_id: int,
    source_type: str,
    source_ref: str | None = None,
    confidence: float = 1.0,
    extraction_method: str | None = None,
    parent_provenance: int | None = None,
) -> int:
    """Bi-temporal provenance: every memory enters with a causal chain.

    Grounds the documented provenance contract: ``source_type`` must be one of
    the five documented values and ``confidence`` is a probability-like weight
    in [0, 1].
    """
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"source_type must be one of {sorted(SOURCE_TYPES)}, got {source_type!r}")
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"confidence must be in [0, 1], got {confidence}")
    cur = conn.execute(
        """INSERT INTO provenance
           (chunk_id, source_type, source_ref, confidence, extraction_method, parent_provenance)
           VALUES (?,?,?,?,?,?)""",
        (chunk_id, source_type, source_ref, confidence, extraction_method, parent_provenance),
    )
    prov_id = int(cur.lastrowid or 0)
    logger.info(
        "provenance_created",
        chunk_id=chunk_id,
        prov_id=prov_id,
        source_type=source_type,
        parent=parent_provenance,
    )
    return prov_id


def get_effective_fact(
    conn: sqlite3.Connection, content_hash_prefix: str, as_of_transaction: int | None = None
) -> dict | None:
    """
    Engram merge-on-read: find the currently valid version of a fact,
    respecting supersession chains. If as_of_transaction is given, time-travel.
    """
    if as_of_transaction:
        sql = """
            SELECT c.*, p.source_type, p.confidence
            FROM chunk c
            LEFT JOIN provenance p ON c.provenance_root = p.provenance_id
            WHERE c.content_hash LIKE ? || '%'
              AND c.valid_from <= ?
              AND (c.valid_to IS NULL OR c.valid_to > ?)
            ORDER BY c.created_at DESC
            LIMIT 1
        """
        row = conn.execute(
            sql, (content_hash_prefix, as_of_transaction, as_of_transaction)
        ).fetchone()
    else:
        sql = """
            SELECT c.*, p.source_type, p.confidence
            FROM chunk c
            LEFT JOIN provenance p ON c.provenance_root = p.provenance_id
            WHERE c.content_hash LIKE ? || '%'
              AND c.valid_to IS NULL
            ORDER BY c.created_at DESC
            LIMIT 1
        """
        row = conn.execute(sql, (content_hash_prefix,)).fetchone()
    return dict(row) if row else None


def supersede_chunk(conn: sqlite3.Connection, old_chunk_id: int, new_chunk_id: int) -> bool:
    """Logical update: old fact is deprecated, new fact carries the chain.

    This is a compare-and-swap. The row is only superseded while it is still
    current, so a chunk that another writer already superseded is left alone.
    An unconditional UPDATE here loses writes: two callers superseding the same
    chunk would both succeed, the second overwriting ``superseded_by``, which
    orphans the first successor from the provenance chain and silently breaks
    ``get_effective_fact`` for that lineage.

    Args:
        conn: SQLite connection. The caller owns the transaction.
        old_chunk_id: Chunk being deprecated. Must currently be valid.
        new_chunk_id: Chunk that carries the fact forward.

    Returns:
        True if this call superseded the chunk. False if the chunk does not
        exist or was already superseded, which callers should treat as "another
        writer got there first", not as an error.

    Raises:
        ValueError: If a chunk is asked to supersede itself, which would make
            the provenance chain cyclic and ``get_effective_fact`` non-terminating.
    """
    if old_chunk_id == new_chunk_id:
        raise ValueError(
            f"chunk {old_chunk_id} cannot supersede itself; "
            "this would make the provenance chain cyclic"
        )

    cursor = conn.execute(
        "UPDATE chunk SET valid_to = unixepoch(), superseded_by = ? "
        "WHERE chunk_id = ? AND valid_to IS NULL",
        (new_chunk_id, old_chunk_id),
    )
    if cursor.rowcount == 0:
        logger.info(
            "chunk_supersede_skipped",
            old=old_chunk_id,
            new=new_chunk_id,
            reason="chunk is missing or already superseded",
        )
        return False

    logger.info("chunk_superseded", old=old_chunk_id, new=new_chunk_id)
    return True
