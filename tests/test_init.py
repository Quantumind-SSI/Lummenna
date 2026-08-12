import sqlite3

import pytest

from lumena._init import initialize_palace
from lumena.config import LumenaConfig
from lumena.conversation import ConversationMemory
from lumena.search import SearchPipeline


@pytest.fixture
def temp_config(tmp_path):
    return LumenaConfig(
        device="generic",
        vector_index="sqlite-vec",
        store_path=str(tmp_path / ".lumena"),
        # No real ONNX model exists in tmp_path; tests opt into the explicit
        # mock escape hatch. Production default (False) raises instead.
        allow_mock_embedder=True,
    )


def test_initialize_palace_raises_without_model_by_default(tmp_path):
    """Without allow_mock_embedder, a missing ONNX model must abort startup.

    Regression test: initialize_palace used to silently substitute
    hash-based mock embeddings, making semantic search meaningless.
    """
    from lumena.brand.errors import ModelNotAvailableError

    config = LumenaConfig(
        device="generic",
        vector_index="sqlite-vec",
        store_path=str(tmp_path / ".lumena"),
        model_path=str(tmp_path / "models"),
        allow_mock_embedder=False,
    )
    with pytest.raises(ModelNotAvailableError):
        initialize_palace(config)


def test_initialize_palace_returns_correct_keys(temp_config):
    result = initialize_palace(temp_config)
    assert "conn" in result
    assert "embedder" in result
    assert "pipeline" in result
    assert "conversation" in result
    assert "tfc" in result
    assert "embedding_model_available" in result


def test_initialize_palace_conn_is_sqlite_connection(temp_config):
    result = initialize_palace(temp_config)
    assert isinstance(result["conn"], sqlite3.Connection)
    result["conn"].close()


def test_initialize_palace_creates_pipeline_and_conversation(temp_config):
    result = initialize_palace(temp_config)
    assert isinstance(result["pipeline"], SearchPipeline)
    assert isinstance(result["conversation"], ConversationMemory)
    result["conn"].close()


def test_initialize_palace_embedding_model_available_is_boolean(temp_config):
    result = initialize_palace(temp_config)
    assert isinstance(result["embedding_model_available"], bool)
    result["conn"].close()
