"""D19: Off-device replication of the SQLite store.

Input wire: LummennaConfig (backup_* fields), the on-disk SQLite store
Output wire: Azure Blob Storage container, workload-identity authenticated
Secret sauce: ``VACUUM INTO`` reads through the WAL and takes only a read
              lock, so a snapshot is transactionally consistent while the API
              keeps serving writes.

Why this module exists
----------------------
A hosted Lummenna runs its store on ephemeral local disk. That is not a cost
decision: SQLite in WAL mode requires a shared-memory region that no network
filesystem provides (https://sqlite.org/wal.html section 2.2), so Azure Files
is not an option and the disk must therefore be local and disposable. Local
and disposable means the store dies with the replica, so durability has to
come from replicating it somewhere else. That is this module.

Sovereignty
-----------
Every function here that touches the network calls
``LummennaConfig.assert_sovereign()`` first. Sovereign mode is the default, so
a stock install can never replicate its memories anywhere, even if the backup
settings are filled in by accident or by a copied config file. Only a
deployment that has explicitly declared itself non-sovereign can back up.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lummenna.brand.errors import (
    BackupDependencyMissingError,
    BackupIntegrityError,
    BackupNotConfiguredError,
    BackupRestoreError,
    BackupUploadError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lummenna.config import LummennaConfig

_LOG = logging.getLogger(__name__)

#: Blob key prefix. Snapshots sort lexicographically by timestamp under it.
BLOB_PREFIX = "lummenna-db"

#: Blob metadata key holding the SHA-256 of the *uncompressed* snapshot.
CHECKSUM_KEY = "sha256"

#: Blob metadata key holding the uncompressed size in bytes.
SIZE_KEY = "rawbytes"

#: ``VACUUM INTO`` landed in SQLite 3.27.0 (2019-02-07).
_MIN_SQLITE_VERSION = (3, 27, 0)

#: Floor on the snapshot interval. Enforced here rather than in the config so
#: that a hand-edited config.toml cannot schedule a hot loop against blob.
MIN_INTERVAL_SECONDS = 60

#: Bounded retry budget. Power of 10 rule 2: no unbounded retry loops.
MAX_UPLOAD_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_CAP_SECONDS = 30.0

#: Read size for hashing and streaming. 1 MiB.
_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class BackupResult:
    """Outcome of a single successful snapshot-and-upload cycle."""

    blob_name: str
    raw_bytes: int
    compressed_bytes: int
    checksum: str
    duration_seconds: float

    @property
    def compression_ratio(self) -> float:
        """Compressed size as a fraction of raw size; 1.0 when raw is empty."""
        if self.raw_bytes <= 0:
            return 1.0
        return self.compressed_bytes / self.raw_bytes


ConnectFactory = Callable[["LummennaConfig"], sqlite3.Connection]


def _utc_stamp(now: datetime | None = None) -> str:
    """Return a lexicographically sortable UTC timestamp, e.g. 20260824T131502Z."""
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def blob_name_for(stamp: str) -> str:
    """Build the full blob key for a snapshot taken at ``stamp``."""
    if not stamp:
        raise ValueError("stamp must be non-empty")
    return f"{BLOB_PREFIX}/{stamp}.db.gz"


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 of ``path``, read in bounded chunks.

    Complexity: O(n) in file size, O(1) memory.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(_CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _assert_vacuum_into_supported() -> None:
    """Raise BackupError if the linked SQLite is too old for ``VACUUM INTO``."""
    actual = sqlite3.sqlite_version_info
    if tuple(actual[:3]) < _MIN_SQLITE_VERSION:
        wanted = ".".join(str(part) for part in _MIN_SQLITE_VERSION)
        raise BackupUploadError(
            "unavailable",
            f"SQLite {sqlite3.sqlite_version} does not support VACUUM INTO; "
            f"{wanted} or newer is required to take a consistent snapshot",
        )


def database_path(config: LummennaConfig) -> Path:
    """Return the store's database file, matching lummenna.data.schema."""
    return Path(config.store_path) / "lummenna.db"


def _default_connect(config: LummennaConfig) -> sqlite3.Connection:
    """Open the store through the sanctioned accessor.

    Uses lummenna.data.schema.get_connection so that encryption mode, WAL
    verification, and permission hardening all match the main path exactly.
    """
    from lummenna.data.schema import get_connection

    return get_connection(config)


def snapshot_database(
    config: LummennaConfig,
    destination: Path,
    connect: ConnectFactory | None = None,
) -> int:
    """Write a consistent snapshot of the store to ``destination``.

    ``VACUUM INTO`` takes a read lock only, reads through the write-ahead log,
    and writes a defragmented standalone database. Writers are not blocked for
    the duration. The destination must not already exist; SQLite refuses to
    overwrite, and relying on that is safer than unlinking behind its back.

    Args:
        config: Active configuration; supplies store_path and encryption mode.
        destination: Path to create. Its parent must exist.
        connect: Connection factory, for tests. Defaults to the real store.

    Returns:
        Size of the snapshot in bytes.

    Raises:
        BackupUploadError: SQLite is too old, or the vacuum itself failed.

    Complexity: O(n) in database size.
    """
    if not destination.parent.is_dir():
        raise BackupUploadError(
            destination.name, f"destination directory {destination.parent} does not exist"
        )
    _assert_vacuum_into_supported()

    if destination.exists():
        raise BackupUploadError(destination.name, "snapshot destination already exists")

    factory = connect or _default_connect
    conn = factory(config)
    try:
        # Parameter binding is supported for VACUUM INTO and avoids any
        # quoting question about the path.
        conn.execute("VACUUM INTO ?", (str(destination),))
    except sqlite3.Error as exc:
        raise BackupUploadError(destination.name, f"VACUUM INTO failed: {exc}") from exc
    finally:
        conn.close()

    if not destination.is_file():
        raise BackupUploadError(destination.name, "VACUUM INTO produced no file")
    return destination.stat().st_size


def compress_file(source: Path, destination: Path) -> int:
    """Gzip ``source`` into ``destination``; return the compressed size."""
    if not source.is_file():
        raise BackupUploadError(destination.name, f"nothing to compress at {source}")
    with source.open("rb") as raw, gzip.open(destination, "wb", compresslevel=6) as packed:
        shutil.copyfileobj(raw, packed, length=_CHUNK_BYTES)
    return destination.stat().st_size


def decompress_file(source: Path, destination: Path) -> int:
    """Gunzip ``source`` into ``destination``; return the uncompressed size.

    Raises:
        BackupIntegrityError: the payload is not valid gzip.
    """
    if not source.is_file():
        raise BackupRestoreError(f"nothing to decompress at {source}")
    try:
        with gzip.open(source, "rb") as packed, destination.open("wb") as raw:
            shutil.copyfileobj(packed, raw, length=_CHUNK_BYTES)
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise BackupIntegrityError(source.name, f"not a readable gzip payload: {exc}") from exc
    return destination.stat().st_size


def verify_database(path: Path) -> None:
    """Run SQLite's own integrity check against ``path``.

    A snapshot that hashes correctly can still be a corrupt database if it was
    corrupt at the source, so the hash check and this check are complementary,
    not redundant.

    Raises:
        BackupIntegrityError: the file is unreadable or fails integrity_check.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BackupIntegrityError(path.name, f"cannot open snapshot: {exc}") from exc
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupIntegrityError(path.name, f"integrity_check failed: {exc}") from exc
    finally:
        conn.close()

    if not row or row[0] != "ok":
        detail = row[0] if row else "no result"
        raise BackupIntegrityError(path.name, f"integrity_check returned {detail!r}")


def _load_azure_sdk() -> tuple[Any, Any]:
    """Import the optional Azure SDK, translating ImportError into our own.

    Raises:
        BackupDependencyMissingError: the [azure] extra is not installed.
    """
    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise BackupDependencyMissingError("azure-storage-blob") from exc
    return DefaultAzureCredential, BlobServiceClient


def _validate_backup_config(config: LummennaConfig) -> str:
    """Check backup settings and return the storage account name.

    Raises:
        SovereignViolationError: sovereign mode is on; network egress refused.
        BackupNotConfiguredError: a required setting is absent.
    """
    config.assert_sovereign("azure blob backup")
    account = config.backup_account
    if not account:
        raise BackupNotConfiguredError("backup_account")
    if not config.backup_container:
        raise BackupNotConfiguredError("backup_container")
    return account


def container_client(config: LummennaConfig) -> Any:
    """Build an authenticated blob container client.

    Authentication is workload identity via DefaultAzureCredential. No account
    key or connection string is ever read, which is why none is configured.

    Raises:
        SovereignViolationError: sovereign mode is on.
        BackupNotConfiguredError: settings incomplete.
        BackupDependencyMissingError: [azure] extra not installed.
    """
    account = _validate_backup_config(config)
    credential_cls, service_cls = _load_azure_sdk()
    service = service_cls(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=credential_cls(),
    )
    return service.get_container_client(config.backup_container)


def _sleep_backoff(attempt: int, sleeper: Callable[[float], None]) -> None:
    """Sleep for capped exponential backoff before retry ``attempt``."""
    delay = min(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _BACKOFF_CAP_SECONDS)
    sleeper(delay)


def upload_snapshot(
    container: Any,
    blob_name: str,
    payload: Path,
    metadata: dict[str, str],
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Upload ``payload`` to ``blob_name`` with bounded retries.

    The retry budget is fixed at MAX_UPLOAD_ATTEMPTS with capped exponential
    backoff, so this cannot spin indefinitely against a failing endpoint.

    Raises:
        BackupUploadError: every attempt failed. The last error is attached.
    """
    if not payload.is_file():
        raise BackupUploadError(blob_name, f"payload missing at {payload}")
    last_detail = "no attempt made"

    for attempt in range(1, MAX_UPLOAD_ATTEMPTS + 1):
        try:
            with payload.open("rb") as handle:
                container.upload_blob(
                    name=blob_name,
                    data=handle,
                    overwrite=True,
                    metadata=metadata,
                )
            return
        except Exception as exc:  # noqa: BLE001 - SDK raises a broad family
            last_detail = f"attempt {attempt}/{MAX_UPLOAD_ATTEMPTS}: {exc}"
            _LOG.warning("Backup upload failed, %s", last_detail)
            if attempt < MAX_UPLOAD_ATTEMPTS:
                _sleep_backoff(attempt, sleeper)

    raise BackupUploadError(blob_name, last_detail)


def list_snapshots(container: Any) -> list[str]:
    """Return snapshot blob names, oldest first.

    Names embed a zero-padded UTC timestamp, so lexicographic order is
    chronological order.
    """
    names = [blob.name for blob in container.list_blobs(name_starts_with=f"{BLOB_PREFIX}/")]
    return sorted(names)


def prune_snapshots(container: Any, retention: int) -> list[str]:
    """Delete all but the newest ``retention`` snapshots; return deleted names.

    A retention of zero or less is treated as "keep everything", because an
    accidental zero should not be a data-destruction instruction.
    """
    if retention <= 0:
        return []

    names = list_snapshots(container)
    surplus = names[:-retention] if len(names) > retention else []

    deleted: list[str] = []
    for name in surplus:
        try:
            container.delete_blob(name)
            deleted.append(name)
        except Exception as exc:  # noqa: BLE001 - SDK raises a broad family
            # Pruning is housekeeping. A failure here must not fail the backup
            # that already succeeded, but it must be visible.
            _LOG.warning("Could not prune old snapshot %s: %s", name, exc)
    return deleted


def run_backup_once(
    config: LummennaConfig,
    connect: ConnectFactory | None = None,
    container: Any | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """Take one snapshot, replicate it, and prune old ones.

    Args:
        config: Active configuration. Must be non-sovereign and configured.
        connect: Connection factory override, for tests.
        container: Pre-built container client, for tests.
        now: Timestamp override, for deterministic blob names in tests.

    Returns:
        BackupResult describing the uploaded snapshot.

    Raises:
        SovereignViolationError: sovereign mode is enabled.
        BackupNotConfiguredError: backup settings incomplete.
        BackupUploadError: snapshot or upload failed after all retries.

    Complexity: O(n) in database size, plus one list and up to
    ``len(blobs) - retention`` deletes.
    """
    started = time.monotonic()
    # Checked here as well as in container_client, so that supplying a
    # pre-built client (tests, or any future caller that pools connections)
    # cannot route around the sovereign boundary.
    config.assert_sovereign("azure blob backup")
    client = container if container is not None else container_client(config)

    with tempfile.TemporaryDirectory(prefix="lummenna-backup-") as scratch:
        workdir = Path(scratch)
        raw = workdir / "snapshot.db"
        packed = workdir / "snapshot.db.gz"

        raw_bytes = snapshot_database(config, raw, connect=connect)
        verify_database(raw)
        checksum = sha256_file(raw)
        compressed_bytes = compress_file(raw, packed)

        name = blob_name_for(_utc_stamp(now))
        upload_snapshot(
            client,
            name,
            packed,
            {CHECKSUM_KEY: checksum, SIZE_KEY: str(raw_bytes)},
        )

    prune_snapshots(client, config.backup_retention)

    result = BackupResult(
        blob_name=name,
        raw_bytes=raw_bytes,
        compressed_bytes=compressed_bytes,
        checksum=checksum,
        duration_seconds=time.monotonic() - started,
    )
    _LOG.info(
        "Backed up %d bytes to %s (%.1f%% of raw) in %.2fs",
        result.raw_bytes,
        result.blob_name,
        result.compression_ratio * 100.0,
        result.duration_seconds,
    )
    return result


def _store_is_populated(config: LummennaConfig) -> bool:
    """True when a non-empty database file already exists locally."""
    path = database_path(config)
    return path.is_file() and path.stat().st_size > 0


def _download_and_verify(container: Any, name: str, workdir: Path) -> Path:
    """Fetch ``name``, decompress it, and verify hash plus integrity.

    Returns:
        Path to the verified, decompressed database inside ``workdir``.

    Raises:
        BackupIntegrityError: checksum mismatch or failed integrity check.
        BackupRestoreError: the download itself failed.
    """
    packed = workdir / "restore.db.gz"
    raw = workdir / "restore.db"

    try:
        downloader = container.download_blob(name)
        with packed.open("wb") as handle:
            downloader.readinto(handle)
        properties = container.get_blob_client(name).get_blob_properties()
    except Exception as exc:  # noqa: BLE001 - SDK raises a broad family
        raise BackupRestoreError(f"could not download {name}: {exc}") from exc

    decompress_file(packed, raw)

    expected = (properties.metadata or {}).get(CHECKSUM_KEY)
    if expected:
        actual = sha256_file(raw)
        if actual != expected:
            raise BackupIntegrityError(name, f"sha256 {actual} does not match recorded {expected}")
    else:
        _LOG.warning("Snapshot %s carries no %s metadata; hash not verified", name, CHECKSUM_KEY)

    verify_database(raw)
    return raw


def restore_if_missing(
    config: LummennaConfig,
    container: Any | None = None,
) -> bool:
    """Restore the newest snapshot when the local store is absent or empty.

    This is the cold-start half of the durability story. A replica that comes
    up on a fresh ephemeral disk calls this before serving traffic, and adopts
    the most recent snapshot if one exists.

    It is deliberately conservative: if a populated database is already
    present, it returns False and touches nothing. A backup system that can
    overwrite live data is a liability, not a safety net.

    Returns:
        True if a snapshot was restored, False if no action was needed.

    Raises:
        SovereignViolationError: sovereign mode is enabled.
        BackupNotConfiguredError: backup settings incomplete.
        BackupRestoreError: a snapshot existed but could not be restored.
        BackupIntegrityError: the snapshot failed verification.
    """
    # Unconditional, and before the populated check, so that the boundary is
    # enforced identically whether or not a client was injected.
    config.assert_sovereign("azure blob restore")

    if _store_is_populated(config):
        _LOG.debug("Local store already populated; skipping restore")
        return False

    client = container if container is not None else container_client(config)
    names = list_snapshots(client)
    if not names:
        _LOG.info("No snapshots present; starting with an empty store")
        return False

    newest = names[-1]
    target = database_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="lummenna-restore-") as scratch:
        verified = _download_and_verify(client, newest, Path(scratch))
        # Copy rather than rename: the scratch directory is frequently on a
        # different filesystem than the store, where rename fails with EXDEV.
        shutil.copyfile(verified, target)

    _LOG.info("Restored store from snapshot %s (%d bytes)", newest, target.stat().st_size)
    return True


class BackupScheduler:
    """Runs :func:`run_backup_once` on a fixed interval in a daemon thread.

    The thread is interruptible: :meth:`stop` sets an event that the sleep
    waits on, so shutdown does not block for a whole interval. Failures are
    logged and the loop continues, because a transient blob outage must not
    take down the API it is protecting.
    """

    def __init__(
        self,
        config: LummennaConfig,
        connect: ConnectFactory | None = None,
        container: Any | None = None,
    ) -> None:
        self._config = config
        self._connect = connect
        self._container = container
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0
        self._successes = 0
        self.interval = max(int(config.backup_interval_seconds), MIN_INTERVAL_SECONDS)

    @property
    def successes(self) -> int:
        """Count of completed backup cycles since start."""
        return self._successes

    @property
    def failures(self) -> int:
        """Count of failed backup cycles since start."""
        return self._failures

    @property
    def running(self) -> bool:
        """True while the worker thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the worker thread. Idempotent."""
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="lummenna-backup",
            daemon=True,
        )
        self._thread.start()
        _LOG.info("Backup scheduler started; interval %ds", self.interval)

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the worker to finish and wait up to ``timeout`` seconds."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        _LOG.info(
            "Backup scheduler stopped after %d success(es), %d failure(s)",
            self._successes,
            self._failures,
        )

    def run_cycle(self) -> BackupResult | None:
        """Execute one cycle, converting failure into a logged None.

        Exposed separately from the loop so tests can drive a single iteration
        without threads or sleeping.
        """
        try:
            result = run_backup_once(
                self._config,
                connect=self._connect,
                container=self._container,
            )
        except Exception as exc:  # noqa: BLE001 - loop must survive any failure
            self._failures += 1
            _LOG.error("Backup cycle failed: %s", exc)
            return None
        self._successes += 1
        return result

    def _loop(self) -> None:
        """Backup on a fixed interval until stopped.

        Termination is provable: the only exit is ``self._stop`` being set, and
        ``Event.wait`` returns True immediately once it is, so no iteration can
        block longer than ``interval``.
        """
        while not self._stop.is_set():
            self.run_cycle()
            if self._stop.wait(timeout=self.interval):
                return


def scheduler_if_enabled(
    config: LummennaConfig,
    connect: ConnectFactory | None = None,
) -> BackupScheduler | None:
    """Build a scheduler when backup is enabled and legal, else None.

    Returns None rather than raising when backup is simply switched off, which
    is the overwhelmingly common case. Misconfiguration while *enabled* still
    raises, because silently not backing up a deployment that asked for backup
    is precisely the failure this module exists to prevent.
    """
    if not config.backup_enabled:
        return None
    _validate_backup_config(config)
    return BackupScheduler(config, connect=connect)


def summarize(results: Sequence[BackupResult]) -> str:
    """Render a one-line human summary of a run of backups."""
    if not results:
        return "no backups recorded"
    total_raw = sum(item.raw_bytes for item in results)
    newest = results[-1]
    return (
        f"{len(results)} snapshot(s), {total_raw} raw bytes total, "
        f"newest {newest.blob_name} at {newest.compression_ratio * 100:.1f}% of raw"
    )
