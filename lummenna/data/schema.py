"""Schema initialisation helpers."""

import os
import re
import sqlite3
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from lummenna.data.migrate import migrate
from lummenna.logging import get_console_logger

logger = get_console_logger(__name__)
_SQL_PATH = Path(__file__).with_suffix(".sql")

# SQLite gained the built-in unixepoch() function in 3.38.0. On older builds we
# register a Python fallback so the schema DEFAULT (unixepoch()) and the many
# queries that call it keep working. We only need to support the argument forms
# actually used in this codebase: unixepoch() and unixepoch('now', '<±N unit>').
_SQLITE_HAS_UNIXEPOCH = sqlite3.sqlite_version_info >= (3, 38, 0)

_MODIFIER_RE = re.compile(r"^\s*([+-]?\d+)\s+(second|minute|hour|day|month|year)s?\s*$")
_MODIFIER_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    # Calendar-aware units are approximated; the codebase only uses day/second
    # modifiers, but we handle these defensively rather than silently erroring.
    "month": 30 * 86400,
    "year": 365 * 86400,
}


def _unixepoch_shim(*args: object) -> int:
    """Pure-Python fallback for SQLite's unixepoch() (added in 3.38.0).

    Mirrors the subset of behaviour used by Lummenna:
      * unixepoch()                     -> current unix time (UTC seconds)
      * unixepoch('now')                -> current unix time
      * unixepoch('now', '-7 days')     -> now shifted by the modifier
      * unixepoch(<int-or-numeric>)     -> that value interpreted as a unix ts

    Returns an integer number of seconds since the epoch, matching SQLite, which
    truncates toward the second for the default 'unixepoch' format.
    """
    if not args:
        return int(time.time())

    base = args[0]
    if base is None:
        return int(time.time())

    if isinstance(base, str) and base.strip().lower() == "now":
        dt = datetime.now(timezone.utc)
    elif isinstance(base, int | float):
        # Bare numeric time value: SQLite treats it as a unix timestamp when the
        # 'unixepoch' modifier is implied. Here we only receive it as seconds.
        dt = datetime.fromtimestamp(float(base), tz=timezone.utc)
    else:
        # Unsupported time string form for this shim; fall back to now so the
        # query still returns a sane value rather than raising inside SQLite.
        dt = datetime.now(timezone.utc)

    for modifier in args[1:]:
        if not isinstance(modifier, str):
            continue
        match = _MODIFIER_RE.match(modifier)
        if not match:
            continue
        amount = int(match.group(1))
        unit = match.group(2)
        dt = dt + timedelta(seconds=amount * _MODIFIER_SECONDS[unit])

    return int(dt.timestamp())


def register_compat_functions(conn: sqlite3.Connection) -> None:
    """Register SQL function shims for older SQLite builds.

    Idempotent and cheap: registering a UDF that also exists natively is
    harmless (the UDF takes precedence), but we skip it when the native
    function is available to preserve SQLite's exact semantics.
    """
    if _SQLITE_HAS_UNIXEPOCH:
        return
    # -1 arity => variadic, so both unixepoch() and unixepoch('now', '-7 days')
    # resolve to our shim.
    conn.create_function("unixepoch", -1, _unixepoch_shim, deterministic=False)


# Graceful SQLCipher import
try:
    import sqlcipher3

    _HAS_SQLCIPHER = True
except Exception:
    sqlcipher3 = None  # type: ignore[assignment]
    _HAS_SQLCIPHER = False


def _enforce_permissions(root_path: Path) -> None:
    """Ensure ~/.lummenna tree is 700 (dirs) and 600 (files). Fail-fast on error."""
    if not root_path.exists():
        return
    try:
        os.chmod(root_path, stat.S_IRWXU)  # 0o700
        for child in root_path.rglob("*"):
            if child.is_dir():
                os.chmod(child, stat.S_IRWXU)  # 0o700
            else:
                os.chmod(child, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except OSError as exc:
        raise RuntimeError(
            f"Cannot enforce file permissions on {root_path}. "
            f"Run as the file owner or check mount options."
        ) from exc


def init_db(conn: sqlite3.Connection) -> None:
    """Execute the canonical schema SQL against an open connection."""
    # Register unixepoch() BEFORE the schema runs: table DEFAULT (unixepoch())
    # clauses are evaluated at insert time, but the function must be resolvable
    # on this connection for any subsequent insert to succeed.
    register_compat_functions(conn)
    sql = _SQL_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current == 0:
        conn.execute("PRAGMA user_version = 1")
    migrate(conn)


def get_connection(config, ensure: bool = True) -> sqlite3.Connection:
    """Return a SQLite connection initialised with the Lummenna schema and optimised pragmas.

    Supports three encryption modes:
      - sqlcipher : Transparent page-level AES-256 encryption via SQLCipher.
      - fernet    : Field-level encryption for chunk.content only (fallback).
      - none      : Plaintext SQLite (not recommended for production).

    Args:
        config: LummennaConfig selecting store path and encryption mode.
        ensure: When True (default), run ``ensure_schema`` and the store-wide
            permission sweep before returning. The connection pool passes
            ``ensure=False``: the first pooled connection runs the full setup,
            and later ones skip the O(store-files) permission walk that would
            otherwise run on every request.
    """
    from lummenna.config import LummennaConfig

    cfg: LummennaConfig = config
    cfg.store_path.mkdir(parents=True, exist_ok=True)
    db_path = cfg.store_path / "lummenna.db"

    mode = getattr(cfg, "database_encryption_mode", "none")

    # Pro gate: encryption-at-rest (SQLCipher/Fernet) is a commercial feature.
    # The default (mode == "none") is fully functional in Community; enabling
    # any encryption mode requires a valid Pro license.
    if mode != "none":
        cfg.assert_licensed("encryption_at_rest")

    if mode == "sqlcipher":
        if not _HAS_SQLCIPHER:
            raise RuntimeError(
                "database_encryption_mode is 'sqlcipher' but sqlcipher3 is not installed. "
                "Install it with: pip install 'sqlcipher3>=0.6'"
            )
        conn = sqlcipher3.connect(str(db_path), check_same_thread=False)  # type: ignore[arg-type]
        passphrase = cfg.encryption_key or ""
        if not passphrase:
            raise RuntimeError(
                "database_encryption_mode='sqlcipher' requires LUMMENNA_ENCRYPTION_KEY to be set."
            )
        # Derive a high-entropy hex passphrase from the user's key
        from lummenna.security.crypto import derive_sqlcipher_passphrase

        derived = derive_sqlcipher_passphrase(passphrase, cfg.store_path)
        conn.execute(f"PRAGMA key = '{derived}'")
        cipher_ver = conn.execute("PRAGMA cipher_version").fetchone()
        if not cipher_ver or not cipher_ver[0]:
            raise RuntimeError("SQLCipher initialisation failed: cipher_version is empty.")
        logger.info("sqlcipher_enabled", version=cipher_ver[0])
    else:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        if mode == "none":
            logger.debug("encryption_at_rest_disabled")

    # sqlcipher3 cursor is not compatible with sqlite3.Row; use a typed tuple fallback
    if mode == "sqlcipher":
        # sqlcipher3 already returns Row-like objects by default in most builds,
        # but sqlite3.Row explodes with sqlcipher3 cursors. We default to None.
        pass  # keep driver default row factory
    else:
        conn.row_factory = sqlite3.Row
    # PRAGMA journal_mode is a query, not a command: SQLite returns the mode that
    # is actually in force. When the VFS cannot provide the shared-memory
    # primitives the wal-index needs -- which is the case on every network
    # filesystem, including SMB/CIFS such as Azure Files -- the pragma is a
    # silent no-op that leaves the database in rollback-journal mode.
    # See https://sqlite.org/wal.html sections 2.2 and 3.
    #
    # Accepting that silently would make the process lie about its own
    # durability model: wear.WriteQueue issues PRAGMA wal_checkpoint after every
    # flush, and that checkpoint degrades to a no-op outside WAL. Fail loudly
    # instead, naming the probable cause, so a store placed on network storage
    # is caught at startup rather than diagnosed after data is lost.
    applied_mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(applied_mode).lower() != "wal":
        raise RuntimeError(
            f"SQLite refused WAL mode for {db_path}; it is running in "
            f"'{applied_mode}' instead. WAL requires shared memory and does not "
            "work over a network filesystem (SMB/CIFS/NFS), so this usually means "
            "the store path is on a network mount. Point LUMENA_STORE_PATH at "
            "local disk or a block device."
        )
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA mmap_size = 268435456")
    # WAL allows one writer at a time; the API event loop and the background
    # scheduler thread hold separate connections. Without a busy timeout,
    # writer contention raises 'database is locked' immediately — with it,
    # SQLite retries internally for up to 5s before surfacing an error.
    conn.execute("PRAGMA busy_timeout = 5000")
    # Register compat shims (e.g. unixepoch on SQLite < 3.38) before any schema
    # DDL/DML runs on this connection.
    register_compat_functions(conn)
    if ensure:
        ensure_schema(conn)
        _enforce_permissions(cfg.store_path.parent)
    return cast(sqlite3.Connection, conn)


def get_encryption(config):
    """Return a FernetEncryption instance if field-level encryption is required.

    When database_encryption_mode is 'sqlcipher', page-level encryption already
    protects all data, so field-level Fernet is disabled to avoid double-encryption
    and to preserve FTS5 / BM25 search performance.

    Backward compatibility: if mode is 'none' but an encryption_key is set,
    we fall back to Fernet field-level encryption (previous default behaviour).

    Raises:
        RuntimeError: If mode is 'fernet' but no encryption key is configured.
            Silently storing plaintext while the operator believes encryption
            is enabled would be a security lie.
    """
    from lummenna.security.crypto import FernetEncryption

    mode = getattr(config, "database_encryption_mode", "none")
    has_key = getattr(config, "encryption_key", None)
    if mode == "sqlcipher":
        return FernetEncryption(None)
    if mode == "fernet" and not has_key:
        raise RuntimeError(
            "database_encryption_mode='fernet' requires LUMMENNA_ENCRYPTION_KEY "
            "to be set. Refusing to silently store plaintext."
        )
    if has_key:
        return FernetEncryption(config.encryption_key)
    return FernetEncryption(None)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Check PRAGMA user_version and initialise schema if database is empty."""
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    if user_version == 0 and not tables:
        init_db(conn)
        # init_db may have already advanced user_version via migrate
        if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
    migrate(conn)
