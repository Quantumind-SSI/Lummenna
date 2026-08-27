"""Operational concerns for hosted Lummenna deployments.

Nothing in this package is imported by the core memory path. It exists for
deployments whose local disk does not survive a restart and which therefore
need the SQLite store replicated somewhere durable.
"""

from lummenna.ops.backup import (
    BackupResult,
    BackupScheduler,
    restore_if_missing,
    run_backup_once,
)

__all__ = [
    "BackupResult",
    "BackupScheduler",
    "restore_if_missing",
    "run_backup_once",
]
