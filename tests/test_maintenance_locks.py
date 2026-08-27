"""Tests for cross-process advisory locking of maintenance passes.

These cover the primitive in ``lummenna.data.locks`` and the three passes that
use it. The cross-process cases spawn real subprocesses, because a lock that
only excludes threads would pass an in-process test while leaving the actual
failure mode (a CLI daemon racing an editor's MCP server) wide open.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from lummenna.data.locks import (
    LOCK_SUFFIX,
    MaintenanceLock,
    database_path,
    maintenance_guard,
    maintenance_lock_for,
)
from lummenna.data.schema import init_db

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def file_db(tmp_path):
    """Yield a connection to an on-disk database, which locking applies to."""
    conn = sqlite3.connect(tmp_path / "lummenna.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield conn
    conn.close()


def _spawn_holder(db_file, hold_seconds: float, ready_file) -> subprocess.Popen:
    """Start a process that holds the maintenance lock, then waits.

    The child touches ``ready_file`` only after the lock is held, so the parent
    can synchronise without sleeping for an arbitrary interval.
    """
    script = textwrap.dedent(
        f"""
        import pathlib, sqlite3, time
        from lummenna.data.locks import maintenance_lock_for

        conn = sqlite3.connect({str(db_file)!r})
        lock = maintenance_lock_for(conn)
        assert lock.acquire(timeout=10.0), "child could not take the lock"
        pathlib.Path({str(ready_file)!r}).write_text("held")
        time.sleep({hold_seconds!r})
        lock.release()
        """
    )
    env = {**os.environ, "PYTHONPATH": REPO_ROOT}
    return subprocess.Popen([sys.executable, "-c", script], env=env)


def _await_ready(ready_file, proc, timeout: float = 20.0) -> None:
    """Block until the child signals it holds the lock."""
    deadline = time.monotonic() + timeout
    while not ready_file.exists():
        if proc.poll() is not None:
            raise AssertionError(f"lock holder exited early with {proc.returncode}")
        if time.monotonic() > deadline:
            raise AssertionError("lock holder never acquired the lock")
        time.sleep(0.01)


class TestDatabasePathResolution:
    def test_on_disk_database_resolves_to_its_file(self, file_db, tmp_path):
        assert database_path(file_db) == tmp_path / "lummenna.db"

    def test_in_memory_database_has_no_path(self, memory_db):
        assert database_path(memory_db) is None

    def test_lock_file_sits_beside_the_database(self, file_db, tmp_path):
        lock = maintenance_lock_for(file_db)
        assert lock.path == tmp_path / ("lummenna.db" + LOCK_SUFFIX)

    def test_lock_never_collides_with_the_database_file(self, file_db):
        """Locking the database file itself would deadlock against SQLite."""
        lock = maintenance_lock_for(file_db)
        assert lock.path != database_path(file_db)


class TestInertLock:
    """In-memory databases cannot be shared, so the lock is a no-op."""

    def test_acquire_always_succeeds(self, memory_db):
        lock = maintenance_lock_for(memory_db)
        assert lock.path is None
        assert lock.acquire() is True
        assert lock.acquire() is True
        lock.release()
        lock.release()
        assert lock.is_held is False

    def test_guard_yields_true(self, memory_db):
        with maintenance_guard(memory_db, "decay") as acquired:
            assert acquired is True

    def test_creates_no_file(self, memory_db, tmp_path):
        with maintenance_guard(memory_db, "decay"):
            pass
        assert list(tmp_path.iterdir()) == []


class TestArgumentValidation:
    def test_negative_timeout_rejected(self, tmp_path):
        lock = MaintenanceLock(tmp_path / "x.lock")
        with pytest.raises(ValueError, match="timeout must be >= 0"):
            lock.acquire(timeout=-1.0)

    def test_non_positive_poll_interval_rejected(self, tmp_path):
        lock = MaintenanceLock(tmp_path / "x.lock")
        with pytest.raises(ValueError, match="poll_interval must be > 0"):
            lock.acquire(poll_interval=0.0)

    def test_release_without_acquire_raises(self, tmp_path):
        lock = MaintenanceLock(tmp_path / "x.lock")
        with pytest.raises(RuntimeError, match="does not hold"):
            lock.release()

    def test_release_more_times_than_acquired_raises(self, tmp_path):
        lock = MaintenanceLock(tmp_path / "x.lock")
        assert lock.acquire()
        lock.release()
        with pytest.raises(RuntimeError, match="does not hold"):
            lock.release()


class TestReentrancy:
    """SleepScheduler holds the window lock while inner passes re-take it."""

    def test_same_thread_can_reacquire(self, tmp_path):
        outer = MaintenanceLock(tmp_path / "x.lock")
        inner = MaintenanceLock(tmp_path / "x.lock")
        assert outer.acquire() is True
        assert inner.acquire() is True, "separate handle, same thread, must re-enter"
        inner.release()
        assert outer.is_held is True
        outer.release()

    def test_lock_is_free_only_after_the_outer_release(self, tmp_path):
        outer = MaintenanceLock(tmp_path / "x.lock")
        inner = MaintenanceLock(tmp_path / "x.lock")
        outer.acquire()
        inner.acquire()
        inner.release()

        released_early = []

        def other_thread():
            contender = MaintenanceLock(tmp_path / "x.lock")
            if contender.acquire():
                released_early.append(True)
                contender.release()

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(timeout=5.0)
        assert released_early == [], "inner release must not free the outer hold"
        outer.release()

    def test_nested_guards_do_not_deadlock(self, file_db):
        with maintenance_guard(file_db, "sleep_cycle") as outer:
            assert outer is True
            with maintenance_guard(file_db, "decay") as inner:
                assert inner is True


class TestThreadExclusion:
    def test_second_thread_is_refused_while_held(self, tmp_path):
        lock = MaintenanceLock(tmp_path / "x.lock")
        assert lock.acquire()
        result: list[bool] = []

        def contend():
            other = MaintenanceLock(tmp_path / "x.lock")
            result.append(other.acquire(timeout=0.0))

        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=5.0)
        lock.release()
        assert result == [False]

    def test_thread_acquires_once_released(self, tmp_path):
        lock = MaintenanceLock(tmp_path / "x.lock")
        lock.acquire()
        result: list[bool] = []
        started = threading.Event()

        def contend():
            other = MaintenanceLock(tmp_path / "x.lock")
            started.set()
            got = other.acquire(timeout=5.0)
            result.append(got)
            if got:
                other.release()

        thread = threading.Thread(target=contend)
        thread.start()
        started.wait(timeout=5.0)
        time.sleep(0.1)
        lock.release()
        thread.join(timeout=10.0)
        assert result == [True]


class TestCrossProcessExclusion:
    """The failure mode this module exists to prevent."""

    def test_second_process_cannot_take_a_held_lock(self, file_db, tmp_path):
        ready = tmp_path / "ready"
        proc = _spawn_holder(tmp_path / "lummenna.db", 3.0, ready)
        try:
            _await_ready(ready, proc)
            lock = maintenance_lock_for(file_db)
            assert lock.acquire(timeout=0.0) is False
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_guard_skips_when_another_process_holds_the_lock(self, file_db, tmp_path):
        ready = tmp_path / "ready"
        proc = _spawn_holder(tmp_path / "lummenna.db", 3.0, ready)
        try:
            _await_ready(ready, proc)
            with maintenance_guard(file_db, "consolidation") as acquired:
                assert acquired is False
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_lock_becomes_available_after_holder_exits(self, file_db, tmp_path):
        ready = tmp_path / "ready"
        proc = _spawn_holder(tmp_path / "lummenna.db", 0.2, ready)
        _await_ready(ready, proc)
        proc.wait(timeout=15)
        lock = maintenance_lock_for(file_db)
        assert lock.acquire(timeout=5.0) is True
        lock.release()

    @pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is POSIX-only")
    def test_killed_holder_does_not_strand_the_lock(self, file_db, tmp_path):
        """The kernel drops advisory locks on death, so there is no stale state.

        This is the whole reason for using an OS lock instead of a PID file:
        a pass killed mid-flight must not wedge maintenance forever.
        """
        ready = tmp_path / "ready"
        proc = _spawn_holder(tmp_path / "lummenna.db", 60.0, ready)
        _await_ready(ready, proc)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=15)

        lock = maintenance_lock_for(file_db)
        assert lock.acquire(timeout=10.0) is True, "SIGKILL stranded the lock"
        lock.release()

    def test_waiting_process_acquires_after_holder_releases(self, file_db, tmp_path):
        ready = tmp_path / "ready"
        proc = _spawn_holder(tmp_path / "lummenna.db", 0.5, ready)
        try:
            _await_ready(ready, proc)
            lock = maintenance_lock_for(file_db)
            assert lock.acquire(timeout=15.0) is True
            lock.release()
        finally:
            proc.wait(timeout=15)


class TestGuardReleasesOnFailure:
    def test_exception_inside_the_guard_still_releases(self, file_db):
        with pytest.raises(ValueError), maintenance_guard(file_db, "decay"):
            raise ValueError("pass blew up")

        with maintenance_guard(file_db, "decay") as acquired:
            assert acquired is True, "a failed pass must not strand the lock"

    def test_lock_file_is_reusable_across_acquisitions(self, file_db):
        for _ in range(5):
            with maintenance_guard(file_db, "decay") as acquired:
                assert acquired is True


class TestPassesSkipWhenLockHeld:
    """The three passes must decline to run rather than corrupt state."""

    def test_decay_skips_and_leaves_scores_untouched(self, file_db, tmp_path, test_config):
        from lummenna.force.mnemonic.forgetting_l1_decay import ebbinghaus_decay
        from lummenna.force.mnemonic.store import store_memory

        store_memory(file_db, "a durable fact about locking", "decisions", config=test_config)
        file_db.commit()
        before = file_db.execute("SELECT chunk_id, vm_score FROM chunk").fetchall()
        assert before, "fixture must contain a chunk for the assertion to mean anything"

        ready = tmp_path / "ready"
        proc = _spawn_holder(tmp_path / "lummenna.db", 3.0, ready)
        try:
            _await_ready(ready, proc)
            assert ebbinghaus_decay(file_db, user_half_life_days=0.001) == 0
        finally:
            proc.terminate()
            proc.wait(timeout=10)

        after = file_db.execute("SELECT chunk_id, vm_score FROM chunk").fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]

    def test_eviction_skips_without_degrading_resolution(self, file_db, tmp_path, test_config):
        from lummenna.force.mnemonic.forgetting_l3_budget import budget_curated_eviction
        from lummenna.force.mnemonic.store import store_memory

        for i in range(5):
            store_memory(file_db, f"chunk number {i}", "decisions", config=test_config)
        file_db.commit()
        before = file_db.execute("SELECT chunk_id, resolution FROM chunk").fetchall()

        ready = tmp_path / "ready"
        proc = _spawn_holder(tmp_path / "lummenna.db", 3.0, ready)
        try:
            _await_ready(ready, proc)
            # target_ram_mb=0 forces the eviction path; the lock must still win.
            assert budget_curated_eviction(file_db, test_config, target_ram_mb=0) == 0
        finally:
            proc.terminate()
            proc.wait(timeout=10)

        after = file_db.execute("SELECT chunk_id, resolution FROM chunk").fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]

    def test_eviction_validates_arguments_before_locking(self, file_db, test_config):
        """Bad configuration must raise whether or not the lock is free."""
        from lummenna.force.mnemonic.forgetting_l3_budget import budget_curated_eviction

        with pytest.raises(ValueError, match="target_ram_mb must be >= 0"):
            budget_curated_eviction(file_db, test_config, target_ram_mb=-1)

    def test_consolidation_skips_when_lock_held(self, file_db, tmp_path, test_config):
        from lummenna.force.mnemonic.consolidation import run_consolidation_pass
        from lummenna.sovereign.wear import WearAwareBatcher

        ready = tmp_path / "ready"
        proc = _spawn_holder(tmp_path / "lummenna.db", 3.0, ready)
        try:
            _await_ready(ready, proc)
            batcher = WearAwareBatcher(file_db)
            assert run_consolidation_pass(test_config, batcher=batcher) == 0
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_passes_still_run_when_the_lock_is_free(self, file_db, test_config):
        """The guard must not become a permanent off switch.

        The pass is evaluated a year in the future so that both the age term
        and the recency boost have collapsed. Decaying a chunk stored moments
        ago is a no-op by design: ``retention`` is floored at the recency
        boost, which is ~1.0 for a fresh write.
        """
        from lummenna.force.mnemonic.forgetting_l1_decay import ebbinghaus_decay
        from lummenna.force.mnemonic.store import store_memory

        store_memory(file_db, "a fact that should decay away", "decisions", config=test_config)
        file_db.commit()
        before = file_db.execute("SELECT vm_score FROM chunk").fetchone()[0]
        assert before > 0.0

        a_year_on = datetime.now(timezone.utc) + timedelta(days=365)
        released = ebbinghaus_decay(file_db, user_half_life_days=0.0001, now=a_year_on)
        file_db.commit()

        assert released == 1, "decay reported no releases, so the pass did not run"
        after = file_db.execute("SELECT vm_score FROM chunk").fetchone()[0]
        assert after < before, "decay ran but changed nothing"
