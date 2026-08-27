"""Cross-process advisory locking for palace maintenance passes.

Lummenna's maintenance passes each read a large slice of the ``chunk`` table
into Python, compute new values, then write them back. SQLite's write lock
makes every individual statement atomic, but it does not span that
read-compute-write sequence. Two Lummenna processes pointed at one store, which
is the documented default (a CLI daemon running alongside an editor's MCP
server), can therefore both read the same pre-pass state and both apply the
same mutation.

The damage is silent and cumulative rather than loud:

* ``ebbinghaus_decay`` multiplies ``vm_score`` by a retention factor derived
  from the row it read. Applied twice, memories decay at roughly double the
  configured half-life and cross ``RELEASE_FLOOR`` early.
* ``budget_curated_eviction`` steps chunks down a resolution ladder
  (FP32 -> FP16 -> INT8 -> BINARY). Applied twice in one pass, an FP32 chunk
  lands on INT8, and the discarded precision cannot be recovered.
* ``run_consolidation_pass`` supersedes the losers of each duplicate group.
  Applied twice, it rewrites ``superseded_by`` and ``valid_to`` on rows that
  are already superseded, corrupting the provenance chain that
  ``lummenna.force.mnemonic.provenance`` depends on.

This module serialises those passes with an OS advisory lock taken on a file
beside the database. Two properties make it the right primitive here:

1. The kernel drops the lock when the holding process exits or the descriptor
   closes, including on SIGKILL. A crashed pass cannot strand the lock, so
   there is no stale-lock recovery path to get wrong.
2. It is advisory and separate from the database, so ordinary reads and writes
   are never blocked. Only maintenance passes contend with each other.

The lock is re-entrant within a process, which lets ``SleepScheduler`` hold it
across a whole maintenance window while the individual passes it calls take it
again without deadlocking.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from lummenna.logging import get_console_logger

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

logger = get_console_logger(__name__)

LOCK_SUFFIX = ".maintenance.lock"

# Lock files carry no data; they exist so the kernel has something to hang an
# advisory lock on. Owner-only, matching the 0o700 store directory that
# lummenna.data.schema._enforce_permissions sets.
_LOCK_FILE_MODE = 0o600


class _LockState:
    """Process-wide state for a single lock file.

    ``rlock`` provides mutual exclusion between threads of this process and the
    re-entrancy counter for the owning thread. ``fd``/``depth`` track the OS
    lock, which is taken once at depth 0 and dropped when depth returns to 0.
    """

    __slots__ = ("depth", "fd", "rlock")

    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.fd: int | None = None
        self.depth = 0


_registry_guard = threading.Lock()
_states: dict[str, _LockState] = {}


def _state_for(key: str) -> _LockState:
    """Return the process-wide state for ``key``, creating it if needed."""
    with _registry_guard:
        state = _states.get(key)
        if state is None:
            state = _LockState()
            _states[key] = state
        return state


def _try_lock_fd(fd: int) -> bool:
    """Take a non-blocking exclusive lock on ``fd``.

    Returns:
        True if the lock was taken, False if another process holds it.
    """
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_fd(fd: int) -> None:
    """Drop the exclusive lock held on ``fd``."""
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def database_path(conn: sqlite3.Connection) -> Path | None:
    """Return the file backing ``conn``'s main database.

    Args:
        conn: Any SQLite connection.

    Returns:
        The database file path, or None for in-memory and temporary databases,
        which have no filename and cannot be shared between processes.
    """
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main":
            filename = row[2]
            return Path(filename) if filename else None
    return None


class MaintenanceLock:
    """An advisory lock serialising maintenance passes over one store.

    A lock whose ``path`` is None is inert: every acquire succeeds immediately.
    That is the correct behaviour for in-memory databases, which no other
    process can open, so there is nothing to serialise against.
    """

    __slots__ = ("_held", "_key", "path")

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._key = str(path) if path is not None else None
        self._held = 0

    @property
    def is_held(self) -> bool:
        """Whether this handle currently holds the lock."""
        return self._held > 0

    def acquire(self, timeout: float = 0.0, poll_interval: float = 0.05) -> bool:
        """Take the lock, waiting up to ``timeout`` seconds.

        Args:
            timeout: Seconds to wait. 0 means try once and give up, which is
                what maintenance passes want: if another process is already
                doing the work, the correct response is to skip, not to queue.
            poll_interval: Seconds between retries. Advisory locks have no
                blocking-with-timeout primitive that is portable across
                flock and msvcrt, so contention is polled.

        Returns:
            True if the lock is now held by this handle, False on timeout.

        Raises:
            ValueError: If ``timeout`` or ``poll_interval`` is negative.
            OSError: If the lock file cannot be created or opened.
        """
        if timeout < 0.0:
            raise ValueError(f"timeout must be >= 0, got {timeout}")
        if poll_interval <= 0.0:
            raise ValueError(f"poll_interval must be > 0, got {poll_interval}")

        if self._key is None:
            self._held += 1
            return True

        deadline = time.monotonic() + timeout
        state = _state_for(self._key)

        # Threads of this process queue on the RLock; the same thread re-enters.
        if timeout > 0.0:
            entered = state.rlock.acquire(True, timeout)
        else:
            entered = state.rlock.acquire(False)
        if not entered:
            return False

        try:
            if state.depth == 0 and not self._lock_file(state, deadline, poll_interval):
                state.rlock.release()
                return False
        except BaseException:
            state.rlock.release()
            raise

        state.depth += 1
        self._held += 1
        return True

    def _lock_file(self, state: _LockState, deadline: float, poll_interval: float) -> bool:
        """Take the OS lock, polling until ``deadline``. Returns success."""
        fd = os.open(self._key, os.O_RDWR | os.O_CREAT, _LOCK_FILE_MODE)  # type: ignore[arg-type]
        try:
            while True:
                if _try_lock_fd(fd):
                    state.fd = fd
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    os.close(fd)
                    return False
                time.sleep(min(poll_interval, remaining))
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        """Release one level of this handle's hold on the lock.

        Raises:
            RuntimeError: If this handle does not hold the lock.
        """
        if self._held <= 0:
            raise RuntimeError("release() called on a MaintenanceLock this handle does not hold")

        self._held -= 1
        if self._key is None:
            return

        state = _state_for(self._key)
        state.depth -= 1
        try:
            if state.depth == 0 and state.fd is not None:
                fd, state.fd = state.fd, None
                try:
                    _unlock_fd(fd)
                finally:
                    os.close(fd)
        finally:
            state.rlock.release()


def maintenance_lock_for(conn: sqlite3.Connection) -> MaintenanceLock:
    """Return the maintenance lock guarding the store behind ``conn``.

    The lock file sits beside the database rather than inside it, so acquiring
    it never contends with ordinary reads and writes.
    """
    db_path = database_path(conn)
    if db_path is None:
        return MaintenanceLock(None)
    return MaintenanceLock(db_path.with_name(db_path.name + LOCK_SUFFIX))


@contextmanager
def maintenance_guard(
    conn: sqlite3.Connection, operation: str, timeout: float = 0.0
) -> Iterator[bool]:
    """Serialise a maintenance pass against other processes.

    Yields True if the caller owns the maintenance window and should proceed,
    or False if another process is already running a pass. Skipping is the
    correct response to contention: these passes are idle-time housekeeping
    that will run again on the next cycle, and running one concurrently
    corrupts ``vm_score``, the resolution ladder, and the provenance chain.

    Args:
        conn: Connection to the store being maintained.
        operation: Pass name, used in the skip log line.
        timeout: Seconds to wait for the lock before yielding False.

    Yields:
        Whether the maintenance window was acquired.

    Example:
        >>> with maintenance_guard(conn, "consolidation") as acquired:
        ...     if not acquired:
        ...         return 0
        ...     ...
    """
    lock = maintenance_lock_for(conn)
    if not lock.acquire(timeout=timeout):
        logger.info(
            "maintenance_pass_skipped",
            operation=operation,
            reason="another process holds the maintenance lock",
            lock_path=str(lock.path),
        )
        yield False
        return
    try:
        yield True
    finally:
        lock.release()
