"""Migration 0004 — make room/locus uniqueness tenant-scoped.

Fixes a multi-tenant isolation defect. Migration 0002 added a ``tenant_id``
column to ``room`` and ``locus`` but left the original uniqueness constraints in
place:

* ``room.name`` carried a **global** ``UNIQUE`` (``sqlite_autoindex_room_1``),
  so two different tenants could not both use a common room name (``"notes"``,
  ``"preferences"``): the second tenant's insert raised
  ``IntegrityError: UNIQUE constraint failed: room.name``.
* ``locus`` was ``UNIQUE(room_id, name)`` — safe only incidentally (loci hang off
  a per-tenant room); this migration makes it explicitly tenant-scoped.

SQLite cannot drop a column-level ``UNIQUE`` in place, so the tables are rebuilt
using the official 12-step ALTER-TABLE procedure
(https://sqlite.org/lang_altertable.html#otheralter): create the corrected
table, copy rows preserving primary keys (so existing foreign-key references from
``chunk``/``locus`` stay valid), drop the old table, rename, then recreate
indexes. Foreign keys are disabled for the duration and re-verified afterwards.

The rebuild is idempotent-by-construction: it always produces the same final
schema regardless of whether the database started fresh (schema.sql → 0002 →
0003 → 0004) or was already populated, and it is a no-op-shaped rebuild when the
constraints are already correct (the copy simply round-trips the rows).
"""

from __future__ import annotations

import sqlite3


def _unique_index_columns(conn: sqlite3.Connection, table: str) -> list[list[str]]:
    """Return the column tuples of every UNIQUE index on ``table``.

    Used to detect whether the rebuild is still required (fail-safe: if the
    expected tenant-scoped uniqueness is already present, we still rebuild to a
    known-good shape, but this lets the migration reason about current state).
    """
    result: list[list[str]] = []
    for _seq, name, unique, _origin, _partial in conn.execute(
        f"PRAGMA index_list({table})"
    ).fetchall():
        if not unique:
            continue
        cols = [row[2] for row in conn.execute(f"PRAGMA index_info({name})").fetchall()]
        result.append(cols)
    return result


def run(conn: sqlite3.Connection) -> None:
    # Step 1 (of the official procedure): foreign keys OFF for the rebuild so the
    # drop/rename of a referenced table does not cascade-delete dependent rows in
    # chunk/locus. PRAGMA foreign_keys is a no-op inside a transaction, so this
    # migration manages its own transaction boundaries explicitly.
    conn.execute("PRAGMA foreign_keys = OFF")

    # The migrate() runner does not open a transaction for us; do it here so the
    # whole rebuild is atomic and rolls back cleanly on any failure.
    conn.execute("BEGIN")
    try:
        # --- Rebuild `room`: global UNIQUE(name) -> UNIQUE(name, tenant_id) -----
        # Recreate with the exact column set produced by schema.sql + 0002 (the
        # tenant_id column was appended by 0002), swapping the column-level UNIQUE
        # on `name` for a tenant-scoped table-level UNIQUE. Column order and
        # defaults are preserved so downstream `SELECT *` / positional access is
        # unaffected.
        conn.execute("""
            CREATE TABLE room_new (
                room_id     INTEGER PRIMARY KEY,
                name        TEXT NOT NULL,
                created_at  INTEGER DEFAULT (unixepoch()),
                last_entry_at INTEGER,
                locus_count INTEGER DEFAULT 0,
                room_type   TEXT CHECK(room_type IN ('domain','project','person','ephemeral')),
                topological_order REAL DEFAULT 0.0,
                tenant_id   TEXT DEFAULT 'default',
                UNIQUE(name, tenant_id)
            )
        """)
        conn.execute("""
            INSERT INTO room_new
                (room_id, name, created_at, last_entry_at, locus_count,
                 room_type, topological_order, tenant_id)
            SELECT room_id, name, created_at, last_entry_at, locus_count,
                   room_type, topological_order, tenant_id
            FROM room
        """)
        conn.execute("DROP TABLE room")
        conn.execute("ALTER TABLE room_new RENAME TO room")
        # Recreate the non-unique tenant lookup index that 0002 created on room.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_room_tenant ON room(tenant_id, name)")

        # --- Rebuild `locus`: UNIQUE(room_id, name) -> UNIQUE(room_id, name, tenant_id)
        conn.execute("""
            CREATE TABLE locus_new (
                locus_id    INTEGER PRIMARY KEY,
                room_id     INTEGER NOT NULL REFERENCES room(room_id) ON DELETE CASCADE,
                name        TEXT NOT NULL,
                description TEXT,
                vector_mean BLOB,
                created_at  INTEGER DEFAULT (unixepoch()),
                access_count INTEGER DEFAULT 0,
                last_access_at INTEGER,
                tenant_id   TEXT DEFAULT 'default',
                UNIQUE(room_id, name, tenant_id)
            )
        """)
        conn.execute("""
            INSERT INTO locus_new
                (locus_id, room_id, name, description, vector_mean,
                 created_at, access_count, last_access_at, tenant_id)
            SELECT locus_id, room_id, name, description, vector_mean,
                   created_at, access_count, last_access_at, tenant_id
            FROM locus
        """)
        conn.execute("DROP TABLE locus")
        conn.execute("ALTER TABLE locus_new RENAME TO locus")

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys = ON")
        raise

    # Step 12: re-validate referential integrity, then re-enable enforcement.
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        conn.execute("PRAGMA foreign_keys = ON")
        raise sqlite3.IntegrityError(f"migration 0004 left dangling foreign keys: {violations!r}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
