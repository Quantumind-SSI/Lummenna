"""Re-embed all valid chunks in the Lumena database with the production embedder.

Used after installing a real embedding model to replace mock/dummy embeddings
that were created before the model was available.
"""
from __future__ import annotations

import sqlite3

from lummenna.config import LumenaConfig
from lummenna.data.schema import get_connection
from lummenna.force.contextual.embed import get_embedder
from lummenna.force.mnemonic.retrieval_dense import VectorChannel


def reembed_all(config: LumenaConfig | None = None) -> int:
    """Regenerate embeddings for every valid chunk and update vector indexes.

    Returns:
        Number of chunks re-embedded.
    """
    cfg = config or LumenaConfig()
    conn = get_connection(cfg)
    embedder = get_embedder(cfg, allow_mock=False)
    vector = VectorChannel(cfg, conn)

    rows = conn.execute(
        "SELECT chunk_id, content FROM chunk WHERE valid_to IS NULL"
    ).fetchall()

    reembedded = 0
    for chunk_id, content in rows:
        vec = embedder.encode_single(content)
        vector.add(chunk_id, vec)
        reembedded += 1

    conn.commit()
    conn.close()
    return reembedded


if __name__ == "__main__":
    count = reembed_all()
    print(f"Re-embedded {count} chunk(s).")
