"""Migration 0005 — tamper-evident hash chain for audit_log.

The compliance (P1) Pro feature advertises "audit trails". Previously the
``audit_log`` table was a plain, freely-mutable table: any operator with database
access could delete or rewrite rows and no reader could tell. This migration adds
the two columns that make the log tamper-evident:

* ``prev_hash``  — the ``entry_hash`` of the immediately preceding audit row
  (a fixed genesis sentinel for the first row).
* ``entry_hash`` — SHA-256 over a canonical serialisation of this row's content
  fields plus ``prev_hash``.

Because each row commits to the hash of the previous one, deleting a row,
reordering rows, or altering any hashed field breaks the chain at that point and
is detected by ``lummenna.audit.verify_audit_chain``. See ``lummenna/audit.py`` for
the canonical hashing used to populate these columns on insert.

Existing rows (written before this migration) are back-filled into a valid chain
so verification starts clean; their hashes are computed from the data actually
present. This does not retroactively prove the pre-migration rows were
untampered — it only establishes the chain from which all *future* tampering is
detectable.
"""

from __future__ import annotations

import sqlite3

from lummenna.audit import GENESIS_PREV_HASH, compute_entry_hash


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def run(conn: sqlite3.Connection) -> None:
    cols = _columns(conn, "audit_log")
    if "prev_hash" not in cols:
        conn.execute("ALTER TABLE audit_log ADD COLUMN prev_hash TEXT")
    if "entry_hash" not in cols:
        conn.execute("ALTER TABLE audit_log ADD COLUMN entry_hash TEXT")

    # Back-fill any rows lacking an entry_hash into a valid chain, in insertion
    # order (audit_id is a monotonic INTEGER PRIMARY KEY, so it is the true
    # append order even if created_at timestamps collide at 1-second resolution).
    rows = conn.execute(
        """SELECT audit_id, event_type, actor, resource_type, resource_id,
                  action, metadata_json, client_ip, request_id, created_at
           FROM audit_log
           WHERE entry_hash IS NULL
           ORDER BY audit_id ASC"""
    ).fetchall()

    # Chain continues from the last already-hashed row, if any.
    last = conn.execute(
        "SELECT entry_hash FROM audit_log "
        "WHERE entry_hash IS NOT NULL ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()
    prev_hash = last[0] if last and last[0] else GENESIS_PREV_HASH

    for row in rows:
        (
            audit_id,
            event_type,
            actor,
            resource_type,
            resource_id,
            action,
            metadata_json,
            client_ip,
            request_id,
            created_at,
        ) = row
        entry_hash = compute_entry_hash(
            prev_hash=prev_hash,
            event_type=event_type,
            actor=actor,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            metadata_json=metadata_json,
            client_ip=client_ip,
            request_id=request_id,
            created_at=created_at,
        )
        conn.execute(
            "UPDATE audit_log SET prev_hash = ?, entry_hash = ? WHERE audit_id = ?",
            (prev_hash, entry_hash, audit_id),
        )
        prev_hash = entry_hash

    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_entry_hash ON audit_log(entry_hash)")
    conn.commit()
