"""Bounded SQLite connection pool for the API server.

The API previously held one module-level connection shared by every request.
Handlers were synchronous and ran on the event loop, so the single connection
was safe only because it accidentally serialized all requests. Once handlers
run on a threadpool (FastAPI sync endpoints), a shared connection is a
correctness hazard: sqlite3's implicit transaction handling would let two
interleaved requests commit each other's partial writes.

This pool keeps a bounded set of independently opened connections. A worker
``acquire()``s a connection for the duration of one request and ``release()``s
it; the same connection is never used by two callers at once. Because every
connection goes through :func:`lummenna.data.schema.get_connection`, WAL mode,
``busy_timeout``, schema ensure/migrate and permission enforcement all apply
identically to pooled connections — except that schema setup and the O(store)
permission sweep run once per *pooled* connection instead of once per request
(see ``get_connection(ensure=False)``).
"""

from __future__ import annotations

import contextlib
import threading
from collections import deque

from lummenna.data.schema import get_connection
from lummenna.logging import get_console_logger

logger = get_console_logger(__name__)

#: Hard floor: 0 means "unpooled", which reintroduces the shared-connection
#: hazard, and a pool of 1 serializes requests. One connection per request
#: without reuse is pointless; the smallest useful pool holds 2.
MIN_POOL_SIZE = 2
#: Hard ceiling. SQLite is a single-writer database; extra writer connections
#: mostly buy lock contention, not throughput. 16 covers a default uvicorn
#: threadpool (40) only up to contention — but the practical scaling limit of
#: one WAL database is reached well before this.
MAX_POOL_SIZE = 16


class PoolExhaustedError(RuntimeError):
    """Raised when acquire() would block longer than its timeout."""


class ConnectionPool:
    """A bounded, blocking pool of fully-initialised SQLite connections."""

    def __init__(self, config, size: int) -> None:
        size = max(MIN_POOL_SIZE, min(int(size), MAX_POOL_SIZE))
        self._config = config
        self._size = size
        self._idle: deque = deque()
        self._created = 0
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._closed = False
        # Fail-loud on first connection and set up the schema: this single
        # full-setup connection (WAL pragmas, migrations, permission sweep)
        # guarantees the store is complete before any pooled connection is
        # handed out with ``ensure=False``. A broken store stops startup here,
        # not per-request.
        with self._not_empty:
            self._idle.append(get_connection(config))
            self._created = 1
        logger.info("db_pool_started", size=size)

    @property
    def size(self) -> int:
        return self._size

    @property
    def idle(self) -> int:
        with self._lock:
            return len(self._idle)

    def acquire(self, timeout: float = 30.0):
        """Check a connection out, creating up to ``size`` lazily.

        Args:
            timeout: Seconds to wait for a released connection before raising
                :class:`PoolExhaustedError`. Fail-loud beats an unbounded queue
                that turns saturation into unbounded latency.

        Raises:
            PoolExhaustedError: No connection became available within timeout.
        """
        create = False
        with self._not_empty:
            while True:
                if self._idle:
                    conn = self._idle.popleft()
                    return conn
                if self._closed:
                    raise PoolExhaustedError("connection pool is closed")
                if self._created < self._size:
                    # Reserve the slot; construct outside the lock below —
                    # opening a connection runs migrations and pragmas and
                    # must not block concurrent acquires/releases.
                    self._created += 1
                    create = True
                    break
                if not self._not_empty.wait(timeout):
                    raise PoolExhaustedError(
                        f"no pooled connection became available within {timeout}s"
                    )
        if not create:
            raise AssertionError("unreachable: loop exits only via return or create")
        try:
            return get_connection(self._config, ensure=False)
        except BaseException:
            # Give the reserved slot back so the pool bound stays honest.
            with self._not_empty:
                self._created -= 1
                self._not_empty.notify()
            raise

    def release(self, conn) -> None:
        """Return a connection to the pool. Closed/broken connections are replaced."""
        with self._lock:
            if self._closed:
                self._close_quietly(conn)
                return
            self._idle.append(conn)
            self._not_empty.notify()

    def discard(self, conn) -> None:
        """Drop a connection suspected of being in a bad state.

        The pool slot stays reserved (``_created`` is not decremented): the
        pool may lazily replace the connection on the next acquire. This keeps
        the bound intact without tracking half-open slots.
        """
        self._close_quietly(conn)
        with self._lock:
            self._created = max(1, self._created)
            self._not_empty.notify()

    def close(self) -> None:
        """Close all idle connections. In-flight acquire() calls raise."""
        with self._lock:
            self._closed = True
            while self._idle:
                self._close_quietly(self._idle.popleft())
            self._not_empty.notify_all()
        logger.info("db_pool_stopped", size=self._size)

    @staticmethod
    def _close_quietly(conn) -> None:
        # Teardown must never raise: a half-closed connection is dropped and
        # the pool keeps draining the rest of the idle queue.
        with contextlib.suppress(Exception):  # noqa: BLE001
            conn.close()
