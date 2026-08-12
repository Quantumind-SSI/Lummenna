"""Shared palace initialization — used by both API server and MCP server."""

from __future__ import annotations

from lumena.brand.errors import ModelNotAvailableError
from lumena.config import LumenaConfig
from lumena.controller import TwinForceController
from lumena.conversation import ConversationMemory
from lumena.data.schema import ensure_schema, get_connection
from lumena.force.contextual.embed import MockEmbedder, get_embedder
from lumena.logging import get_console_logger
from lumena.search import SearchPipeline

logger = get_console_logger(__name__)


def initialize_palace(config: LumenaConfig) -> dict:
    """Initialize the memory palace: schema, embedder, pipeline, conversation.

    Returns a dictionary with keys: conn, embedder, pipeline, conversation,
    tfc, embedding_model_available. The caller is responsible for closing
    conn when done.

    Raises:
        ModelNotAvailableError: If the configured ONNX embedding model cannot
            be loaded and ``config.allow_mock_embedder`` is False (the
            default). Serving hash-based mock embeddings silently would make
            semantic search meaningless, so startup fails loudly instead.
    """
    conn = get_connection(config)
    ensure_schema(conn)

    embedding_model_available = False
    try:
        embedder = get_embedder(config, allow_mock=False)
        embedding_model_available = True
        logger.info("embedder_ready", model=config.embedding_model)
    except ModelNotAvailableError as exc:
        if not config.allow_mock_embedder:
            conn.close()
            raise ModelNotAvailableError(
                f"{exc}. Download it with 'lumena model download "
                f"{config.embedding_model}', or set LUMENA_ALLOW_MOCK_EMBEDDER=1 "
                f"(testing only — semantic search will be meaningless)."
            ) from exc
        logger.warning(
            "embedder_mock_substituted",
            reason=str(exc),
            warning="LUMENA_ALLOW_MOCK_EMBEDDER=1 — semantic search is meaningless",
        )
        embedder = MockEmbedder(dims=config.embedding_dims)

    pipeline = SearchPipeline(conn, config, embedder=embedder)
    conversation = ConversationMemory(config=config, conn=conn, embedder=embedder)
    tfc = TwinForceController()

    return {
        "conn": conn,
        "embedder": embedder,
        "pipeline": pipeline,
        "conversation": conversation,
        "tfc": tfc,
        "embedding_model_available": embedding_model_available,
    }
