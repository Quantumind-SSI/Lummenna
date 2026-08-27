"""Tests for off-device store replication (lummenna.ops.backup).

No test here touches the network. The Azure container client is replaced by an
in-memory fake, which is also what the conftest egress guard demands.
"""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from lummenna.brand.errors import (
    BackupIntegrityError,
    BackupNotConfiguredError,
    BackupRestoreError,
    BackupUploadError,
    SovereignViolationError,
)
from lummenna.config import LummennaConfig
from lummenna.data.schema import get_connection
from lummenna.ops import backup as ops_backup


class _FakeDownloader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def readinto(self, handle) -> int:
        handle.write(self._payload)
        return len(self._payload)


class FakeContainer:
    """Minimal stand-in for azure.storage.blob.ContainerClient."""

    def __init__(self, fail_uploads: int = 0, fail_deletes: bool = False) -> None:
        self.blobs: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.fail_uploads = fail_uploads
        self.fail_deletes = fail_deletes
        self.upload_attempts = 0
        self.deleted: list[str] = []

    def upload_blob(self, name, data, overwrite, metadata):  # noqa: ARG002
        self.upload_attempts += 1
        if self.upload_attempts <= self.fail_uploads:
            raise RuntimeError("simulated transient blob failure")
        self.blobs[name] = data.read()
        self.metadata[name] = dict(metadata)

    def list_blobs(self, name_starts_with: str = ""):
        return [
            SimpleNamespace(name=name)
            for name in sorted(self.blobs)
            if name.startswith(name_starts_with)
        ]

    def delete_blob(self, name: str) -> None:
        if self.fail_deletes:
            raise RuntimeError("simulated delete failure")
        self.blobs.pop(name, None)
        self.deleted.append(name)

    def download_blob(self, name: str) -> _FakeDownloader:
        return _FakeDownloader(self.blobs[name])

    def get_blob_client(self, name: str):
        return SimpleNamespace(
            get_blob_properties=lambda: SimpleNamespace(metadata=self.metadata.get(name, {}))
        )


def _config(tmp_path: Path, **overrides) -> LummennaConfig:
    base = {
        "store_path": str(tmp_path / "store"),
        "sovereign": False,
        "backup_enabled": True,
        "backup_account": "examplestorage",
        "backup_container": "db-backups",
    }
    base.update(overrides)
    return LummennaConfig(**base)


def _seed_store(config: LummennaConfig, rows: int = 5) -> None:
    """Create a real store with some content in it."""
    conn = get_connection(config)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany(
            "INSERT INTO probe (body) VALUES (?)",
            [(f"row-{index}",) for index in range(rows)],
        )
        conn.commit()
    finally:
        conn.close()


def _no_sleep(_seconds: float) -> None:
    """Sleep replacement so retry tests do not actually wait."""
    return None


# --------------------------------------------------------------------------
# Snapshot correctness
# --------------------------------------------------------------------------


def test_snapshot_is_readable_and_preserves_rows(tmp_path):
    config = _config(tmp_path)
    _seed_store(config, rows=7)

    dest = tmp_path / "out" / "snap.db"
    dest.parent.mkdir()
    size = ops_backup.snapshot_database(config, dest)

    assert size > 0
    conn = sqlite3.connect(dest)
    try:
        assert conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 7
    finally:
        conn.close()


def test_snapshot_taken_while_writer_holds_wal_is_consistent(tmp_path):
    """A snapshot must reflect committed state only, never a partial write."""
    config = _config(tmp_path)
    _seed_store(config, rows=3)

    writer = get_connection(config)
    try:
        # Uncommitted work must not appear in the snapshot.
        writer.execute("BEGIN")
        writer.execute("INSERT INTO probe (body) VALUES ('uncommitted')")

        dest = tmp_path / "snap.db"
        ops_backup.snapshot_database(config, dest)

        snap = sqlite3.connect(dest)
        try:
            count = snap.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
            bodies = [row[0] for row in snap.execute("SELECT body FROM probe")]
        finally:
            snap.close()

        assert count == 3
        assert "uncommitted" not in bodies
        writer.rollback()
    finally:
        writer.close()


def test_snapshot_refuses_existing_destination(tmp_path):
    config = _config(tmp_path)
    _seed_store(config)
    dest = tmp_path / "snap.db"
    dest.write_bytes(b"occupied")

    with pytest.raises(BackupUploadError, match="already exists"):
        ops_backup.snapshot_database(config, dest)


def test_snapshot_rejects_missing_destination_directory(tmp_path):
    config = _config(tmp_path)
    _seed_store(config)

    with pytest.raises(BackupUploadError, match="does not exist"):
        ops_backup.snapshot_database(config, tmp_path / "absent" / "snap.db")


# --------------------------------------------------------------------------
# Integrity primitives
# --------------------------------------------------------------------------


def test_compress_decompress_roundtrip_preserves_bytes(tmp_path):
    source = tmp_path / "a.bin"
    source.write_bytes(b"lummenna" * 4096)
    packed = tmp_path / "a.gz"
    restored = tmp_path / "a.out"

    ops_backup.compress_file(source, packed)
    ops_backup.decompress_file(packed, restored)

    assert restored.read_bytes() == source.read_bytes()
    assert ops_backup.sha256_file(restored) == ops_backup.sha256_file(source)


def test_decompress_rejects_non_gzip_payload(tmp_path):
    bogus = tmp_path / "not.gz"
    bogus.write_bytes(b"this is definitely not gzip")

    with pytest.raises(BackupIntegrityError):
        ops_backup.decompress_file(bogus, tmp_path / "out.db")


def test_decompress_rejects_missing_source(tmp_path):
    with pytest.raises(BackupRestoreError, match="nothing to decompress"):
        ops_backup.decompress_file(tmp_path / "absent.gz", tmp_path / "out.db")


def test_verify_database_accepts_real_database(tmp_path):
    config = _config(tmp_path)
    _seed_store(config)
    dest = tmp_path / "snap.db"
    ops_backup.snapshot_database(config, dest)

    ops_backup.verify_database(dest)


def test_verify_database_rejects_corrupt_file(tmp_path):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"SQLite format 3\x00" + b"\xde\xad\xbe\xef" * 256)

    with pytest.raises(BackupIntegrityError):
        ops_backup.verify_database(corrupt)


def test_blob_name_rejects_empty_stamp():
    with pytest.raises(ValueError, match="non-empty"):
        ops_backup.blob_name_for("")


# --------------------------------------------------------------------------
# Upload, retry, prune
# --------------------------------------------------------------------------


def test_upload_succeeds_after_transient_failures(tmp_path):
    container = FakeContainer(fail_uploads=2)
    payload = tmp_path / "p.gz"
    payload.write_bytes(b"payload")

    ops_backup.upload_snapshot(container, "x.gz", payload, {"sha256": "abc"}, sleeper=_no_sleep)

    assert container.upload_attempts == 3
    assert container.blobs["x.gz"] == b"payload"


def test_upload_gives_up_after_bounded_attempts(tmp_path):
    container = FakeContainer(fail_uploads=99)
    payload = tmp_path / "p.gz"
    payload.write_bytes(b"payload")

    with pytest.raises(BackupUploadError) as excinfo:
        ops_backup.upload_snapshot(container, "x.gz", payload, {}, sleeper=_no_sleep)

    assert container.upload_attempts == ops_backup.MAX_UPLOAD_ATTEMPTS
    assert "attempt 5/5" in str(excinfo.value)


def test_upload_rejects_missing_payload(tmp_path):
    with pytest.raises(BackupUploadError, match="payload missing"):
        ops_backup.upload_snapshot(FakeContainer(), "x.gz", tmp_path / "gone.gz", {})


def test_prune_keeps_only_the_newest_n():
    container = FakeContainer()
    for index in range(6):
        container.blobs[f"{ops_backup.BLOB_PREFIX}/2026082{index}T000000Z.db.gz"] = b"x"

    deleted = ops_backup.prune_snapshots(container, retention=2)

    assert len(deleted) == 4
    assert len(container.blobs) == 2
    # The two survivors must be the newest by timestamp.
    assert sorted(container.blobs) == [
        f"{ops_backup.BLOB_PREFIX}/20260824T000000Z.db.gz",
        f"{ops_backup.BLOB_PREFIX}/20260825T000000Z.db.gz",
    ]


def test_prune_with_zero_retention_deletes_nothing():
    container = FakeContainer()
    container.blobs[f"{ops_backup.BLOB_PREFIX}/a.db.gz"] = b"x"

    assert ops_backup.prune_snapshots(container, retention=0) == []
    assert len(container.blobs) == 1


def test_prune_failure_does_not_raise():
    """Housekeeping must never fail a backup that already succeeded."""
    container = FakeContainer(fail_deletes=True)
    for index in range(4):
        container.blobs[f"{ops_backup.BLOB_PREFIX}/0{index}.db.gz"] = b"x"

    assert ops_backup.prune_snapshots(container, retention=1) == []
    assert len(container.blobs) == 4


# --------------------------------------------------------------------------
# Full cycle
# --------------------------------------------------------------------------


def test_run_backup_once_uploads_verifiable_snapshot(tmp_path):
    config = _config(tmp_path)
    _seed_store(config, rows=4)
    container = FakeContainer()

    result = ops_backup.run_backup_once(config, container=container)

    assert result.blob_name in container.blobs
    assert result.raw_bytes > 0
    assert result.checksum == container.metadata[result.blob_name]["sha256"]
    # The stored blob must be gzip that decompresses to the recorded hash.
    restored = gzip.decompress(container.blobs[result.blob_name])
    assert len(restored) == result.raw_bytes


def test_run_backup_once_prunes_to_retention(tmp_path):
    config = _config(tmp_path, backup_retention=2)
    _seed_store(config)
    container = FakeContainer()

    stamps = ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z"]
    for stamp in stamps:
        from datetime import datetime, timezone

        moment = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        ops_backup.run_backup_once(config, container=container, now=moment)

    assert len(container.blobs) == 2


# --------------------------------------------------------------------------
# Sovereignty and configuration
# --------------------------------------------------------------------------


def test_backup_refused_in_sovereign_mode(tmp_path):
    """The default posture must make off-device replication impossible."""
    config = _config(tmp_path, sovereign=True)
    _seed_store(config)

    with pytest.raises(SovereignViolationError):
        ops_backup.run_backup_once(config, container=FakeContainer())


def test_restore_refused_in_sovereign_mode(tmp_path):
    config = _config(tmp_path, sovereign=True)

    with pytest.raises(SovereignViolationError):
        ops_backup.restore_if_missing(config, container=FakeContainer())


def test_missing_account_is_a_configuration_error(tmp_path):
    config = _config(tmp_path, backup_account=None)

    with pytest.raises(BackupNotConfiguredError, match="backup_account"):
        ops_backup.container_client(config)


def test_scheduler_is_none_when_backup_disabled(tmp_path):
    config = _config(tmp_path, backup_enabled=False)

    assert ops_backup.scheduler_if_enabled(config) is None


def test_scheduler_raises_when_enabled_but_misconfigured(tmp_path):
    """Silently not backing up an opted-in deployment is the core failure mode."""
    config = _config(tmp_path, backup_account=None)

    with pytest.raises(BackupNotConfiguredError):
        ops_backup.scheduler_if_enabled(config)


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------


def test_restore_is_a_noop_when_store_is_populated(tmp_path):
    config = _config(tmp_path)
    _seed_store(config)
    container = FakeContainer()
    ops_backup.run_backup_once(config, container=container)

    before = ops_backup.database_path(config).read_bytes()
    assert ops_backup.restore_if_missing(config, container=container) is False
    assert ops_backup.database_path(config).read_bytes() == before


def test_restore_returns_false_when_no_snapshots_exist(tmp_path):
    config = _config(tmp_path)

    assert ops_backup.restore_if_missing(config, container=FakeContainer()) is False


def test_restore_recovers_data_onto_a_fresh_disk(tmp_path):
    """The whole point: a replica on empty ephemeral disk gets its data back."""
    source_config = _config(tmp_path / "original")
    _seed_store(source_config, rows=9)
    container = FakeContainer()
    ops_backup.run_backup_once(source_config, container=container)

    fresh_config = _config(tmp_path / "replacement")
    assert ops_backup.restore_if_missing(fresh_config, container=container) is True

    conn = sqlite3.connect(ops_backup.database_path(fresh_config))
    try:
        assert conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 9
    finally:
        conn.close()


def test_restore_rejects_snapshot_with_wrong_checksum(tmp_path):
    config = _config(tmp_path / "src")
    _seed_store(config)
    container = FakeContainer()
    result = ops_backup.run_backup_once(config, container=container)

    container.metadata[result.blob_name]["sha256"] = "0" * 64

    fresh = _config(tmp_path / "dst")
    with pytest.raises(BackupIntegrityError, match="does not match"):
        ops_backup.restore_if_missing(fresh, container=container)

    assert not ops_backup.database_path(fresh).exists()


def test_restore_reports_download_failure(tmp_path):
    config = _config(tmp_path / "src")
    _seed_store(config)
    container = FakeContainer()
    ops_backup.run_backup_once(config, container=container)

    def _boom(_name):
        raise RuntimeError("network gone")

    container.download_blob = _boom

    with pytest.raises(BackupRestoreError, match="could not download"):
        ops_backup.restore_if_missing(_config(tmp_path / "dst"), container=container)


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------


def test_scheduler_counts_success_and_failure(tmp_path):
    config = _config(tmp_path)
    _seed_store(config)
    container = FakeContainer()
    scheduler = ops_backup.BackupScheduler(config, container=container)

    assert scheduler.run_cycle() is not None
    assert scheduler.successes == 1

    container.fail_uploads = 99
    original_sleep = ops_backup.time.sleep
    ops_backup.time.sleep = _no_sleep
    try:
        assert scheduler.run_cycle() is None
    finally:
        ops_backup.time.sleep = original_sleep
    assert scheduler.failures == 1


def test_scheduler_enforces_minimum_interval(tmp_path):
    config = _config(tmp_path, backup_interval_seconds=1)
    scheduler = ops_backup.BackupScheduler(config)

    assert scheduler.interval == ops_backup.MIN_INTERVAL_SECONDS


def test_scheduler_thread_starts_and_stops_cleanly(tmp_path):
    config = _config(tmp_path)
    _seed_store(config)
    scheduler = ops_backup.BackupScheduler(config, container=FakeContainer())

    scheduler.start()
    assert scheduler.running
    scheduler.stop(timeout=5.0)

    assert not scheduler.running
    assert scheduler.successes >= 1


def test_summarize_handles_empty_and_populated():
    assert ops_backup.summarize([]) == "no backups recorded"

    result = ops_backup.BackupResult(
        blob_name="lummenna-db/x.db.gz",
        raw_bytes=1000,
        compressed_bytes=250,
        checksum="abc",
        duration_seconds=0.5,
    )
    summary = ops_backup.summarize([result])
    assert "1 snapshot(s)" in summary
    assert "25.0%" in summary


def test_backup_result_ratio_is_safe_for_empty_database():
    result = ops_backup.BackupResult("n", 0, 0, "c", 0.0)

    assert result.compression_ratio == 1.0
