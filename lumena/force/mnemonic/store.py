"""A6: Store Pipeline (The Write Path).

Input wire: A1 (schema), A3 (vector backend), A5 (provenance), A9 (V(m) model), B2 (embedding model)
Output wire: A2 (FTS5 index), A7 (interference), A11 (consolidation queue)
Secret sauce: Palace-aware placement + V(m) scoring + interference check on write
"""

import hashlib
import json
import sqlite3
import uuid

import numpy as np

from lumena.contracts import EMBEDDING_DIMS, SOURCE_TYPES
from lumena.force.mnemonic.provenance import create_provenance
from lumena.force.mnemonic.value_model import compute_vm
from lumena.logging import get_console_logger
from lumena.sovereign.wear import WearAwareBatcher

logger = get_console_logger(__name__)


def _tenant_id_supported(conn: sqlite3.Connection) -> bool:
    """Return True if the schema has the tenant_id column on chunk."""
    return any(row[1] == "tenant_id" for row in conn.execute("PRAGMA table_info(chunk)").fetchall())


def _get_lexical_channel(conn: sqlite3.Connection):
    from lumena.force.mnemonic.retrieval_lexical import LexicalChannel

    return LexicalChannel(conn)


def _get_vector_channel(conn: sqlite3.Connection, config=None):
    from lumena.force.mnemonic.retrieval_dense import VectorChannel

    if config is None:
        from lumena.config import LumenaConfig

        config = LumenaConfig()
    return VectorChannel(config, conn)


def store_memory(
    conn: sqlite3.Connection,
    content: str,
    room_name: str,
    locus_name: str | None = None,
    source_type: str = "user_input",
    source_ref: str | None = None,
    embedding: np.ndarray | None = None,
    vm_weights: dict | None = None,
    config=None,
    batcher: WearAwareBatcher | None = None,
    tenant_id: str = "default",
) -> int:
    """
    Atomic store: schema + vector + lexical + provenance in one transaction.

    ``batcher`` is accepted for API uniformity but is not used here because
    single-store writes depend on ``lastrowid`` from chunk and provenance
    inserts, which is incompatible with deferred SQL batching.  The batcher
    is wired into bulk callers (consolidation, decay, eviction) where it
    provides real SD/eMMC endurance benefits.
    """
    # Ground the documented source_type contract at the write boundary
    # (whitepaper 3.4 / schema CHECK) before any work is done.
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"source_type must be one of {sorted(SOURCE_TYPES)}, got {source_type!r}")

    # Pre-store safety scan — apply configured PII strategy at the write path
    if config is None:
        from lumena.config import LumenaConfig

        config = LumenaConfig()

    # Ground the embedding-dimensionality claim (whitepaper 3.1: 384-dim vectors)
    # against the configured model dims (default 384). A mismatched vector would
    # silently corrupt the dense index, so reject it at the boundary.
    if embedding is not None:
        emb_arr = np.asarray(embedding)
        emb_dims = int(emb_arr.shape[-1]) if emb_arr.ndim else 0
        expected_dims = int(getattr(config, "embedding_dims", EMBEDDING_DIMS))
        if emb_dims != expected_dims:
            raise ValueError(
                f"embedding must be {expected_dims}-dimensional "
                f"(configured embedding_dims; whitepaper 3.1 default "
                f"{EMBEDDING_DIMS}), got {emb_dims}"
            )

    if config.pii_detection_enabled:
        from lumena.compliance.safety_forgetting import apply_pii_strategy, safety_scan_chunk

        scan_hits = safety_scan_chunk(content, custom_patterns=config.pii_custom_patterns)
        if scan_hits:
            logger.warning(
                "safety_scan_triggered",
                room=room_name,
                hits=scan_hits,
                mode=config.pii_redaction_mode,
            )
            redacted = apply_pii_strategy(
                content,
                scan_hits,
                config.pii_redaction_mode,
                custom_patterns=config.pii_custom_patterns,
            )
            if redacted is None:
                logger.warning("store_blocked_by_pii_policy", room=room_name, hits=scan_hits)
                raise RuntimeError(f"Store blocked by PII policy: {scan_hits}")
            content = redacted

    # Optional field-level encryption
    from lumena.data.schema import get_encryption

    _enc = get_encryption(config)
    _encrypted = 0
    _plain_content = content
    if _enc.enabled:
        content = _enc.encrypt(content).decode("ascii")
        _encrypted = 1
        logger.info("content_encrypted", room=room_name, chunk_len=len(_plain_content))

    has_tenant = _tenant_id_supported(conn)

    # Multi-tenant isolation is a Pro capability. Writing under a real (non-
    # default) tenant requires a license; the default single-tenant path is
    # always free. Gate at the write boundary so unlicensed callers cannot
    # persist tenant-scoped data (AGENTS.md Rule 2; mirrors search read gate).
    if has_tenant and tenant_id != "default":
        config.assert_licensed("multi_tenant")
        # Seat enforcement: seats cap the number of concurrent non-default
        # tenants. Reject a store that would introduce a new tenant beyond the
        # licensed seat count (no-op for unlimited licences or existing tenants).
        from lumena.licensing import assert_seat_available

        assert_seat_available(conn, tenant_id, config)

    with conn:
        # 1. Resolve room
        if has_tenant:
            row = conn.execute(
                "SELECT room_id FROM room WHERE name = ? AND tenant_id = ?", (room_name, tenant_id)
            ).fetchone()
            if not row:
                cur = conn.execute(
                    "INSERT INTO room(name, room_type, tenant_id) VALUES (?, 'domain', ?)",
                    (room_name, tenant_id),
                )
                room_id = cur.lastrowid
            else:
                room_id = row[0]
        else:
            row = conn.execute("SELECT room_id FROM room WHERE name = ?", (room_name,)).fetchone()
            if not row:
                cur = conn.execute(
                    "INSERT INTO room(name, room_type) VALUES (?, 'domain')", (room_name,)
                )
                room_id = cur.lastrowid
            else:
                room_id = row[0]

        # 2. Deduplication (hash plaintext so encryption doesn't break dedup)
        content_hash = hashlib.sha256(_plain_content.encode()).hexdigest()
        if has_tenant:
            existing = conn.execute(
                "SELECT chunk_id FROM chunk WHERE content_hash = ? AND valid_to IS NULL AND tenant_id = ?",
                (content_hash, tenant_id),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT chunk_id FROM chunk WHERE content_hash = ? AND valid_to IS NULL",
                (content_hash,),
            ).fetchone()
        if existing:
            logger.info("store_dedup", room=room_name, hash=content_hash[:16])
            return int(existing[0])

        # 3. Locus resolution
        locus_id = _resolve_locus(conn, room_id, locus_name, embedding, tenant_id, has_tenant)

        # 4. Compute V(m)
        vm_score, vm_factors = compute_vm(content, vm_weights, source_type)

        # 5. Insert chunk
        if has_tenant:
            cur = conn.execute(
                """INSERT INTO chunk
                   (locus_id, room_id, content, content_hash, vm_score, vm_factors, resolution, encrypted, tenant_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    locus_id,
                    room_id,
                    content,
                    content_hash,
                    vm_score,
                    json.dumps(vm_factors),
                    "FP32",
                    _encrypted,
                    tenant_id,
                ),
            )
        else:
            cur = conn.execute(
                """INSERT INTO chunk
                   (locus_id, room_id, content, content_hash, vm_score, vm_factors, resolution, encrypted)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    locus_id,
                    room_id,
                    content,
                    content_hash,
                    vm_score,
                    json.dumps(vm_factors),
                    "FP32",
                    _encrypted,
                ),
            )
        chunk_id = int(cur.lastrowid or 0)

        # 6. Provenance
        prov_id = create_provenance(conn, chunk_id, source_type, source_ref)
        conn.execute("UPDATE chunk SET provenance_root = ? WHERE chunk_id = ?", (prov_id, chunk_id))

        # 7. Vector index
        if embedding is not None:
            _get_vector_channel(conn, config).add(chunk_id, embedding)

        # 8. Lexical index (always index plaintext so search works)
        _get_lexical_channel(conn).index_chunk(chunk_id, _plain_content)

        # 9. Interference check
        _trigger_interference_check(conn, room_id, locus_id, chunk_id, embedding)

        logger.info(
            "memory_stored", chunk_id=chunk_id, room=room_name, locus=locus_name, vm=vm_score
        )
        return chunk_id


def _resolve_locus(
    conn, room_id, locus_name, embedding, tenant_id: str = "default", has_tenant: bool = False
):
    if locus_name:
        row = conn.execute(
            "SELECT locus_id FROM locus WHERE room_id=? AND name=?", (room_id, locus_name)
        ).fetchone()
        if row:
            return row[0]
        try:
            if has_tenant:
                cur = conn.execute(
                    "INSERT INTO locus(room_id, name, tenant_id) VALUES (?,?,?)",
                    (room_id, locus_name, tenant_id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO locus(room_id, name) VALUES (?,?)", (room_id, locus_name)
                )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT locus_id FROM locus WHERE room_id=? AND name=?", (room_id, locus_name)
            ).fetchone()
            if row:
                return row[0]
            raise
    # Auto-placement
    max_attempts = 10
    for _ in range(max_attempts):
        auto_name = f"auto_{uuid.uuid4().hex}"
        try:
            if has_tenant:
                cur = conn.execute(
                    "INSERT INTO locus(room_id, name, tenant_id) VALUES (?,?,?)",
                    (room_id, auto_name, tenant_id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO locus(room_id, name) VALUES (?,?)", (room_id, auto_name)
                )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError(f"Failed to insert locus after {max_attempts} attempts")


def _trigger_interference_check(conn, room_id, locus_id, new_chunk_id, embedding):
    from lumena.force.mnemonic.forgetting_l2_interference import check_locus_interference

    if embedding is not None:
        check_locus_interference(conn, room_id, locus_id, new_chunk_id, embedding)
