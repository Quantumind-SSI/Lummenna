"""Tests for Beam replay-nonce stores (process-local and durable).

The property under test throughout is the one an attacker cares about: a frame
that was accepted once is never accepted again while it could still pass the
timestamp check. Restarts, key rotation, concurrent delivery and flooding are all
ways of trying to get a second acceptance, so each gets a case here.

Cross-process durability is exercised with a real subprocess. A store that only
excluded a second *object* in the same interpreter would pass an in-process test
while leaving the actual failure mode — a restarted daemon, or a second process
against the same database — wide open.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
import subprocess  # nosec B404 — fixed argv, no shell; see _run_in_subprocess
import sys
import textwrap
import threading
import time

import pytest

from lummenna import licensing
from lummenna.brand.errors import LicenseRequiredError
from lummenna.config import LummennaConfig
from lummenna.data.migrate import CURRENT_SCHEMA_VERSION
from lummenna.data.schema import get_connection, init_db
from lummenna.p2p.beam import decode_frame
from lummenna.p2p.replay import SqliteNonceStore, create_nonce_table
from lummenna.security.crypto import InMemoryNonceStore, P2PSign, ReplayStoreError

REPO_ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)


# --------------------------------------------------------------------------- #
# Licensing helpers (mirrors tests/test_licensing.py so no real key is embedded)
# --------------------------------------------------------------------------- #


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _mint(priv) -> str:
    payload = json.dumps(
        {
            "sub": "Test Corp",
            "edition": "pro",
            "features": ["*"],
            "iss": "QuantumindSSI",
            "id": "test-license",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"LUMMENNA-{_b64url(payload)}.{_b64url(priv.sign(payload))}"


@pytest.fixture
def pro_license_key(monkeypatch):
    """Yield a valid Pro key, with the engine verifying against an ephemeral pair."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    monkeypatch.setattr(licensing, "_QUANTUMINDSSI_PUBLIC_KEY_B64", _b64url(pub_raw))
    licensing.clear_license_cache()
    yield _mint(priv)
    licensing.clear_license_cache()


@pytest.fixture
def file_db(tmp_path):
    """Yield a connection to an on-disk database carrying the Lummenna schema.

    ``check_same_thread=False`` mirrors how ``get_connection`` builds the
    connection a store gets in production, which is what makes cross-thread use
    legal at all.
    """
    conn = sqlite3.connect(tmp_path / "lummenna.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def durable_store(file_db):
    store = SqliteNonceStore(file_db)
    yield store
    store.close()


def _pro_config(tmp_path, license_key, **overrides):
    kwargs = {
        "device": "generic",
        "vector_index": "sqlite-vec",
        "sovereign": False,
        "license_key": license_key,
        "p2p_encryption_key": "test-p2p-key-4321",
        "store_path": str(tmp_path / ".lummenna" / "store"),
        "p2p_replay_store": "persistent",
    }
    kwargs.update(overrides)
    return LummennaConfig(**kwargs)


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #


class TestMigration:
    def test_schema_version_includes_replay_table(self):
        assert CURRENT_SCHEMA_VERSION >= 6

    def test_migration_creates_nonce_table(self, file_db):
        assert file_db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        row = file_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='p2p_replay_nonce'"
        ).fetchone()
        assert row is not None, "migration 0006 did not create p2p_replay_nonce"

    def test_migration_and_self_check_agree_on_schema(self, tmp_path):
        """The migrated table must match what a store creates on an unknown DB.

        Migration 0006 imports its DDL from ``lummenna.p2p.replay`` precisely so
        the two cannot drift; this pins that they have not been decoupled.
        """
        migrated = sqlite3.connect(tmp_path / "migrated.db")
        init_db(migrated)
        via_migration = migrated.execute(
            "SELECT sql FROM sqlite_master WHERE name='p2p_replay_nonce'"
        ).fetchone()[0]
        migrated.close()

        bare = sqlite3.connect(tmp_path / "bare.db")
        create_nonce_table(bare)
        via_self_check = bare.execute(
            "SELECT sql FROM sqlite_master WHERE name='p2p_replay_nonce'"
        ).fetchone()[0]
        bare.close()

        assert via_migration == via_self_check

    def test_create_nonce_table_is_idempotent(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "x.db")
        create_nonce_table(conn)
        create_nonce_table(conn)  # must not raise
        conn.close()


# --------------------------------------------------------------------------- #
# Store semantics — asserted identically against both implementations
# --------------------------------------------------------------------------- #


@pytest.fixture(params=["memory", "sqlite"])
def any_store(request, tmp_path):
    """Yield each store implementation so shared semantics are pinned for both."""
    if request.param == "memory":
        yield InMemoryNonceStore()
        return
    conn = sqlite3.connect(tmp_path / "lummenna.db")
    create_nonce_table(conn)
    store = SqliteNonceStore(conn, owns_connection=True)
    yield store
    store.close()


class TestStoreSemantics:
    def test_first_sighting_is_unseen(self, any_store):
        assert any_store.record_if_unseen(b"n1", int(time.time()) + 30) is True

    def test_second_sighting_is_seen(self, any_store):
        expires = int(time.time()) + 30
        any_store.record_if_unseen(b"n1", expires)
        assert any_store.record_if_unseen(b"n1", expires) is False

    def test_distinct_nonces_are_independent(self, any_store):
        expires = int(time.time()) + 30
        assert any_store.record_if_unseen(b"a", expires) is True
        assert any_store.record_if_unseen(b"b", expires) is True

    def test_prune_drops_only_expired(self, any_store):
        now = int(time.time())
        any_store.record_if_unseen(b"expired", now - 1)
        any_store.record_if_unseen(b"live", now + 30)
        assert any_store.prune(now) == 1
        # The pruned nonce is forgotten; the live one is still remembered.
        assert any_store.record_if_unseen(b"expired", now + 30) is True
        assert any_store.record_if_unseen(b"live", now + 30) is False

    def test_prune_on_empty_store_is_a_noop(self, any_store):
        assert any_store.prune(int(time.time())) == 0

    def test_boundary_expiry_is_pruned(self, any_store):
        """expires_at == now is past the horizon: the frame can no longer verify."""
        now = int(time.time())
        any_store.record_if_unseen(b"edge", now)
        assert any_store.prune(now) == 1


class TestCapacityPolicy:
    """Overflow must evict nearest-expiry first, never the freshest nonces."""

    def _store(self, kind, tmp_path, max_entries):
        if kind == "memory":
            return InMemoryNonceStore(max_entries=max_entries)
        conn = sqlite3.connect(tmp_path / "cap.db")
        create_nonce_table(conn)
        # prune_interval 1s so the sweep that enforces capacity is reachable.
        return SqliteNonceStore(
            conn, max_entries=max_entries, prune_interval_seconds=1, owns_connection=True
        )

    @pytest.mark.parametrize("kind", ["memory", "sqlite"])
    def test_ceiling_is_enforced(self, kind, tmp_path):
        store = self._store(kind, tmp_path, max_entries=10)
        now = int(time.time())
        for i in range(50):
            store.record_if_unseen(f"n{i}".encode(), now + 30 + i)
        if kind == "sqlite":
            # SQLite enforces capacity on the amortised sweep rather than on
            # every insert, so drive one deterministically.
            store.prune(now)
        assert len(store) <= 10
        if hasattr(store, "close"):
            store.close()

    @pytest.mark.parametrize("kind", ["memory", "sqlite"])
    def test_nearest_expiry_is_evicted_first(self, kind, tmp_path):
        store = self._store(kind, tmp_path, max_entries=2)
        now = int(time.time())
        store.record_if_unseen(b"soonest", now + 10)
        store.record_if_unseen(b"middle", now + 20)
        store.record_if_unseen(b"latest", now + 30)
        if kind == "sqlite":
            store.prune(now)
        # The evicted nonce is forgotten (recording it again reads as unseen);
        # the two with the most protective value left are still remembered.
        assert store.record_if_unseen(b"soonest", now + 10) is True
        assert store.record_if_unseen(b"latest", now + 30) is False
        if hasattr(store, "close"):
            store.close()

    def test_non_positive_bounds_are_rejected(self, file_db):
        with pytest.raises(ValueError, match="max_entries"):
            InMemoryNonceStore(max_entries=0)
        with pytest.raises(ValueError, match="max_entries"):
            SqliteNonceStore(file_db, max_entries=0)
        with pytest.raises(ValueError, match="prune_interval_seconds"):
            SqliteNonceStore(file_db, prune_interval_seconds=0)


# --------------------------------------------------------------------------- #
# Durability — the capability this change delivers
# --------------------------------------------------------------------------- #


class TestDurability:
    def test_nonce_survives_a_new_store_on_the_same_database(self, tmp_path):
        db = tmp_path / "lummenna.db"
        conn = sqlite3.connect(db)
        create_nonce_table(conn)
        first = SqliteNonceStore(conn, owns_connection=True)
        expires = int(time.time()) + 300
        assert first.record_if_unseen(b"survivor", expires) is True
        first.close()

        conn2 = sqlite3.connect(db)
        second = SqliteNonceStore(conn2, owns_connection=True)
        assert second.record_if_unseen(b"survivor", expires) is False
        second.close()

    def test_in_memory_store_does_not_survive(self, tmp_path):
        """Pins the Community limitation the docs state, so it stays honest."""
        expires = int(time.time()) + 300
        assert InMemoryNonceStore().record_if_unseen(b"n", expires) is True
        assert InMemoryNonceStore().record_if_unseen(b"n", expires) is True

    def test_replay_rejected_after_process_restart(self, tmp_path):
        """A frame accepted by one process is rejected by the next one.

        This is the whole point of the durable store, and it cannot be observed
        without a second process: an in-process check would also pass with the
        old in-memory set.
        """
        db = tmp_path / "lummenna.db"
        conn = sqlite3.connect(db)
        create_nonce_table(conn)
        conn.close()

        script = textwrap.dedent(
            """
            import sqlite3, sys, time
            from lummenna.p2p.replay import SqliteNonceStore

            store = SqliteNonceStore(sqlite3.connect(sys.argv[1]), owns_connection=True)
            print("UNSEEN" if store.record_if_unseen(b"cross-process", int(sys.argv[2])) else "SEEN")
            store.close()
            """
        )
        expires = str(int(time.time()) + 300)
        first = _run_in_subprocess(script, str(db), expires)
        second = _run_in_subprocess(script, str(db), expires)
        assert first == "UNSEEN"
        assert second == "SEEN", "a restarted process re-accepted an already-seen nonce"

    def test_concurrent_processes_cannot_both_accept(self, tmp_path):
        """Two processes racing the same nonce: exactly one may be told UNSEEN."""
        db = tmp_path / "lummenna.db"
        conn = sqlite3.connect(db)
        create_nonce_table(conn)
        conn.close()

        script = textwrap.dedent(
            """
            import sqlite3, sys
            from lummenna.p2p.replay import SqliteNonceStore

            conn = sqlite3.connect(sys.argv[1], timeout=30)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            store = SqliteNonceStore(conn, owns_connection=True)
            print("UNSEEN" if store.record_if_unseen(b"contended", int(sys.argv[2])) else "SEEN")
            store.close()
            """
        )
        expires = str(int(time.time()) + 300)
        procs = [
            subprocess.Popen(  # nosec B603 — fixed argv, no shell
                [sys.executable, "-c", script, str(db), expires],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=REPO_ROOT,
            )
            for _ in range(4)
        ]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, f"subprocess failed: {err}"
            results.append(out.strip().splitlines()[-1])
        assert results.count("UNSEEN") == 1, f"expected exactly one acceptance, got {results}"

    def test_store_opened_from_config_is_usable_across_threads(self, tmp_path, pro_license_key):
        """The production path must yield a store other threads can actually use."""
        store = SqliteNonceStore.open(_pro_config(tmp_path, pro_license_key))
        outcome: list[object] = []

        def attempt() -> None:
            try:
                outcome.append(store.record_if_unseen(b"from-thread", int(time.time()) + 30))
            except ReplayStoreError as exc:  # pragma: no cover - failure detail
                outcome.append(exc)

        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(timeout=30)
        store.close()
        assert outcome == [True], f"cross-thread use failed: {outcome}"

    def test_concurrent_threads_cannot_both_accept(self, durable_store):
        expires = int(time.time()) + 300
        verdicts: list[bool] = []
        barrier = threading.Barrier(8)
        lock = threading.Lock()

        def attempt() -> None:
            barrier.wait()
            verdict = durable_store.record_if_unseen(b"contended", expires)
            with lock:
                verdicts.append(verdict)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert verdicts.count(True) == 1, f"expected exactly one acceptance, got {verdicts}"


# --------------------------------------------------------------------------- #
# Fail-closed
# --------------------------------------------------------------------------- #


class TestFailClosed:
    def test_closed_store_refuses_to_answer(self, file_db):
        store = SqliteNonceStore(file_db)
        store.close()
        with pytest.raises(ReplayStoreError, match="closed"):
            store.record_if_unseen(b"n", int(time.time()) + 30)

    def test_close_is_idempotent(self, file_db):
        store = SqliteNonceStore(file_db)
        store.close()
        store.close()  # must not raise
        assert store.is_closed is True

    def test_broken_database_raises_rather_than_accepting(self, tmp_path):
        """A store that cannot write must not report a frame as fresh."""
        conn = sqlite3.connect(tmp_path / "lummenna.db")
        create_nonce_table(conn)
        store = SqliteNonceStore(conn, owns_connection=True)
        conn.execute("DROP TABLE p2p_replay_nonce")
        conn.commit()
        with pytest.raises(ReplayStoreError):
            store.record_if_unseen(b"n", int(time.time()) + 30)
        store.close()

    def test_verify_propagates_store_failure_instead_of_accepting(self, tmp_path):
        """P2PSign must surface a store outage, never silently pass the frame."""
        conn = sqlite3.connect(tmp_path / "lummenna.db")
        create_nonce_table(conn)
        store = SqliteNonceStore(conn, owns_connection=True)
        signer = P2PSign("key", nonce_store=store)
        framed = signer.sign(b"payload")
        conn.execute("DROP TABLE p2p_replay_nonce")
        conn.commit()
        with pytest.raises(ReplayStoreError):
            signer.verify(framed)
        store.close()

    def test_store_failure_is_not_a_value_error(self, tmp_path):
        """Callers branch on the difference; a bad frame and a bad store differ."""
        conn = sqlite3.connect(tmp_path / "lummenna.db")
        create_nonce_table(conn)
        store = SqliteNonceStore(conn, owns_connection=True)
        conn.execute("DROP TABLE p2p_replay_nonce")
        conn.commit()
        try:
            store.record_if_unseen(b"n", int(time.time()) + 30)
        except ReplayStoreError as exc:
            assert not isinstance(exc, ValueError)
        else:
            pytest.fail("a broken store returned a verdict")
        store.close()


# --------------------------------------------------------------------------- #
# P2PSign integration
# --------------------------------------------------------------------------- #


class TestSignerIntegration:
    def test_default_store_is_process_local(self):
        assert isinstance(P2PSign("key").nonce_store, InMemoryNonceStore)

    def test_retention_horizon_matches_the_replay_window(self, durable_store):
        """A nonce is kept exactly as long as its frame could still verify."""
        signer = P2PSign("key", replay_window_seconds=45, nonce_store=durable_store)
        before = int(time.time())
        signer.verify(signer.sign(b"payload"))
        expires_at = durable_store._conn.execute(  # noqa: SLF001 — asserting the stored horizon
            "SELECT expires_at FROM p2p_replay_nonce"
        ).fetchone()[0]
        assert before + 45 <= expires_at <= int(time.time()) + 45

    def test_replay_rejected_through_a_durable_store(self, durable_store):
        signer = P2PSign("key", nonce_store=durable_store)
        framed = signer.sign(b"payload")
        assert signer.verify(framed) == b"payload"
        with pytest.raises(ValueError, match="Replay detected"):
            signer.verify(framed)

    def test_a_second_signer_sharing_the_store_rejects_the_replay(self, durable_store):
        """Restart equivalence: a fresh signer over the same store still refuses."""
        framed = P2PSign("key", nonce_store=durable_store).sign(b"payload")
        P2PSign("key", nonce_store=durable_store).verify(framed)
        with pytest.raises(ValueError, match="Replay detected"):
            P2PSign("key", nonce_store=durable_store).verify(framed)

    def test_nonces_are_bound_to_the_signing_key(self, durable_store):
        """Why one store is safe to share across a key rotation.

        Same payload, same second, different key: the nonces must differ, or
        rotating a key would start rejecting legitimate frames as replays.
        """
        payload = b"identical"
        P2PSign("old-key", nonce_store=durable_store).verify(
            P2PSign("old-key", nonce_store=durable_store).sign(payload)
        )
        rotated = P2PSign("new-key", nonce_store=durable_store)
        assert rotated.verify(rotated.sign(payload)) == payload

    def test_tampered_frame_never_reaches_the_store(self, durable_store):
        signer = P2PSign("key", nonce_store=durable_store)
        framed = bytearray(signer.sign(b"payload"))
        framed[-1] ^= 0xFF
        with pytest.raises(ValueError, match="signature verification failed"):
            signer.verify(bytes(framed))
        assert len(durable_store) == 0, "a forged frame was recorded as seen"

    def test_expired_frame_never_reaches_the_store(self, durable_store):
        signer = P2PSign("key", replay_window_seconds=1, nonce_store=durable_store)
        framed = signer.sign(b"payload")
        time.sleep(2.1)
        with pytest.raises(ValueError, match="outside replay window"):
            signer.verify(framed)
        assert len(durable_store) == 0

    def test_non_positive_replay_window_is_rejected(self):
        with pytest.raises(ValueError, match="replay_window_seconds"):
            P2PSign("key", replay_window_seconds=0)


# --------------------------------------------------------------------------- #
# Pro gating (advanced_p2p)
# --------------------------------------------------------------------------- #


class TestProGate:
    def test_durable_store_blocked_in_community(self, tmp_path):
        licensing.clear_license_cache()
        cfg = _pro_config(tmp_path, license_key=None)
        with pytest.raises(LicenseRequiredError):
            SqliteNonceStore.open(cfg)

    def test_durable_store_allowed_with_pro(self, tmp_path, pro_license_key):
        cfg = _pro_config(tmp_path, pro_license_key)
        store = SqliteNonceStore.open(cfg)
        try:
            assert store.record_if_unseen(b"n", int(time.time()) + 30) is True
        finally:
            store.close()

    def test_beam_node_refuses_durable_store_without_a_license(self, tmp_path):
        licensing.clear_license_cache()
        from lummenna.p2p.beam import BeamNode

        cfg = _pro_config(tmp_path, license_key=None)
        with pytest.raises(LicenseRequiredError):
            BeamNode(cfg)

    def test_community_default_is_process_local_and_ungated(self, tmp_path):
        """Community keeps working with no license and no durable store."""
        licensing.clear_license_cache()
        from lummenna.p2p.beam import BeamNode

        node = BeamNode(_pro_config(tmp_path, license_key=None, p2p_replay_store="memory"))
        assert node.replay_store_is_durable is False
        assert isinstance(node._signer.nonce_store, InMemoryNonceStore)  # noqa: SLF001


# --------------------------------------------------------------------------- #
# BeamNode wiring
# --------------------------------------------------------------------------- #


class TestBeamNodeWiring:
    def test_durable_store_is_selected_by_config(self, tmp_path, pro_license_key):
        from lummenna.p2p.beam import BeamNode

        node = BeamNode(_pro_config(tmp_path, pro_license_key))
        assert node.replay_store_is_durable is True
        assert isinstance(node._signer.nonce_store, SqliteNonceStore)  # noqa: SLF001

    def test_replay_window_is_configurable(self, tmp_path, pro_license_key):
        from lummenna.p2p.beam import BeamNode

        node = BeamNode(_pro_config(tmp_path, pro_license_key, p2p_replay_window_seconds=90))
        assert node._signer.replay_window_seconds == 90  # noqa: SLF001

    def test_rotate_key_keeps_the_replay_set(self, tmp_path, pro_license_key):
        """Rotation must not be usable to wipe the nonce set and reopen replays."""
        from lummenna.p2p.beam import BeamNode

        node = BeamNode(_pro_config(tmp_path, pro_license_key))
        before = node._signer.nonce_store  # noqa: SLF001
        framed = node._signer.sign(b"payload")  # noqa: SLF001
        node._signer.verify(framed)  # noqa: SLF001
        node.rotate_key("rotated-key-0001")
        assert node._signer.nonce_store is before  # noqa: SLF001
        assert len(before) == 1, "rotation discarded the recorded nonce"

    def test_replay_survives_a_beam_node_restart(self, tmp_path, pro_license_key):
        """A frame accepted before a node is rebuilt is refused afterwards."""
        from lummenna.p2p.beam import BeamNode

        cfg = _pro_config(tmp_path, pro_license_key)
        first = BeamNode(cfg)
        framed = first._signer.sign(b"payload")  # noqa: SLF001
        assert first._signer.verify(framed) == b"payload"  # noqa: SLF001
        first._persistent_nonce_store.close()  # noqa: SLF001

        second = BeamNode(_pro_config(tmp_path, pro_license_key))
        with pytest.raises(ValueError, match="Replay detected"):
            second._signer.verify(framed)  # noqa: SLF001
        second._persistent_nonce_store.close()  # noqa: SLF001

    def test_stop_closes_the_durable_store(self, tmp_path, pro_license_key, monkeypatch):
        from lummenna.p2p import beam as beam_module

        node = beam_module.BeamNode(_pro_config(tmp_path, pro_license_key))
        monkeypatch.setattr(node, "_server", None)
        monkeypatch.setattr(node, "_browser", None)
        monkeypatch.setattr(node, "_azc", None)
        asyncio.run(node.stop())
        assert node._persistent_nonce_store.is_closed is True  # noqa: SLF001

    def test_start_reopens_a_store_closed_by_a_previous_stop(
        self, tmp_path, pro_license_key, monkeypatch
    ):
        """A stop/start cycle must not leave the node rejecting every frame.

        The durable store is closed on stop, so without a reopen the restarted
        listener would fail closed on every packet — safe, but useless.
        """
        from lummenna.p2p import beam as beam_module

        node = beam_module.BeamNode(_pro_config(tmp_path, pro_license_key))
        first_store = node._persistent_nonce_store  # noqa: SLF001
        monkeypatch.setattr(node, "_server", None)
        monkeypatch.setattr(node, "_browser", None)
        monkeypatch.setattr(node, "_azc", None)
        asyncio.run(node.stop())
        assert first_store.is_closed is True

        # Neutralise the mDNS/listener side of start(); the reopen is the subject.
        monkeypatch.setattr(beam_module, "AsyncZeroconf", _FakeZeroconf)
        monkeypatch.setattr(beam_module, "ServiceInfo", lambda *a, **k: object())
        monkeypatch.setattr(beam_module, "AsyncServiceBrowser", lambda *a, **k: object())

        async def _fake_start_server(*_args, **_kwargs):
            return object()

        monkeypatch.setattr(beam_module.asyncio, "start_server", _fake_start_server)
        asyncio.run(node.start())

        reopened = node._persistent_nonce_store  # noqa: SLF001
        assert reopened is not first_store
        assert reopened.is_closed is False
        assert node._signer.nonce_store is reopened  # noqa: SLF001
        assert reopened.record_if_unseen(b"after-restart", int(time.time()) + 30) is True
        reopened.close()

    def test_in_memory_node_restart_loses_the_set(self, tmp_path):
        """Pins the documented Community caveat rather than overstating it."""
        licensing.clear_license_cache()
        from lummenna.p2p.beam import BeamNode

        cfg = _pro_config(tmp_path, license_key=None, p2p_replay_store="memory")
        first = BeamNode(cfg)
        framed = first._signer.sign(b"payload")  # noqa: SLF001
        first._signer.verify(framed)  # noqa: SLF001
        second = BeamNode(cfg)
        assert second._signer.verify(framed) == b"payload"  # noqa: SLF001


class TestAdversarialWire:
    """Capture-and-retransmit against the real frame path, not just the signer.

    The replay check lives inside ``decode_frame``, so these drive the bytes an
    attacker would actually capture off the LAN through the same function the
    listener uses.
    """

    @staticmethod
    async def _decode(node, framed: bytes):
        reader = asyncio.StreamReader()
        reader.feed_data(framed)
        reader.feed_eof()
        return await decode_frame(reader, crypto=node._crypto, signer=node._signer)  # noqa: SLF001

    def test_captured_frame_is_refused_on_retransmission(self, tmp_path, pro_license_key):
        from lummenna.p2p.beam import BeamNode, encode_frame

        node = BeamNode(_pro_config(tmp_path, pro_license_key))
        packet = {"room": "shared", "ttl": 24, "chunks": [{"content": "hello"}]}
        captured = encode_frame(packet, crypto=node._crypto, signer=node._signer)

        assert asyncio.run(self._decode(node, captured)) == packet
        with pytest.raises(ValueError, match="Replay detected"):
            asyncio.run(self._decode(node, captured))
        node._persistent_nonce_store.close()  # noqa: SLF001

    def test_captured_frame_is_refused_by_a_restarted_listener(self, tmp_path, pro_license_key):
        """The gap this feature closes: capture, wait for a restart, retransmit."""
        from lummenna.p2p.beam import BeamNode, encode_frame

        first = BeamNode(_pro_config(tmp_path, pro_license_key))
        packet = {"room": "shared", "ttl": 24, "chunks": [{"content": "hello"}]}
        captured = encode_frame(packet, crypto=first._crypto, signer=first._signer)  # noqa: SLF001
        assert asyncio.run(self._decode(first, captured)) == packet
        first._persistent_nonce_store.close()  # noqa: SLF001

        second = BeamNode(_pro_config(tmp_path, pro_license_key))
        with pytest.raises(ValueError, match="Replay detected"):
            asyncio.run(self._decode(second, captured))
        second._persistent_nonce_store.close()  # noqa: SLF001

    def test_handle_peer_drops_a_replayed_frame_without_storing_it(self, tmp_path, pro_license_key):
        """A replayed share must not reach the memory store on the second pass."""
        from lummenna.p2p.beam import BeamNode, encode_frame

        cfg = _pro_config(tmp_path, pro_license_key)
        node = BeamNode(cfg)
        packet = {"room": "shared", "ttl": 24, "chunks": [{"content": "beamed memory"}]}
        captured = encode_frame(packet, crypto=node._crypto, signer=node._signer)  # noqa: SLF001

        async def deliver() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(captured)
            reader.feed_eof()
            await node._handle_peer(reader, _NullWriter())  # noqa: SLF001

        asyncio.run(deliver())
        asyncio.run(deliver())

        conn = get_connection(cfg)
        try:
            stored = conn.execute(
                "SELECT COUNT(*) FROM chunk WHERE content = ?", ("beamed memory",)
            ).fetchone()[0]
        finally:
            conn.close()
        node._persistent_nonce_store.close()  # noqa: SLF001
        assert stored == 1, f"a replayed frame was stored {stored} times"

    def test_handle_peer_fails_closed_when_the_store_is_unavailable(
        self, tmp_path, pro_license_key, caplog
    ):
        """A store outage must drop the frame and say so, not accept silently."""
        from lummenna.p2p.beam import BeamNode, encode_frame

        cfg = _pro_config(tmp_path, pro_license_key)
        node = BeamNode(cfg)
        packet = {"room": "shared", "ttl": 24, "chunks": [{"content": "unpoliced"}]}
        framed = encode_frame(packet, crypto=node._crypto, signer=node._signer)  # noqa: SLF001
        node._persistent_nonce_store.close()  # noqa: SLF001

        async def deliver() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(framed)
            reader.feed_eof()
            await node._handle_peer(reader, _NullWriter())  # noqa: SLF001

        with caplog.at_level(logging.ERROR, logger="lummenna.p2p.beam"):
            asyncio.run(deliver())

        conn = get_connection(cfg)
        try:
            stored = conn.execute(
                "SELECT COUNT(*) FROM chunk WHERE content = ?", ("unpoliced",)
            ).fetchone()[0]
        finally:
            conn.close()
        assert stored == 0, "a frame was stored while replay checking was unavailable"
        assert any(
            "beam_replay_store_unavailable" in record.getMessage() for record in caplog.records
        ), "a replay-store outage was not surfaced at ERROR"


class _NullWriter:
    """Minimal asyncio.StreamWriter stand-in for the handler's teardown path."""

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _FakeZeroconf:
    """mDNS stand-in so start() can be driven without touching the network."""

    def __init__(self) -> None:
        self.zeroconf = object()

    async def async_register_service(self, _info: object) -> None:
        return None


def _run_in_subprocess(script: str, *args: str) -> str:
    """Run *script* in a fresh interpreter and return its last stdout line.

    Fixed argv, no shell: the only variable parts are paths this test created.
    """
    proc = subprocess.run(  # nosec B603 — fixed argv, no shell
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, f"subprocess failed: {proc.stderr}"
    return proc.stdout.strip().splitlines()[-1]
