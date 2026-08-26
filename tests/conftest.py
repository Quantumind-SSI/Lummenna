"""D14: In-Memory Test Fixtures & Harness.

Input wire: pytest, SQLite :memory:, mock embedding model
Output wire: All test files
"""

import sqlite3

import numpy as np
import pytest

from lummenna.config import LummennaConfig
from lummenna.data.schema import init_db


@pytest.fixture
def memory_db():
    """Yield an in-memory SQLite connection with the Lummenna schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def mock_embedder():
    """Return a deterministic mock embedder for tests."""

    class MockEmbedder:
        dims = 384

        def encode_single(self, text: str) -> np.ndarray:
            # Deterministic pseudo-embedding from text hash
            h = hash(text) % (2**31)
            np.random.seed(h)
            vec = np.random.randn(self.dims).astype(np.float32)
            return vec / np.linalg.norm(vec)

    return MockEmbedder()


@pytest.fixture
def test_config():
    """Return a LummennaConfig tuned for testing."""
    return LummennaConfig(
        vector_index="sqlite-vec",
        device="generic",
        store_path="/tmp/.lummenna-test",
    )
