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
        # First-run guard: if opted in, auto-provision the model once, then
        # retry. This performs a one-time network fetch (crossing the sovereign
        # boundary deliberately, because the operator asked for it) and caches
        # the model locally for all subsequent — offline — launches.
        if config.auto_provision_model:
            embedder = _auto_provision_and_load(config, conn, exc)
            if embedder is not None:
                embedding_model_available = True
                return _assemble_palace(conn, config, embedder, embedding_model_available)

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

    return _assemble_palace(conn, config, embedder, embedding_model_available)


def _assemble_palace(conn, config: LumenaConfig, embedder, embedding_model_available: bool) -> dict:
    """Build the pipeline/conversation/TFC around a ready connection + embedder."""
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


def _auto_provision_and_load(config: LumenaConfig, conn, original_exc: Exception):
    """Attempt a one-time model download+export, then load the embedder.

    Returns the loaded embedder on success, or ``None`` if provisioning failed
    (in which case the caller falls back to its normal missing-model handling).
    The sovereign boundary is deliberately bypassed for this single call because
    the operator explicitly opted in via ``auto_provision_model``.
    """
    from lumena.cli.models import provision_embedding_model

    logger.warning(
        "embedder_auto_provision_start",
        model=config.embedding_model,
        reason=str(original_exc),
        note="LUMENA_AUTO_PROVISION_MODEL=1 — one-time network fetch; sovereign thereafter",
    )
    try:
        config.model_path.mkdir(parents=True, exist_ok=True)
        # config=None ⇒ skip assert_sovereign; the opt-in flag is the consent.
        provision_embedding_model(config.embedding_model, config.model_path, config=None)
        embedder = get_embedder(config, allow_mock=False)
        logger.info("embedder_ready", model=config.embedding_model, provisioned=True)
        return embedder
    except Exception as exc:  # provisioning is best-effort; fall back on failure
        logger.error("embedder_auto_provision_failed", model=config.embedding_model, exc=str(exc))
        return None
