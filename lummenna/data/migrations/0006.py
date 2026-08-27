"""Migration 0006 — durable replay-nonce table for the Beam P2P transport.

Beam verifies each frame against a signing window *and* a set of already-seen
nonces. That set used to live only in process memory, so every restart emptied
it: a frame captured shortly before a restart could be replayed once afterwards,
for whatever remained of its window, and the transport silently fell back to
timestamp-only protection during that gap.

This migration adds the table that closes the gap by keeping the nonce set in the
Lummenna store, where it survives restarts and is shared by every process opening
the same database. Rows are short-lived by design: each one is retained only
until the frame that produced it can no longer pass the timestamp check, so the
table is a bounded cache and not a growing log.

The DDL is imported from ``lummenna.p2p.replay`` rather than restated here so
that the migrated schema and the schema a store self-checks on open cannot drift
apart. ``lummenna.p2p.replay`` defers its own database import, so importing it
during a migration does not re-enter the storage layer.
"""

from __future__ import annotations

import sqlite3

from lummenna.p2p.replay import create_nonce_table


def run(conn: sqlite3.Connection) -> None:
    create_nonce_table(conn)
    conn.commit()
