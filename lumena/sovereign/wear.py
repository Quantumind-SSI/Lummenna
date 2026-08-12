"""A12: Wear-Aware Write Batcher.

Input wire: caller-populated write queue, SQLite WAL
Output wire: Disk I/O
Secret sauce: batched sequential transactions reduce write amplification on
SD/eMMC media.

Usage model: callers append ``(sql, params)`` tuples to ``batcher.queue``
and flush with ``flush_sync`` (see forgetting_l1_decay, forgetting_l3_budget,
consolidation, sleep). This is a synchronous API by design — an earlier
asyncio queue/loop variant was never wired into any code path and has been
removed rather than advertised.
"""

import sqlite3
from collections import deque

from lumena.logging import get_console_logger

logger = get_console_logger(__name__)


class WearAwareBatcher:
    """
    Collects write operations and flushes as large sequential SQLite transactions.
    Target: write amplification < 1.1x vs naive per-query writes.
    """

    def __init__(
        self, conn: sqlite3.Connection, max_batch_size: int = 100, max_latency_ms: float = 500
    ):
        self.conn = conn
        self.queue: deque[tuple[str, tuple]] = deque()
        self.max_batch = max_batch_size
        self.max_latency = max_latency_ms / 1000.0

    def flush_sync(self, batch: list[tuple[str, tuple]]) -> None:
        """Flush *batch* as one transaction followed by a passive WAL checkpoint."""
        with self.conn:
            for sql, params in batch:
                self.conn.execute(sql, params)
        self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        logger.debug("wear_flush", count=len(batch))
