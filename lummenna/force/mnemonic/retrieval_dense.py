"""A3: Vector Channel (sqlite-vec + USearch Adapter).

Input wire: sqlite-vec (<=50k) OR USearch (50k-500k)
Output wire: C2 (fusion engine), D10 (progressive quantization)
Secret sauce: FRQAD distance metric switch-in (Task A4)
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from lummenna.logging import get_console_logger

logger = get_console_logger(__name__)


@dataclass(frozen=True)
class DenseHit:
    chunk_id: int
    score: float  # FRQAD geodesic distance (lower = closer) or cosine (higher = closer)
    vector: np.ndarray


_VEC_FALLBACK_DDL = """
    CREATE TABLE IF NOT EXISTS vec_fallback (
        chunk_id INTEGER PRIMARY KEY,
        embedding BLOB NOT NULL
    )
"""


def _create_vec_fallback(conn: sqlite3.Connection) -> None:
    """Create the durable vector mirror if absent.

    ``vec_fallback`` is the single source of truth for embeddings: it is read by
    the fusion rerank, by L2 interference detection and by L3 eviction. Every
    backend must maintain it, or those three subsystems silently no-op.
    """
    conn.execute(_VEC_FALLBACK_DDL)


class VectorBackend(Protocol):
    def add(self, chunk_id: int, vector: np.ndarray) -> None: ...
    def search(self, query_vector: np.ndarray, k: int) -> list[DenseHit]: ...
    def remove(self, chunk_id: int) -> None: ...
    def degrade(self, chunk_id: int, new_resolution: str) -> None: ...
    def flush(self) -> None: ...


class SqliteVecBackend:
    """For < 50k memories. Single-file, zero process."""

    def __init__(self, conn: sqlite3.Connection, dims: int):
        self.conn = conn
        self.dims = dims
        self._has_sqlite_vec = False
        try:
            import sqlite_vec

            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self._has_sqlite_vec = True
        except Exception as exc:
            logger.debug("sqlite_vec_load_failed", error=str(exc))

        # Always maintain vec_fallback as a direct blob-access mirror
        _create_vec_fallback(conn)

        if self._has_sqlite_vec:
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                    chunk_id INTEGER PRIMARY KEY,
                    embedding float[{dims}] distance_metric=cosine
                )
            """)
            # sqlite-vec changed its KNN query grammar across releases: older
            # builds accept `... ORDER BY distance LIMIT ?`, while >= 0.1.x
            # require an explicit `AND k = ?` constraint in the WHERE clause and
            # reject a bare LIMIT. Probe once and remember which form to use so
            # search() works regardless of the installed extension version.
            self._knn_uses_k_constraint = self._detect_knn_syntax()

    def _detect_knn_syntax(self) -> bool:
        """Return True if this sqlite-vec build requires the `k = ?` KNN form."""
        try:
            probe = np.zeros(self.dims, dtype=np.float32).tobytes()
            self.conn.execute(
                "SELECT chunk_id FROM vec_chunks WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (probe, 1),
            ).fetchall()
            return False  # LIMIT form accepted
        except sqlite3.OperationalError:
            return True  # requires `k = ?`

    def add(self, chunk_id: int, vector: np.ndarray) -> None:
        blob = vector.astype(np.float32).tobytes()
        self.conn.execute(
            "INSERT OR REPLACE INTO vec_fallback(chunk_id, embedding) VALUES (?,?)",
            (chunk_id, blob),
        )
        if self._has_sqlite_vec:
            self.conn.execute("DELETE FROM vec_chunks WHERE chunk_id = ?", (chunk_id,))
            self.conn.execute(
                "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?,?)", (chunk_id, blob)
            )

    def search(self, query_vector: np.ndarray, k: int) -> list[DenseHit]:
        blob = query_vector.astype(np.float32).tobytes()
        if self._has_sqlite_vec:
            if getattr(self, "_knn_uses_k_constraint", False):
                # sqlite-vec >= 0.1.x grammar: k is a WHERE constraint.
                rows = self.conn.execute(
                    "SELECT chunk_id, distance FROM vec_chunks "
                    "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (blob, k),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT chunk_id, distance FROM vec_chunks "
                    "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                    (blob, k),
                ).fetchall()
            return [DenseHit(cid, 1.0 - (dist / 2.0), np.array([])) for cid, dist in rows]
        else:
            return self._brute_force_search(query_vector, k)

    def _brute_force_search(self, query_vector: np.ndarray, k: int) -> list[DenseHit]:
        rows = self.conn.execute("SELECT chunk_id, embedding FROM vec_fallback").fetchall()
        q = query_vector.astype(np.float32)
        qn = np.linalg.norm(q)
        hits = []
        for cid, emb_blob in rows:
            vec = np.frombuffer(emb_blob, dtype=np.float32)
            vn = np.linalg.norm(vec)
            sim = 0.0 if vn == 0 or qn == 0 else float(np.dot(q, vec) / (qn * vn))
            hits.append(DenseHit(cid, sim, vec))
        hits.sort(key=lambda x: x.score, reverse=True)
        return hits[:k]

    def remove(self, chunk_id: int) -> None:
        self.conn.execute("DELETE FROM vec_fallback WHERE chunk_id = ?", (chunk_id,))
        if self._has_sqlite_vec:
            self.conn.execute("DELETE FROM vec_chunks WHERE chunk_id = ?", (chunk_id,))

    def degrade(self, chunk_id: int, new_resolution: str) -> None:
        """Re-quantize the vector from FP32->FP16->INT8->BINARY.

        Falls back to removal if the quantizer is unavailable.
        """
        try:
            from lummenna.sovereign.optical import QUANTIZERS

            if new_resolution in QUANTIZERS:
                row = self.conn.execute(
                    "SELECT embedding FROM vec_fallback WHERE chunk_id = ?",
                    (chunk_id,),
                ).fetchone()
                if row:
                    original = np.frombuffer(row[0], dtype=np.float32)
                    quantized = QUANTIZERS[new_resolution](original)
                    blob = np.asarray(quantized, dtype=np.float32).tobytes()
                    self.conn.execute(
                        "UPDATE vec_fallback SET embedding = ? WHERE chunk_id = ?",
                        (blob, chunk_id),
                    )
                    logger.info(
                        "vector_degraded",
                        chunk_id=chunk_id,
                        resolution=new_resolution,
                    )
                    return
        except Exception:
            logger.exception(
                "vector_degrade_failed",
                chunk_id=chunk_id,
                resolution=new_resolution,
            )
        self.remove(chunk_id)

    def flush(self) -> None:
        """No-op: this backend writes straight through to SQLite.

        Present so every backend satisfies :class:`VectorBackend` and callers
        never need to branch on the configured index type.
        """
        return


class FakeSqliteVecBackend(SqliteVecBackend):
    """Explicit fallback brute-force backend with identical interface."""

    def __init__(self, conn: sqlite3.Connection, dims: int):
        self.conn = conn
        self.dims = dims
        self._has_sqlite_vec = False
        _create_vec_fallback(conn)


class USearchBackend:
    """For 50k-500k memories. HNSW index over a durable SQLite mirror.

    Durability model: ``vec_fallback`` in SQLite is the source of truth and is
    written inside the caller's transaction, so a vector is durable the moment
    ``store_memory`` commits. The ``.usearch`` file is a *rebuildable cache*
    that only accelerates search.

    This split is deliberate. Persisting an HNSW index costs O(index size) per
    save, which is unaffordable per write at 500k vectors; and the previous
    design (``view()`` + never saving) lost every vector added during a session
    on process exit, silently degrading retrieval to lexical-only. Reconciling
    from the mirror at construction makes :meth:`flush` a pure optimisation:
    correctness never depends on it, and an unclean shutdown self-heals.
    """

    def __init__(self, conn: sqlite3.Connection, path: Path, dims: int):
        try:
            from usearch.index import Index, MetricKind, ScalarKind
        except Exception as exc:
            raise NotImplementedError("usearch is not installed") from exc
        self.conn = conn
        self.path = path
        self.dims = dims
        self._dirty = False
        _create_vec_fallback(conn)
        self.index = Index(
            ndim=dims,
            metric=MetricKind.Cos,
            dtype=ScalarKind.F32,
            expansion_add=128,
            expansion_search=64,
        )
        if path.exists():
            # load(), not view(): view() maps the file read-only, so the index
            # cannot be durably extended afterwards.
            try:
                self.index.load(str(path))
            except Exception as exc:
                logger.warning("usearch_load_failed_rebuilding", path=str(path), error=str(exc))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._reconcile_from_mirror()

    def _reconcile_from_mirror(self) -> None:
        """Insert any mirrored vector the on-disk index is missing.

        Bounded by the number of vectors written since the last successful
        :meth:`flush`, not by the size of the store, so a clean restart is O(0).
        """
        restored = 0
        for chunk_id, blob in self.conn.execute(
            "SELECT chunk_id, embedding FROM vec_fallback"
        ).fetchall():
            if chunk_id in self.index:
                continue
            vector = np.frombuffer(blob, dtype=np.float32)
            if vector.size != self.dims:
                logger.warning(
                    "usearch_mirror_dim_mismatch",
                    chunk_id=chunk_id,
                    expected=self.dims,
                    actual=int(vector.size),
                )
                continue
            self.index.add(chunk_id, vector)
            restored += 1
        if restored:
            self._dirty = True
            logger.info("usearch_reconciled_from_mirror", restored=restored)

    def add(self, chunk_id: int, vector: np.ndarray) -> None:
        blob = vector.astype(np.float32).tobytes()
        # Durable write first: this participates in the caller's transaction.
        self.conn.execute(
            "INSERT OR REPLACE INTO vec_fallback(chunk_id, embedding) VALUES (?,?)",
            (chunk_id, blob),
        )
        if chunk_id in self.index:
            self.index.remove(chunk_id)
        self.index.add(chunk_id, vector.astype(np.float32))
        self._dirty = True

    def search(self, query_vector: np.ndarray, k: int) -> list[DenseHit]:
        # A single (1-D) query vector always yields an iterable `Matches`; usearch's
        # stub types the return as `Matches | BatchMatches`, so mypy can't see the
        # single-vector iterability. Safe to iterate here.
        matches = self.index.search(query_vector.astype(np.float32), k)
        return [DenseHit(int(m.key), float(m.distance), np.array([])) for m in matches]  # type: ignore[union-attr]

    def remove(self, chunk_id: int) -> None:
        self.conn.execute("DELETE FROM vec_fallback WHERE chunk_id = ?", (chunk_id,))
        try:
            if chunk_id in self.index:
                self.index.remove(chunk_id)
                self._dirty = True
        except Exception as exc:
            logger.debug("usearch_remove_failed", chunk_id=chunk_id, error=str(exc))

    def degrade(self, chunk_id: int, new_resolution: str) -> None:
        """Re-quantize the mirrored vector and re-insert it into the index.

        The mirror holds the full FP32 blob, so unlike the previous
        implementation this does not have to fall back to removal: the vector
        survives degradation at reduced precision and the chunk stays
        semantically searchable.
        """
        try:
            from lummenna.sovereign.optical import QUANTIZERS

            if new_resolution in QUANTIZERS:
                row = self.conn.execute(
                    "SELECT embedding FROM vec_fallback WHERE chunk_id = ?", (chunk_id,)
                ).fetchone()
                if row:
                    original = np.frombuffer(row[0], dtype=np.float32)
                    quantized = np.asarray(QUANTIZERS[new_resolution](original), dtype=np.float32)
                    self.conn.execute(
                        "UPDATE vec_fallback SET embedding = ? WHERE chunk_id = ?",
                        (quantized.tobytes(), chunk_id),
                    )
                    if chunk_id in self.index:
                        self.index.remove(chunk_id)
                    self.index.add(chunk_id, quantized)
                    self._dirty = True
                    logger.info("vector_degraded", chunk_id=chunk_id, resolution=new_resolution)
                    return
        except Exception:
            logger.exception("vector_degrade_failed", chunk_id=chunk_id, resolution=new_resolution)
        self.remove(chunk_id)

    def flush(self) -> None:
        """Persist the HNSW index to disk if it has changed.

        Optional: the mirror already guarantees durability. Calling this simply
        makes the next construction skip reconciliation.
        """
        if not self._dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.index.save(str(self.path))
            self._dirty = False
        except Exception as exc:
            # Never fail a write path over a cache save; the mirror is durable.
            logger.warning("usearch_flush_failed", path=str(self.path), error=str(exc))


class VectorChannel:
    """Runtime switchable backend; config decides, not the caller."""

    def __init__(self, config, conn: sqlite3.Connection):
        from lummenna.config import LummennaConfig

        cfg: LummennaConfig = config
        if cfg.vector_index == "sqlite-vec":
            self.backend: VectorBackend = SqliteVecBackend(conn, cfg.embedding_dims)
        elif cfg.vector_index == "usearch":
            try:
                self.backend = USearchBackend(
                    conn, cfg.store_path / "vectors.usearch", cfg.embedding_dims
                )
            except Exception as exc:
                logger.error(
                    "usearch_unavailable_falling_back_to_brute_force",
                    error=str(exc),
                )
                self.backend = FakeSqliteVecBackend(conn, cfg.embedding_dims)
        else:
            from lummenna.brand.errors import RetrievalEmptyError

            raise RetrievalEmptyError(f"Unknown vector backend: {cfg.vector_index}")

    def add(self, chunk_id: int, vector: np.ndarray) -> None:
        self.backend.add(chunk_id, vector)

    def search(self, query_vector: np.ndarray, k: int) -> list[DenseHit]:
        return self.backend.search(query_vector, k)

    def remove(self, chunk_id: int) -> None:
        self.backend.remove(chunk_id)

    def degrade(self, chunk_id: int, new_resolution: str) -> None:
        self.backend.degrade(chunk_id, new_resolution)

    def flush(self) -> None:
        """Persist any backend-side index cache. No-op for SQLite backends."""
        self.backend.flush()
