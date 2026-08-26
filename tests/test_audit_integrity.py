"""Tests for tamper-evident audit-log integrity (hash chain) and RTBF atomicity.

Covers:
  * The audit hash chain builds and verifies for a clean sequence of events.
  * Altering a row's content is detected (entry_hash mismatch).
  * Deleting a row is detected (prev_hash break).
  * Migration 0005 back-fills legacy rows into a valid chain and appends continue it.
  * safety_forget_chunk records its audit event atomically, clears content_hash,
    and leaves the chain intact.
"""

from __future__ import annotations

import base64
import json

import pytest

from lummenna import licensing
from lummenna.audit import (
    GENESIS_PREV_HASH,
    compute_entry_hash,
    log_audit_event,
    verify_audit_chain,
)
from lummenna.config import LummennaConfig
from lummenna.data.schema import get_connection


def _write(conn, i: int) -> None:
    log_audit_event(
        conn,
        event_type="compliance",
        actor="system",
        resource_type="chunk",
        resource_id=i,
        action="safety_forget",
        metadata_json=json.dumps({"reason": f"r{i}"}),
        client_ip=None,
        request_id=None,
    )


# --------------------------------------------------------------------------- #
# Chain build + verification
# --------------------------------------------------------------------------- #


def test_clean_chain_verifies(tmp_path):
    conn = get_connection(LummennaConfig(store_path=str(tmp_path / ".lummenna")))
    try:
        for i in range(5):
            _write(conn, i)
        result = verify_audit_chain(conn)
        assert result.ok
        assert result.rows_checked == 5
        assert result.broken_at is None
    finally:
        conn.close()


def test_first_row_uses_genesis_prev_hash(tmp_path):
    conn = get_connection(LummennaConfig(store_path=str(tmp_path / ".lummenna")))
    try:
        _write(conn, 0)
        prev = conn.execute(
            "SELECT prev_hash FROM audit_log ORDER BY audit_id ASC LIMIT 1"
        ).fetchone()[0]
        assert prev == GENESIS_PREV_HASH
    finally:
        conn.close()


def test_altered_row_is_detected(tmp_path):
    conn = get_connection(LummennaConfig(store_path=str(tmp_path / ".lummenna")))
    try:
        for i in range(4):
            _write(conn, i)
        # Tamper with a hashed field on row 2.
        conn.execute("UPDATE audit_log SET action = 'TAMPERED' WHERE audit_id = 2")
        conn.commit()
        result = verify_audit_chain(conn)
        assert not result.ok
        assert result.broken_at == 2
        assert "altered" in (result.reason or "")
    finally:
        conn.close()


def test_deleted_row_is_detected(tmp_path):
    conn = get_connection(LummennaConfig(store_path=str(tmp_path / ".lummenna")))
    try:
        for i in range(4):
            _write(conn, i)
        conn.execute("DELETE FROM audit_log WHERE audit_id = 2")
        conn.commit()
        result = verify_audit_chain(conn)
        assert not result.ok
        # The break surfaces at the row whose prev_hash no longer matches.
        assert result.broken_at == 3
        assert "deleted or reordered" in (result.reason or "")
    finally:
        conn.close()


def test_compute_entry_hash_is_deterministic():
    kwargs = {
        "prev_hash": GENESIS_PREV_HASH,
        "event_type": "compliance",
        "actor": "system",
        "resource_type": "chunk",
        "resource_id": 7,
        "action": "safety_forget",
        "metadata_json": '{"reason":"x"}',
        "client_ip": None,
        "request_id": None,
        "created_at": 1_700_000_000,
    }
    assert compute_entry_hash(**kwargs) == compute_entry_hash(**kwargs)
    # A changed field yields a different hash.
    changed = {**kwargs, "resource_id": 8}
    assert compute_entry_hash(**changed) != compute_entry_hash(**kwargs)


# --------------------------------------------------------------------------- #
# Migration 0005 back-fill
# --------------------------------------------------------------------------- #


def test_migration_backfills_legacy_rows(tmp_path):
    cfg = LummennaConfig(store_path=str(tmp_path / ".lummenna"))
    conn = get_connection(cfg)
    try:
        # Simulate legacy (pre-0005) rows with no chain, then force a re-migration.
        conn.execute("DELETE FROM audit_log")
        for i in range(3):
            conn.execute(
                "INSERT INTO audit_log(event_type, actor, action, resource_id, created_at)"
                " VALUES('compliance','system','legacy', ?, unixepoch())",
                (i,),
            )
        conn.execute("UPDATE audit_log SET prev_hash = NULL, entry_hash = NULL")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
    finally:
        conn.close()

    conn2 = get_connection(cfg)  # triggers migrate -> 0005 back-fill
    try:
        assert conn2.execute("PRAGMA user_version").fetchone()[0] >= 5
        assert verify_audit_chain(conn2).ok
        # New appends continue the back-filled chain.
        _write(conn2, 99)
        result = verify_audit_chain(conn2)
        assert result.ok
        assert result.rows_checked == 4
    finally:
        conn2.close()


# --------------------------------------------------------------------------- #
# RTBF atomicity + content_hash clearing
# --------------------------------------------------------------------------- #


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@pytest.fixture
def pro_env(monkeypatch):
    """Embed an ephemeral signing key and set LUMMENNA_LICENSE_KEY for the gate.

    safety_forget_chunk constructs a fresh LummennaConfig() for its defense-in-depth
    gate, which reads the key from the environment, so the license must be set
    there rather than only on a passed config.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(licensing, "_QUANTUMINDSSI_PUBLIC_KEY_B64", _b64(pub))
    payload = json.dumps(
        {"sub": "t", "edition": "pro", "features": ["*"], "iss": "QuantumindSSI", "id": "x"},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    key = f"LUMMENNA-{_b64(payload)}.{_b64(priv.sign(payload))}"
    monkeypatch.setenv("LUMMENNA_LICENSE_KEY", key)
    licensing.clear_license_cache()
    yield key
    licensing.clear_license_cache()


def test_safety_forget_is_atomic_and_clears_hash(tmp_path, pro_env):
    from lummenna.compliance.safety_forgetting import safety_forget_chunk
    from lummenna.force.mnemonic.store import store_memory

    cfg = LummennaConfig(store_path=str(tmp_path / ".lummenna"), license_key=pro_env)
    conn = get_connection(cfg)
    try:
        cid = store_memory(conn, "sensitive 123-45-6789", "secrets", config=cfg)
        conn.commit()
        assert conn.execute("SELECT content_hash FROM chunk WHERE chunk_id = ?", (cid,)).fetchone()[
            0
        ]

        safety_forget_chunk(conn, cid, "gdpr erasure")

        content, content_hash, optical = conn.execute(
            "SELECT content, content_hash, optical_level FROM chunk WHERE chunk_id = ?",
            (cid,),
        ).fetchone()
        assert content == "[REDACTED]"
        assert content_hash == ""  # original-PII hash must not survive erasure
        assert optical == 2

        # The erasure recorded exactly one audit event, and the chain is intact
        # (audit + erasure committed atomically).
        n = conn.execute(
            "SELECT count(*) FROM audit_log WHERE action = 'safety_forget'"
        ).fetchone()[0]
        assert n == 1
        assert verify_audit_chain(conn).ok
    finally:
        conn.close()
