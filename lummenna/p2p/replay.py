# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-QuantumindSSI-Commercial
# Copyright (c) 2025-2026 QuantumindSSI. All rights reserved.
# NOTE: The durable replay-nonce store in this module is gated as Lummenna Pro
# (Advanced P2P, see COMMERCIAL-LICENSE.md, P6). Community keeps the
# process-local store (lummenna.security.crypto.InMemoryNonceStore), which is
# the default and needs no license.
"""Durable replay-nonce store for the Beam P2P transport.

Beam rejects a frame twice over: its timestamp must fall inside the signing
window, and its nonce must not have been seen before. The second check is only
as strong as the memory behind it. With the process-local store, that memory is
lost on restart, so a frame captured shortly before a crash can be replayed once
afterwards for whatever remains of its window. :class:`SqliteNonceStore` keeps
the nonce set in the Lummenna store instead, so a restart does not reopen it.

RETENTION POLICY
    A nonce is retained until ``frame_timestamp + replay_window_seconds`` and no
    longer. This bound is exact rather than heuristic: past that instant
    ``P2PSign.verify`` rejects the frame on its timestamp before the nonce is
    ever consulted, so a retained nonce could not change any verdict. Expired
    rows are deleted on an amortised schedule (see ``prune_interval_seconds``)
    rather than on every write, which keeps the cost off the per-frame path.

    The horizon is measured against the local clock. A backwards clock jump
    larger than the replay window can therefore make already-pruned nonces
    acceptable again, degrading replay protection to the timestamp check for the
    duration of the jump. Keep the node clock monotonic (NTP) if that matters.

CAPACITY POLICY
    Expiry alone bounds the table under normal traffic, since nothing is kept
    longer than one replay window. It does not bound it under flood: an attacker
    who can deliver valid-signature frames faster than the window retires them
    would otherwise grow the table without limit. ``max_entries`` caps it, and
    the overflow is discarded nearest-expiry first, so the nonces sacrificed are
    always the ones with the least protective value left. Overflow eviction is
    logged at WARNING because, while it is happening, evicted frames fall back to
    timestamp-window-only protection.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import TYPE_CHECKING

from lummenna.logging import get_console_logger
from lummenna.security.crypto import DEFAULT_MAX_NONCES, ReplayStoreError

if TYPE_CHECKING:
    from lummenna.config import LummennaConfig

logger = get_console_logger(__name__)

#: Table holding recently-seen Beam frame nonces. WITHOUT ROWID keeps the 32-byte
#: nonce as the sole B-tree key rather than duplicating it into a secondary index,
#: which is what makes the hot path a single primary-key probe.
NONCE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS p2p_replay_nonce (
    nonce      BLOB NOT NULL PRIMARY KEY,
    expires_at INTEGER NOT NULL
) WITHOUT ROWID
"""

#: Expiry-ordered index. Both retention pruning and overflow eviction scan in
#: expiry order, so without this each pass would be a full table scan.
NONCE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_p2p_replay_nonce_expires
    ON p2p_replay_nonce(expires_at)
"""

#: How often expired nonces are swept, in seconds. Sweeping on every frame would
#: put a DELETE on the per-packet path for no security gain: a not-yet-swept
#: expired nonce cannot cause a wrong verdict, it only occupies a row.
DEFAULT_PRUNE_INTERVAL_SECONDS: int = 30


def create_nonce_table(conn: sqlite3.Connection) -> None:
    """Create the replay-nonce table and its index if they are absent.

    Idempotent, so it is safe both as a migration step and as a self-check when a
    store is opened on a connection whose schema version is unknown.
    """
    conn.execute(NONCE_TABLE_SQL)
    conn.execute(NONCE_INDEX_SQL)


class SqliteNonceStore:
    """Replay-nonce store backed by the Lummenna SQLite database.

    Implements ``lummenna.security.crypto.ReplayNonceStore``. Durable across
    process restarts and safe across concurrent callers, including callers in
    other processes: the uniqueness verdict is decided by a primary-key conflict
    inside SQLite, not by a read-then-write in Python, so two callers racing on
    the same nonce cannot both be told it was fresh.

    Cross-*thread* use additionally requires a connection that allows it
    (``check_same_thread=False``), since sqlite3 otherwise refuses the call
    outright. :meth:`open` builds one that way; a connection supplied directly by
    a caller is the caller's responsibility.

    Because :meth:`open` takes its connection from ``get_connection``, the store
    also inherits WAL mode, the busy timeout, ``~/.lummenna`` permission
    hardening, and encryption-at-rest when SQLCipher is configured.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        max_entries: int = DEFAULT_MAX_NONCES,
        prune_interval_seconds: int = DEFAULT_PRUNE_INTERVAL_SECONDS,
        owns_connection: bool = False,
    ):
        """Wrap *conn* as a nonce store.

        Args:
            conn: An open SQLite connection. The store issues its own COMMITs, so
                the connection must not be shared with a caller that manages
                transactions of its own. :meth:`open` supplies a dedicated one.
            max_entries: Ceiling on retained nonces. See CAPACITY POLICY.
            prune_interval_seconds: Minimum seconds between expiry sweeps.
            owns_connection: Whether :meth:`close` should close *conn*.

        Raises:
            ValueError: If a bound is non-positive.
            ReplayStoreError: If the table cannot be created.
        """
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        if prune_interval_seconds < 1:
            raise ValueError("prune_interval_seconds must be >= 1")
        self._conn = conn
        self._max_entries = max_entries
        self._prune_interval = prune_interval_seconds
        self._owns_connection = owns_connection
        self._closed = False
        # Non-reentrant: every public method acquires it exactly once and
        # delegates to a _locked helper, so re-entry would be a bug, not a case
        # to tolerate.
        self._lock = threading.Lock()
        self._next_prune_at = 0.0
        try:
            create_nonce_table(conn)
            conn.commit()
        except sqlite3.Error as exc:
            raise ReplayStoreError(f"Cannot initialise the replay-nonce table: {exc}") from exc

    @classmethod
    def open(
        cls,
        config: LummennaConfig,
        *,
        max_entries: int = DEFAULT_MAX_NONCES,
        prune_interval_seconds: int = DEFAULT_PRUNE_INTERVAL_SECONDS,
    ) -> SqliteNonceStore:
        """Open a durable nonce store against the configured Lummenna database.

        Pro-gated (``advanced_p2p``): durable replay protection is a commercial
        Advanced P2P capability. Community uses the process-local store, which
        remains the default and is not gated.

        Args:
            config: Supplies the store path and license entitlement.
            max_entries: Ceiling on retained nonces.
            prune_interval_seconds: Minimum seconds between expiry sweeps.

        Returns:
            A store owning its own dedicated connection.

        Raises:
            LicenseRequiredError: If the configuration is not entitled to
                ``advanced_p2p``.
            ReplayStoreError: If the database cannot be opened.
        """
        config.assert_licensed("advanced_p2p")
        # Deferred: lummenna.data.schema imports lummenna.security.crypto, so a
        # module-level import here would close an import cycle through the store
        # interface this module implements.
        from lummenna.data.schema import get_connection

        try:
            conn = get_connection(config)
        except sqlite3.Error as exc:
            raise ReplayStoreError(f"Cannot open the replay-nonce store: {exc}") from exc
        try:
            return cls(
                conn,
                max_entries=max_entries,
                prune_interval_seconds=prune_interval_seconds,
                owns_connection=True,
            )
        except Exception:
            conn.close()
            raise

    def record_if_unseen(self, nonce: bytes, expires_at: int) -> bool:
        """Record *nonce* and report whether it was previously unseen.

        Args:
            nonce: Opaque nonce bytes identifying one frame.
            expires_at: Unix timestamp past which the nonce may be discarded.

        Returns:
            True if the nonce was newly recorded, False if it was already known
            and the frame is therefore a replay.

        Raises:
            ReplayStoreError: If the store is closed or the database rejects the
                write. The nonce is not recorded and no verdict is implied.
        """
        with self._lock:
            self._assert_open()
            self._maybe_prune_locked(time.time())
            try:
                cursor = self._conn.execute(
                    "INSERT INTO p2p_replay_nonce (nonce, expires_at) VALUES (?, ?) "
                    "ON CONFLICT(nonce) DO NOTHING",
                    (nonce, int(expires_at)),
                )
                inserted = cursor.rowcount == 1
                # Committed per frame on purpose. An uncommitted nonce is not
                # durable, and a store that loses its most recent nonces on
                # restart is the exact failure this class exists to remove.
                # In WAL with synchronous=NORMAL this is a write to the log, not
                # an fsync, so the per-frame cost stays small.
                self._conn.commit()
            except sqlite3.Error as exc:
                self._rollback_quietly()
                raise ReplayStoreError(f"Replay-nonce write failed: {exc}") from exc
            return inserted

    def prune(self, now: int) -> int:
        """Delete every nonce that expired at or before *now*.

        Also enforces the capacity ceiling. Returns the number of rows deleted by
        both passes combined.

        Raises:
            ReplayStoreError: If the store is closed or the delete fails.
        """
        with self._lock:
            self._assert_open()
            return self._prune_locked(now)

    def close(self) -> None:
        """Release the store, closing the connection if this store owns it.

        Idempotent, so it is safe to call from a shutdown path that may run more
        than once.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._owns_connection:
                try:
                    self._conn.close()
                except sqlite3.Error as exc:
                    logger.warning("replay_store_close_failed", error=str(exc))

    @property
    def is_closed(self) -> bool:
        """Whether :meth:`close` has run and the store no longer answers checks."""
        return self._closed

    def __len__(self) -> int:
        """Number of nonces currently retained, including any not yet swept."""
        with self._lock:
            self._assert_open()
            try:
                row = self._conn.execute("SELECT COUNT(*) FROM p2p_replay_nonce").fetchone()
            except sqlite3.Error as exc:
                raise ReplayStoreError(f"Replay-nonce count failed: {exc}") from exc
            return int(row[0]) if row else 0

    # --- internals: callers must already hold self._lock ---

    def _assert_open(self) -> None:
        if self._closed:
            raise ReplayStoreError("Replay-nonce store is closed")

    def _maybe_prune_locked(self, now: float) -> None:
        """Sweep expired nonces if the amortisation interval has elapsed."""
        if now < self._next_prune_at:
            return
        self._next_prune_at = now + self._prune_interval
        self._prune_locked(int(now))

    def _prune_locked(self, now: int) -> int:
        try:
            deleted = self._conn.execute(
                "DELETE FROM p2p_replay_nonce WHERE expires_at <= ?", (int(now),)
            ).rowcount
            deleted += self._evict_overflow_locked()
            self._conn.commit()
        except sqlite3.Error as exc:
            self._rollback_quietly()
            raise ReplayStoreError(f"Replay-nonce prune failed: {exc}") from exc
        return max(deleted, 0)

    def _evict_overflow_locked(self) -> int:
        """Drop nearest-expiry nonces until the table is back within its ceiling.

        Runs only after expiry pruning, so it evicts live nonces exclusively when
        arrivals outpace the replay window.
        """
        row = self._conn.execute("SELECT COUNT(*) FROM p2p_replay_nonce").fetchone()
        total = int(row[0]) if row else 0
        overflow = total - self._max_entries
        if overflow <= 0:
            return 0
        evicted = self._conn.execute(
            "DELETE FROM p2p_replay_nonce WHERE nonce IN ("
            "    SELECT nonce FROM p2p_replay_nonce ORDER BY expires_at ASC LIMIT ?"
            ")",
            (overflow,),
        ).rowcount
        logger.warning(
            "replay_nonce_overflow_evicted",
            evicted=evicted,
            max_entries=self._max_entries,
            detail=(
                "Beam frames are arriving faster than the replay window retires "
                "their nonces; evicted frames fall back to timestamp-window-only "
                "replay protection until the rate drops."
            ),
        )
        return max(evicted, 0)

    def _rollback_quietly(self) -> None:
        """Undo a failed statement without masking the error that caused it."""
        try:
            self._conn.rollback()
        except sqlite3.Error as exc:
            logger.warning("replay_store_rollback_failed", error=str(exc))
