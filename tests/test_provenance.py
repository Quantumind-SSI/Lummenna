import sqlite3
import time

import pytest

from lummenna.data.schema import init_db
from lummenna.force.mnemonic.provenance import (
    create_provenance,
    get_effective_fact,
    supersede_chunk,
)


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    _setup_test_data(conn)
    yield conn
    conn.close()


def _setup_test_data(conn):
    conn.execute("INSERT INTO room(room_id, name, room_type) VALUES (1, 'prov_room', 'domain')")
    conn.execute("INSERT INTO locus(locus_id, room_id, name) VALUES (1, 1, 'prov_locus')")
    conn.commit()


def test_create_provenance_returns_int(db_conn):
    chunk_id = 1
    db_conn.execute(
        "INSERT INTO chunk(chunk_id, room_id, locus_id, content, content_hash) "
        "VALUES (?, 1, 1, 'test', 'hash1')",
        (chunk_id,),
    )
    db_conn.commit()
    prov_id = create_provenance(db_conn, chunk_id, "user_input", source_ref="test/1")
    assert isinstance(prov_id, int)
    assert prov_id > 0


def test_create_provenance_stores_record(db_conn):
    chunk_id = 2
    db_conn.execute(
        "INSERT INTO chunk(chunk_id, room_id, locus_id, content, content_hash) "
        "VALUES (?, 1, 1, 'test2', 'hash2')",
        (chunk_id,),
    )
    db_conn.commit()
    prov_id = create_provenance(
        db_conn,
        chunk_id,
        "agent_reasoning",
        source_ref="ag/1",
        confidence=0.8,
        extraction_method="rule",
    )
    row = db_conn.execute("SELECT * FROM provenance WHERE provenance_id = ?", (prov_id,)).fetchone()
    assert row["chunk_id"] == chunk_id
    assert row["source_type"] == "agent_reasoning"
    assert row["confidence"] == 0.8


def test_supersede_chunk_sets_valid_to(db_conn):
    int(time.time())
    db_conn.execute(
        "INSERT INTO chunk(chunk_id, room_id, locus_id, content, content_hash, valid_to) "
        "VALUES (10, 1, 1, 'old', 'h10', NULL)"
    )
    db_conn.execute(
        "INSERT INTO chunk(chunk_id, room_id, locus_id, content, content_hash, valid_to) "
        "VALUES (11, 1, 1, 'new', 'h11', NULL)"
    )
    db_conn.commit()
    supersede_chunk(db_conn, old_chunk_id=10, new_chunk_id=11)
    row = db_conn.execute(
        "SELECT valid_to, superseded_by FROM chunk WHERE chunk_id = ?", (10,)
    ).fetchone()
    assert row["valid_to"] is not None
    assert row["superseded_by"] == 11


def _insert_chunk(conn, chunk_id: int, content: str, content_hash: str) -> None:
    conn.execute(
        "INSERT INTO chunk(chunk_id, room_id, locus_id, content, content_hash, valid_to) "
        "VALUES (?, 1, 1, ?, ?, NULL)",
        (chunk_id, content, content_hash),
    )


class TestSupersedeCompareAndSwap:
    """Superseding is a CAS, so concurrent writers cannot lose each other's work."""

    def test_returns_true_when_it_supersedes(self, db_conn):
        _insert_chunk(db_conn, 40, "old", "h40")
        _insert_chunk(db_conn, 41, "new", "h41")
        db_conn.commit()
        assert supersede_chunk(db_conn, old_chunk_id=40, new_chunk_id=41) is True

    def test_second_supersede_is_refused(self, db_conn):
        """The losing writer must not overwrite the winner's successor link."""
        _insert_chunk(db_conn, 50, "old", "h50")
        _insert_chunk(db_conn, 51, "winner", "h51")
        _insert_chunk(db_conn, 52, "loser", "h52")
        db_conn.commit()

        assert supersede_chunk(db_conn, old_chunk_id=50, new_chunk_id=51) is True
        assert supersede_chunk(db_conn, old_chunk_id=50, new_chunk_id=52) is False

        row = db_conn.execute(
            "SELECT superseded_by FROM chunk WHERE chunk_id = ?", (50,)
        ).fetchone()
        assert row["superseded_by"] == 51, "the second writer clobbered the first"

    def test_valid_to_is_not_bumped_by_a_refused_supersede(self, db_conn):
        _insert_chunk(db_conn, 60, "old", "h60")
        _insert_chunk(db_conn, 61, "winner", "h61")
        _insert_chunk(db_conn, 62, "loser", "h62")
        db_conn.commit()

        supersede_chunk(db_conn, old_chunk_id=60, new_chunk_id=61)
        first_valid_to = db_conn.execute(
            "SELECT valid_to FROM chunk WHERE chunk_id = ?", (60,)
        ).fetchone()["valid_to"]

        supersede_chunk(db_conn, old_chunk_id=60, new_chunk_id=62)
        second_valid_to = db_conn.execute(
            "SELECT valid_to FROM chunk WHERE chunk_id = ?", (60,)
        ).fetchone()["valid_to"]

        assert second_valid_to == first_valid_to

    def test_missing_chunk_reports_failure_instead_of_silent_success(self, db_conn):
        _insert_chunk(db_conn, 70, "new", "h70")
        db_conn.commit()
        assert supersede_chunk(db_conn, old_chunk_id=999_999, new_chunk_id=70) is False

    def test_self_supersede_is_rejected(self, db_conn):
        """A cyclic chain would make get_effective_fact non-terminating."""
        _insert_chunk(db_conn, 80, "self", "h80")
        db_conn.commit()
        with pytest.raises(ValueError, match="cannot supersede itself"):
            supersede_chunk(db_conn, old_chunk_id=80, new_chunk_id=80)

    def test_effective_fact_still_resolves_to_the_winner(self, db_conn):
        """The CAS must not break the lineage it exists to protect."""
        _insert_chunk(db_conn, 90, "old", "shared")
        _insert_chunk(db_conn, 91, "winner", "shared")
        _insert_chunk(db_conn, 92, "loser", "other")
        db_conn.commit()
        create_provenance(db_conn, 90, "user_input")
        create_provenance(db_conn, 91, "user_input")

        supersede_chunk(db_conn, old_chunk_id=90, new_chunk_id=91)
        supersede_chunk(db_conn, old_chunk_id=90, new_chunk_id=92)

        result = get_effective_fact(db_conn, "shared")
        assert result is not None
        assert result["chunk_id"] == 91


def test_get_effective_fact_returns_none_for_missing(db_conn):
    result = get_effective_fact(db_conn, "nonexistent_hash")
    assert result is None


def test_get_effective_fact_returns_valid_chunk(db_conn):
    db_conn.execute(
        "INSERT INTO chunk(chunk_id, room_id, locus_id, content, content_hash, valid_from, valid_to) "
        "VALUES (20, 1, 1, 'valid content', 'ab12', unixepoch(), NULL)"
    )
    db_conn.commit()
    create_provenance(db_conn, 20, "user_input")
    result = get_effective_fact(db_conn, "ab12")
    assert result is not None
    assert result["chunk_id"] == 20


def test_get_effective_fact_excludes_superseded_chunk(db_conn):
    db_conn.execute(
        "INSERT INTO chunk(chunk_id, room_id, locus_id, content, content_hash, valid_from, valid_to) "
        "VALUES (30, 1, 1, 'old', 'cd34', unixepoch(), NULL)"
    )
    db_conn.execute(
        "INSERT INTO chunk(chunk_id, room_id, locus_id, content, content_hash, valid_from, valid_to) "
        "VALUES (31, 1, 1, 'new', 'cd34', unixepoch(), NULL)"
    )
    db_conn.commit()
    create_provenance(db_conn, 30, "user_input")
    create_provenance(db_conn, 31, "user_input")
    supersede_chunk(db_conn, old_chunk_id=30, new_chunk_id=31)
    result = get_effective_fact(db_conn, "cd34")
    assert result is not None
    assert result["chunk_id"] == 31
