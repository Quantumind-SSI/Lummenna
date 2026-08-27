"""A8: Ebbinghaus Passive Decay (L1).

Input wire: APScheduler + SQLite (A1)
Output wire: A9 (V(m) scalar updates)
Secret sauce: User-specific decay rate (D16 user_profile.ebbinghaus_half_life_days)
"""

import math
import sqlite3
from datetime import datetime, timezone
from typing import Annotated

from pydantic import Field, validate_call

from lummenna.data.locks import maintenance_guard
from lummenna.logging import get_console_logger
from lummenna.sovereign.wear import WearAwareBatcher

logger = get_console_logger(__name__)

# Whitepaper 3.5 (L1): chunks whose value decays below this floor are released.
RELEASE_FLOOR = 0.05


@validate_call(config={"arbitrary_types_allowed": True})
def ebbinghaus_decay(
    conn: sqlite3.Connection,
    user_half_life_days: Annotated[float, Field(gt=0.0)] = 7.0,
    now: datetime | None = None,
    batcher: WearAwareBatcher | None = None,
) -> int:
    """
    R(t) = e^(-t / (h * ln(2))) where h = user-specific half-life in days.
    Applied as a multiplicative penalty to vm_score.
    Chunks with vm_score below ``RELEASE_FLOOR`` (0.05) are queued for release.

    ``user_half_life_days`` is validated to be strictly positive: a non-positive
    half-life would make the Ebbinghaus formula ill-defined (division by zero or
    a growing "retention"), violating the claim that R(t) ∈ (0, 1] is decay.
    """
    now = now or datetime.now(timezone.utc)
    unix_now = int(now.timestamp())

    # Decay is multiplicative on the vm_score this pass reads. If a second
    # process applies the same pass against the same pre-decay values, the
    # factor lands twice and memories decay at roughly double the configured
    # half-life, dropping below RELEASE_FLOOR early. Serialise the whole
    # read-compute-write sequence, and skip if another pass owns the window.
    with maintenance_guard(conn, "ebbinghaus_decay") as acquired:
        if not acquired:
            return 0

        rows = conn.execute(
            """SELECT chunk_id, vm_score, last_access_at, created_at
               FROM chunk
               WHERE valid_to IS NULL AND optical_level < 2
            """
        ).fetchall()

        hl_sec = user_half_life_days * 86400.0
        updates = []
        released = 0
        for chunk_id, vm, last_access, created_at in rows:
            created = created_at if created_at is not None else unix_now
            age_sec = max(0, unix_now - created)  # future-dated rows must not amplify Vm
            retention = math.exp(-age_sec / (hl_sec * math.log(2)))
            # Recency boost
            if last_access:
                recency_hours = max(0.0, (unix_now - last_access) / 3600.0)
                recency_boost = math.exp(-recency_hours / 24.0)
                retention = max(retention, recency_boost)
            # Invariant: R(t) ∈ (0, 1]. exp of a non-positive exponent is already in
            # this range; clamp defensively so decay can never increase Vm.
            retention = min(1.0, max(0.0, retention))
            new_vm = vm * retention
            if new_vm < RELEASE_FLOOR:
                new_vm = 0.0
                released += 1
            updates.append((new_vm, chunk_id))

        if batcher:
            for new_vm, chunk_id in updates:
                batcher.queue.append(
                    ("UPDATE chunk SET vm_score = ? WHERE chunk_id = ?", (new_vm, chunk_id))
                )
        else:
            conn.executemany("UPDATE chunk SET vm_score = ? WHERE chunk_id = ?", updates)
        logger.info(
            "decay_applied",
            chunks=len(updates),
            released=released,
            half_life_days=user_half_life_days,
        )
        return released
