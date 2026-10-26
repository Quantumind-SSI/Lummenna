"""A6: Store Pipeline (The Write Path).

Input wire: A1 (schema), A3 (vector backend), A5 (provenance), A9 (V(m) model), B2 (embedding model)
Output wire: A2 (FTS5 index), A7 (interference), A11 (consolidation queue)
Secret sauce: Palace-aware placement + V(m) scoring + interference check on write
"""

import hashlib
import json
import sqlite3
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import numpy as np

from lummenna.contracts import EMBEDDING_DIMS, SOURCE_TYPES
from lummenna.force.mnemonic.provenance import create_provenance
from lummenna.force.mnemonic.value_model import compute_vm
from lummenna.logging import get_console_logger
from lummenna.sovereign.wear import WearAwareBatcher

logger = get_console_logger(__name__)


def _tenant_id_supported(conn: sqlite3.Connection) -> bool:
    """Return True if the schema has the tenant_id column on chunk."""
    return any(row[1] == "tenant_id" for row in conn.execute("PRAGMA table_info(chunk)").fetchall())


# Constructing a retrieval channel is not free: the vector channel loads the
# sqlite-vec extension, issues two CREATE TABLE IF NOT EXISTS statements and
# probes the KNN grammar, and the lexical channel runs PRAGMA optimize. Building
# one per write put all of that on every store, and ran DDL inside the caller's
# transaction. Channels are therefore cached per connection.
#
# sqlite3.Connection supports neither weak references nor attribute assignment,
# so the cache is keyed by id() and holds the connection itself: that strong
# reference makes id reuse impossible while an entry is live, and each lookup
# re-verifies identity with `is` before trusting the entry. The cache is bounded
# so a long-lived process that opens short-lived connections (the sleep
# scheduler opens one per pass) cannot grow it without limit.
_CHANNEL_CACHE_MAX = 8
_channel_lock = threading.Lock()
_lexical_channels: OrderedDict[int, tuple[sqlite3.Connection, Any]] = OrderedDict()
_vector_channels: OrderedDict[int, tuple[sqlite3.Connection, Any]] = OrderedDict()


def _cached_channel(
    cache: OrderedDict[int, tuple[sqlite3.Connection, Any]],
    conn: sqlite3.Connection,
    factory: Callable[[], Any],
) -> Any:
    """Return the cached channel for *conn*, constructing it at most once.

    Args:
        cache: The per-channel-type cache to use.
        conn: Connection the channel is bound to.
        factory: Zero-argument constructor, called only on a miss.

    Returns:
        The channel bound to *conn*.

    Note:
        ``factory`` runs outside the lock because it touches SQLite. Two threads
        racing a miss may both construct; the loser's channel is discarded. That
        is safe because construction is idempotent (``CREATE TABLE IF NOT
        EXISTS``) and costs one redundant build, whereas holding the lock across
        I/O would serialise every writer.
    """
    key = id(conn)
    with _channel_lock:
        entry = cache.get(key)
        if entry is not None and entry[0] is conn:
            cache.move_to_end(key)
            return entry[1]

    channel = factory()

    with _channel_lock:
        entry = cache.get(key)
        if entry is not None and entry[0] is conn:
            return entry[1]
        cache[key] = (conn, channel)
        cache.move_to_end(key)
        while len(cache) > _CHANNEL_CACHE_MAX:
            cache.popitem(last=False)
    return channel


def reset_channel_cache() -> None:
    """Drop all cached channels. Intended for tests and connection teardown."""
    with _channel_lock:
        _lexical_channels.clear()
        _vector_channels.clear()


def _get_lexical_channel(conn: sqlite3.Connection):
    from lummenna.force.mnemonic.retrieval_lexical import LexicalChannel

    return _cached_channel(_lexical_channels, conn, lambda: LexicalChannel(conn))


def _get_vector_channel(conn: sqlite3.Connection, config=None):
    from lummenna.force.mnemonic.retrieval_dense import VectorChannel

    if config is None:
        from lummenna.config import LummennaConfig

        config = LummennaConfig()
    resolved = config
    return _cached_channel(_vector_channels, conn, lambda: VectorChannel(resolved, conn))


def store_document(
    conn: sqlite3.Connection,
    content: str,
    room_name: str,
    locus_name: str | None = None,
    source_type: str = "import",
    source_ref: str | None = None,
    embedder: Any = None,
    vm_weights: dict | None = None,
    config=None,
    tenant_id: str = "default",
    max_tokens: int = 448,
) -> list[int]:
    """Chunk *content* semantically and store one chunk row per segment.

    This is the attachment/ingestion write path. Long documents are split with
    :func:`lummenna.force.contextual.chunker.semantic_chunks` (heading- and
    paragraph-aware, embedder-window-bounded) so every segment's dense vector
    represents the segment's actual text rather than the document's first ~512
    tokens. Each row's provenance carries the same ``source_ref`` (generated
    from the plaintext hash when omitted) plus a ``segment:N/M`` marker, so a
    document's segments are linked and reconstructable via
    :func:`get_document`.

    Args:
        conn: Open palace connection.
        content: Full document text.
        room_name: Room to store segments in (created on demand).
        locus_name: Optional locus; segments share it (created on demand).
        source_type: Provenance type for every segment. Must be in SOURCE_TYPES.
        source_ref: Shared provenance reference (attachment id, URL, hash...).
            Generated deterministically from the plaintext hash when omitted.
        embedder: Embedder with ``encode_single``; segments are embedded
            individually. When None, segments are stored lexical-only (never
            mock-embedded).
        vm_weights: Optional V(m) weight override.
        config: Runtime config (defaults to LummennaConfig()).
        tenant_id: Tenant scope for every segment.
        max_tokens: Chunking window in tokens (embedder window is 512; the
            default 448 leaves headroom for special tokens).

    Returns:
        The stored chunk_ids in document order (dedup may collapse segments;
        collapsed segments are reported once via the surviving row).

    Raises:
        ValueError: On source_type violation (raised by store_memory).
    """
    if config is None:
        from lummenna.config import LummennaConfig

        config = LummennaConfig()

    from lummenna.force.contextual.chunker import semantic_chunks

    segments = semantic_chunks(
        content,
        max_tokens=max_tokens,
        counter=None,
        embedder=embedder,
    )
    if not segments:
        return []

    if source_ref is None:
        source_ref = f"doc:{hashlib.sha256(content.encode()).hexdigest()[:16]}"
    total = len(segments)

    chunk_ids: list[int] = []
    for index, segment in enumerate(segments):
        embedding = embedder.encode_single(segment) if embedder is not None else None
        chunk_ids.append(
            store_memory(
                conn,
                content=segment,
                room_name=room_name,
                locus_name=locus_name,
                source_type=source_type,
                source_ref=f"{source_ref}#seg{index + 1}/{total}",
                embedding=embedding,
                vm_weights=vm_weights,
                config=config,
                tenant_id=tenant_id,
            )
        )
    logger.info(
        "document_stored",
        room=room_name,
        segments=total,
        source_ref=source_ref,
    )
    return chunk_ids


def get_document(conn: sqlite3.Connection, source_ref: str) -> list[dict]:
    """Reconstruct a document's segments in order from a bare source_ref.

    Args:
        conn: Open palace connection.
        source_ref: The reference passed to (or generated by) store_document,
            WITHOUT the ``#segN/M`` suffix.

    Returns:
        Ordered list of ``{chunk_id, segment, content}`` dicts for valid
        (non-superseded) segments; empty when the reference is unknown.
    """
    rows = conn.execute(
        """
        SELECT c.chunk_id, p.source_ref, c.content
        FROM chunk c
        JOIN provenance p ON p.chunk_id = c.chunk_id
        WHERE p.source_ref LIKE ? || '#seg%'
          AND c.valid_to IS NULL
        ORDER BY c.chunk_id
        """,
        (source_ref,),
    ).fetchall()
    segments: list[dict] = []
    for row in rows:
        # Row access must work under both sqlite3.Row and sqlcipher's default
        # tuple cursors (get_connection downgrades the factory for sqlcipher).
        if isinstance(row, sqlite3.Row):
            chunk_id, source_ref_row, content = row["chunk_id"], row["source_ref"], row["content"]
        else:
            chunk_id, source_ref_row, content = row[0], row[1], row[2]
        marker = str(source_ref_row).rsplit("#seg", 1)[-1]
        position = marker.split("/", 1)[0]
        try:
            seg_index = int(position) - 1
        except ValueError:
            continue
        segments.append({"chunk_id": int(chunk_id), "segment": seg_index, "content": content})
    segments.sort(key=lambda s: s["segment"])
    return segments


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
        from lummenna.config import LummennaConfig

        config = LummennaConfig()

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
        from lummenna.compliance.safety_forgetting import apply_pii_strategy, safety_scan_chunk

        if config.pii_custom_patterns:
            config.assert_licensed("compliance")
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
    from lummenna.data.schema import get_encryption

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
        from lummenna.licensing import assert_seat_available

        assert_seat_available(conn, tenant_id, config)

    # Acquire channels before opening the transaction. Their first construction
    # issues DDL (CREATE TABLE IF NOT EXISTS) and loads the sqlite-vec
    # extension; doing that inside `with conn:` mixes schema changes into a data
    # transaction, whose interaction with sqlite3's implicit transaction control
    # differs across the supported 3.10 to 3.12 range. After the first call both
    # are served from the cache.
    vector_channel = _get_vector_channel(conn, config) if embedding is not None else None
    lexical_channel = _get_lexical_channel(conn)

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

        # 2b. Per-tenant write quota (config.tenant_max_active_chunks; 0 = off).
        # Checked after dedup (a dedup writes nothing) and before any insert,
        # inside the caller's transaction so a concurrent writer cannot race
        # past the cap un-noticed: two writers both see count == limit-1, both
        # insert, SQLite's single-writer WAL means one commits first; the
        # second's count query runs after the first's commit (busy_timeout
        # serialises the write lock), so it sees the updated count and fails.
        limit = int(getattr(config, "tenant_max_active_chunks", 0))
        if limit > 0:
            if has_tenant:
                row = conn.execute(
                    "SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL AND tenant_id = ?",
                    (tenant_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL"
                ).fetchone()
            current = int(row[0]) if row else 0
            if current >= limit:
                from lummenna.brand.errors import TenantQuotaError

                raise TenantQuotaError(tenant_id=tenant_id, limit=limit, current=current)

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
        if vector_channel is not None and embedding is not None:
            vector_channel.add(chunk_id, embedding)

        # 8. Lexical index (always index plaintext so search works)
        lexical_channel.index_chunk(chunk_id, _plain_content)

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
    from lummenna.force.mnemonic.forgetting_l2_interference import check_locus_interference

    if embedding is not None:
        check_locus_interference(conn, room_id, locus_id, new_chunk_id, embedding)
