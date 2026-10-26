"""MCP (Model Context Protocol) server for Lummenna memory operations.

Exposes Lummenna's palace via stdio to any MCP-compatible host:
  OpenCode, GitHub Copilot, Claude Desktop, etc.

Usage::

    python -m lummenna.integrations.mcp_server

Or configured as an MCP server in host config::

    {
      "mcp": {
        "lummenna": {
          "type": "local",
          "command": ["python3", "-m", "lummenna.integrations.mcp_server"],
          "environment": {"LUMMENNA_DEVICE": "generic"}
        }
      }
    }
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

try:
    from mcp.server.fastmcp import Context, FastMCP
except Exception as _mcp_exc:  # pragma: no cover
    _MCP_AVAILABLE = False
    _MCP_ERROR: Exception | None = _mcp_exc
else:
    _MCP_AVAILABLE = True
    _MCP_ERROR = None

from lummenna import __version__
from lummenna._init import initialize_palace
from lummenna.config import LummennaConfig
from lummenna.contracts import FeedbackType, SourceType
from lummenna.controller import TwinForceController
from lummenna.conversation import ConversationMemory
from lummenna.force.mnemonic.store import store_memory
from lummenna.logging import get_console_logger
from lummenna.search import SearchPipeline

logger = get_console_logger(__name__)

# ---------------------------------------------------------------------------
# Input guardrails — Pydantic models validating every tool argument at the
# stdio boundary. MCP hosts are semi-trusted: an LLM can emit malformed,
# out-of-range, or oversized arguments. Validating here (before any DB write or
# embedding) turns those into clear, actionable errors instead of deep
# stack traces or silent misbehaviour. Bounds and closed literal sets are
# sourced from lummenna.contracts (the documented-claims source of truth).
# ---------------------------------------------------------------------------

# Upper bound on free-text arguments. Guards against a single tool call trying
# to persist a megabyte of text (which would blow the token budget and bloat
# the palace). Aligned with the API's request_max_size default order of
# magnitude while staying comfortable for normal notes/turns.
_MAX_CONTENT_CHARS = 100_000
_MAX_QUERY_CHARS = 8_000
_MAX_NAME_CHARS = 200

# A non-empty, whitespace-trimmed short identifier (room/locus names).
NameStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=_MAX_NAME_CHARS),
]
# A non-empty, trimmed body of text (content / messages).
ContentStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=_MAX_CONTENT_CHARS),
]
QueryStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=_MAX_QUERY_CHARS),
]
# Retrieval fan-out is bounded to a sane range (docstrings already say 1–50).
TopK = Annotated[int, Field(ge=1, le=50)]


class _StrictModel(BaseModel):
    """Base for tool-input models: reject unknown fields, forbid coercion sloppiness."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SearchInput(_StrictModel):
    query: QueryStr
    top_k: TopK = 5


class StoreInput(_StrictModel):
    content: ContentStr
    room: NameStr
    locus: NameStr | None = None
    # SourceType is the documented closed set from lummenna.contracts.
    source_type: SourceType = "user_input"


class AssembleInput(_StrictModel):
    query: QueryStr
    top_k: TopK = 5


class TurnInput(_StrictModel):
    user_msg: ContentStr
    assistant_msg: ContentStr
    room: NameStr = "conversations"


class FeedbackInput(_StrictModel):
    # Chunk IDs are positive autoincrement rowids.
    chunk_id: Annotated[int, Field(ge=1)]
    was_useful: bool
    feedback_type: FeedbackType = "explicit"


def _format_validation_error(func_name: str, exc: ValidationError) -> str:
    """Render a Pydantic ValidationError as a concise, LLM-actionable message."""
    problems = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(input)"
        problems.append(f"{loc}: {err.get('msg', 'invalid')}")
    return f"[{func_name}] Invalid arguments — " + "; ".join(problems)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


@dataclass
class LummennaAppState:
    config: LummennaConfig
    conn: sqlite3.Connection
    embedder: Any
    pipeline: SearchPipeline
    conversation: ConversationMemory
    tfc: TwinForceController
    embedding_model_available: bool


_module_state: LummennaAppState | None = None


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[LummennaAppState]:
    """Initialize Lummenna on server startup and close on shutdown."""
    global _module_state
    try:
        config = LummennaConfig()
        config.store_path.mkdir(parents=True, exist_ok=True)
        config.model_path.mkdir(parents=True, exist_ok=True)

        palace = initialize_palace(config)

        state = LummennaAppState(
            config=config,
            conn=palace["conn"],
            embedder=palace["embedder"],
            pipeline=palace["pipeline"],
            conversation=palace["conversation"],
            tfc=palace["tfc"],
            embedding_model_available=palace["embedding_model_available"],
        )
        _module_state = state

        logger.info("mcp_server_started", device=config.device, db=str(config.db_path))
        try:
            yield state
        finally:
            palace["conn"].close()
            _module_state = None
            logger.info("mcp_server_stopped")
    except Exception as exc:
        logger.error("mcp_lifespan_init_failed", exc=str(exc))
        raise


class _UnavailableMCP:
    """Import-safe stand-in when the ``mcp`` package is not installed.

    Provides no-op ``tool``/``resource`` decorators so this module can be
    imported (and ``main()`` can print an actionable error) instead of
    crashing with ``AttributeError: 'NoneType' object has no attribute
    'tool'`` at import time.
    """

    def tool(self, *args, **kwargs):
        def _decorator(fn):
            return fn

        return _decorator

    def resource(self, *args, **kwargs):
        def _decorator(fn):
            return fn

        return _decorator

    def run(self, *args, **kwargs):
        raise RuntimeError(
            "The 'mcp' package is required for the MCP server. "
            "Install with: pip install lummenna[mcp]"
        )


mcp: FastMCP | _UnavailableMCP = (
    FastMCP("lummenna", lifespan=app_lifespan) if _MCP_AVAILABLE else _UnavailableMCP()
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(ctx: Context | None = None) -> LummennaAppState:
    if ctx is not None:
        try:
            state: LummennaAppState = ctx.request_context.lifespan_context
            return state
        except Exception:
            pass
    if _module_state is not None:
        return _module_state
    raise RuntimeError("Lummenna MCP server is not initialized. Run inside an active lifespan.")


def _tool_error(func_name: str, exc: Exception) -> str:
    """Convert an exception into a user-friendly MCP tool error string."""
    if isinstance(exc, sqlite3.Error):
        return (
            f"[{func_name}] Database error: {exc}. "
            "Hint: ensure no other process (e.g. `lummenna serve`) is locking the database."
        )
    if isinstance(exc, RuntimeError):
        return f"[{func_name}] {exc}"
    return f"[{func_name}] Unexpected error: {exc}"


_PREVIEW_ELLIPSIS = "…"


def _preview(content: str, max_chars: int) -> str:
    """Return a search-preview of *content* bounded to *max_chars* characters.

    ``lummenna_search`` is a ranked index, not a full-content reader, so long
    chunks are shown as a preview. When the content is cut, a single-character
    ellipsis marker is appended so the caller can distinguish a genuinely short
    chunk from a truncated one (read the full chunk from the store via its
    ``chunk_id``). A non-positive *max_chars* disables truncation entirely.

    Complexity: O(max_chars) — a single bounded slice, no scan of the tail.
    """
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    return content[:max_chars] + _PREVIEW_ELLIPSIS


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def lummenna_search(query: str, top_k: int = 5, ctx: Context | None = None) -> str:
    """Search the Lummenna memory palace with semantic + lexical hybrid retrieval.

    Args:
        query: Natural-language search query.
        top_k: Maximum number of memories to return (1-50).
    """
    try:
        args = SearchInput(query=query, top_k=top_k)
    except ValidationError as exc:
        return _format_validation_error("lummenna_search", exc)
    try:
        state = _state(ctx)
        results = state.pipeline.execute(args.query, k=args.top_k)
        if not results:
            return "No memories found."

        preview_chars = state.config.search_preview_chars
        lines = [f"Results for: {args.query}", ""]
        for rank, rc in enumerate(results[: args.top_k], 1):
            preview = _preview(rc.content, preview_chars)
            lines.append(
                f"{rank}. [{rc.room_name}/{rc.locus_name}] score={rc.final_score:.3f} "
                f"chunk_id={rc.chunk_id}\n   {preview}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return _tool_error("lummenna_search", exc)


@mcp.tool()
async def lummenna_store(
    content: str,
    room: str,
    locus: str | None = None,
    source_type: str = "user_input",
    ctx: Context | None = None,
) -> str:
    """Store a memory chunk in the Lummenna palace.

    Args:
        content: Text to persist.
        room: Room name (e.g., 'decisions', 'architecture', 'snippets').
        locus: Optional sub-location within the room.
        source_type: One of user_input, agent_reasoning, consolidation, import, p2p_share.
    """
    try:
        # source_type is an untrusted plain string from the MCP host; Pydantic
        # validates it against the SourceType literal set at runtime (the whole
        # point of this guardrail), so the static type mismatch is expected.
        args = StoreInput(
            content=content,
            room=room,
            locus=locus,
            source_type=source_type,  # type: ignore[arg-type]
        )
    except ValidationError as exc:
        return _format_validation_error("lummenna_store", exc)
    try:
        state = _state(ctx)
        embedding = (
            state.embedder.encode_single(args.content) if state.embedder is not None else None
        )
        chunk_id = store_memory(
            state.conn,
            content=args.content,
            room_name=args.room,
            locus_name=args.locus,
            source_type=args.source_type,
            embedding=embedding,
            config=state.config,
        )
        return f"Stored chunk_id={chunk_id} in room '{args.room}'."
    except Exception as exc:
        return _tool_error("lummenna_store", exc)


@mcp.tool()
async def lummenna_assemble(query: str, top_k: int = 5, ctx: Context | None = None) -> str:
    """Retrieve memories and assemble a ready-to-use context string.

    Args:
        query: User message or task description.
        top_k: Max memories to retrieve.
    """
    try:
        args = AssembleInput(query=query, top_k=top_k)
    except ValidationError as exc:
        return _format_validation_error("lummenna_assemble", exc)
    try:
        state = _state(ctx)
        turn = state.conversation.retrieve_and_assemble(args.query, top_k=args.top_k)
        return (
            f"Assembled context ({len(turn.retrieved_chunks)} chunks retrieved):\n\n"
            f"{turn.assembled_context}"
        )
    except Exception as exc:
        return _tool_error("lummenna_assemble", exc)


@mcp.tool()
async def lummenna_turn(
    user_msg: str,
    assistant_msg: str,
    room: str = "conversations",
    ctx: Context | None = None,
) -> str:
    """Store a full conversation turn and log implicit feedback.

    Args:
        user_msg: User's message.
        assistant_msg: Assistant's response.
        room: Room to store the turn in.
    """
    try:
        args = TurnInput(user_msg=user_msg, assistant_msg=assistant_msg, room=room)
    except ValidationError as exc:
        return _format_validation_error("lummenna_turn", exc)
    try:
        state = _state(ctx)
        turn = state.conversation.retrieve_and_assemble(args.user_msg, top_k=5)
        user_id, assistant_id = state.conversation.store_turn(
            user_msg=args.user_msg,
            assistant_msg=args.assistant_msg,
            retrieved_chunks=turn.retrieved_chunks,
            room_name=args.room,
        )
        return (
            f"Turn stored: user_chunk_id={user_id}, "
            f"assistant_chunk_id={assistant_id} in room '{args.room}'."
        )
    except Exception as exc:
        return _tool_error("lummenna_turn", exc)


@mcp.tool()
async def lummenna_feedback(
    chunk_id: int,
    was_useful: bool,
    feedback_type: str = "explicit",
    ctx: Context | None = None,
) -> str:
    """Log explicit or implicit feedback for a retrieved memory chunk.

    Args:
        chunk_id: The chunk ID to rate.
        was_useful: True if the memory was helpful, False otherwise.
        feedback_type: explicit, implicit, or repair.
    """
    try:
        # feedback_type is an untrusted plain string from the MCP host; Pydantic
        # validates it against the FeedbackType literal set at runtime.
        args = FeedbackInput(
            chunk_id=chunk_id,
            was_useful=was_useful,
            feedback_type=feedback_type,  # type: ignore[arg-type]
        )
    except ValidationError as exc:
        return _format_validation_error("lummenna_feedback", exc)
    try:
        state = _state(ctx)
        state.conversation.log_explicit_feedback(
            chunk_id=args.chunk_id,
            was_useful=args.was_useful,
            feedback_type=args.feedback_type,
        )
        return f"Feedback logged for chunk_id={args.chunk_id} (useful={args.was_useful})."
    except Exception as exc:
        return _tool_error("lummenna_feedback", exc)


@mcp.tool()
async def lummenna_status(ctx: Context | None = None) -> str:
    """Show current palace status: room count, active chunks, TFC state."""
    try:
        state = _state(ctx)
        room_count = state.conn.execute("SELECT COUNT(*) FROM room").fetchone()[0]
        chunk_count = state.conn.execute(
            "SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL"
        ).fetchone()[0]
        env = state.tfc.to_env()
        return (
            f"Lummenna Status\n"
            f"  Device: {state.config.device}\n"
            f"  Rooms: {room_count}\n"
            f"  Active chunks: {chunk_count}\n"
            f"  Context budget: {state.config.context_budget} tokens\n"
            f"  TFC → e={env['e']:.2f} a={env['a']:.2f} tau={env['tau']:.1f} r={env['r']}\n"
            f"  Embedding model available: {state.embedding_model_available}\n"
            f"\nDashboard: http://localhost:{state.config.api_port}/\n"
            f"Run `lummenna serve` or `lummenna_dashboard` for real-time effectiveness metrics."
        )
    except Exception as exc:
        return _tool_error("lummenna_status", exc)


@mcp.tool()
async def lummenna_dashboard(ctx: Context | None = None) -> str:
    """Display runtime health, committed benchmark evidence, and memory state.
    Call this when the user asks about Lummenna's effectiveness, performance, or wants to see the dashboard.
    Metrics come from committed benchmark artifacts; anything not yet measured is shown as N/A.
    """
    try:
        state = _state(ctx)
        metrics = _dashboard_metrics(state)
        m = metrics
        retrieval = m["evaluation"]["retrieval"]
        recall = retrieval["recall_at_10"]
        latency = retrieval["latency_mean_ms"]
        feedback_pct = m["feedback"]["satisfaction_pct"]

        room_lines = (
            "  " + ", ".join(f"{r['name']}({r['active_chunks']})" for r in m["palace"]["topology"])
            if m["palace"]["topology"]
            else "  (none)"
        )

        return (
            f"╔══════════════════════════════════════════════╗\n"
            f"║     Lummenna Effectiveness Dashboard v{__version__}    ║\n"
            f"╠══════════════════════════════════════════════╣\n"
            f"║ System Health                               ║\n"
            f"╠══════════════════════════════════════════════╣\n"
            f"  Device:         {m['system']['device']}\n"
            f"  Embedder:       {m['system']['embedder']} ({'LOADED' if m['system']['embedder_loaded'] else 'MOCK'})\n"
            f"  Active Chunks:  {m['palace']['active_chunks']:,}\n"
            f"  Rooms / Loci:   {m['palace']['rooms']} / {m['palace']['loci']}\n"
            f"  Released:       {m['palace']['released_chunks']:,}\n"
            f"\n"
            f"  Retrieval Evidence (corpus: {retrieval['corpus'] or 'not available'}):\n"
            f"  BM25 R@10:          {_fmt_metric(recall['bm25'])}\n"
            f"  Hybrid R@10:        {_fmt_metric(recall['hybrid'])}\n"
            f"  Dense R@10:         {_fmt_metric(recall['dense'])}\n"
            f"  Mean latency BM25:  {_fmt_metric(latency['bm25'])} ms\n"
            f"  Mean latency Dense: {_fmt_metric(latency['dense'])} ms\n"
            f"  Mean latency Hybrid:{_fmt_metric(latency['hybrid'])} ms\n"
            f"  External rank:      {_fmt_metric(m['evaluation']['official_leaderboard_rank'])}\n"
            f"\n"
            f"╔══════════════════════════════════════════════╗\n"
            f"║ TFC State                                  ║\n"
            f"╠══════════════════════════════════════════════╣\n"
            f"  e  = {m['tfc']['e']:.2f}  mnemonic/conservation bias\n"
            f"  a  = {m['tfc']['a']:.2f}  attentional temperature\n"
            f"  τ  = {m['tfc']['tau']:.1f}  temporal horizon (days)\n"
            f"  r  = {m['tfc']['r']}  resolution level\n"
            f"\n"
            f"╔══════════════════════════════════════════════╗\n"
            f"║ Memory Palace Topology                      ║\n"
            f"╠══════════════════════════════════════════════╣\n"
            f"{room_lines}\n"
            f"\n"
            f"╔══════════════════════════════════════════════╗\n"
            f"║ Memory Lifecycle                           ║\n"
            f"╠══════════════════════════════════════════════╣\n"
            f"  Scheduled L1/L3 state:   NOT OBSERVED BY MCP\n"
            f"  L2 Interference:         ON WRITE\n"
            f"  Degradation Chain:       FP32 → FP16 → INT8 → BINARY → RELEASED\n"
            f"  Active resolution mix:   {m['palace']['resolution_counts']}\n"
            f"  Released Chunks:         {m['palace']['released_chunks']}\n"
            f"\n"
            f"╔══════════════════════════════════════════════╗\n"
            f"║ Runtime Configuration                       ║\n"
            f"╠══════════════════════════════════════════════╣\n"
            f"  Sovereign mode:           {'ENABLED' if m['runtime']['sovereign_mode'] else 'DISABLED'}\n"
            f"  Off-device backup:        {'ENABLED' if m['runtime']['backup_enabled'] else 'DISABLED'}\n"
            f"  Encryption mode:          {m['runtime']['encryption_mode']}\n"
            f"  Edition:                  {m['runtime']['edition']}\n"
            f"  Feedback Satisfaction:    {_fmt_metric(feedback_pct)}%\n"
            f"  Feedback Total:           {m['feedback']['total']}\n"
            f"\n"
            f"Web dashboard: http://localhost:{state.config.api_port}/\n"
            f"Machine metrics: http://localhost:{state.config.api_port}/metrics\n"
            f"Run `lummenna serve` to start the full API + dashboard."
        )
    except Exception as exc:
        return _tool_error("lummenna_dashboard", exc)


# ---------------------------------------------------------------------------
# Resources (exposed as MCP resources visible in OpenCode workspace)
# ---------------------------------------------------------------------------


def _fmt_metric(value) -> str:
    """Format a benchmark metric or show 'N/A' if not yet available."""
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _load_evaluation_summary() -> dict:
    """Load committed evaluation metrics (shared loader).

    Delegates to :mod:`lummenna.bench_results` so the MCP dashboard and the
    HTTP API report identical artifact-backed numbers; unmeasured metrics
    are ``None`` — no hardcoded fallback numbers, ever.
    """
    from lummenna.bench_results import load_evaluation_summary

    return load_evaluation_summary()


def _dashboard_metrics(state: LummennaAppState) -> dict:
    """Build dashboard metrics dict used by resources and tools."""

    conn = state.conn
    env = state.tfc.to_env()

    room_count = conn.execute("SELECT COUNT(*) FROM room").fetchone()[0]
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL").fetchone()[0]
    locus_count = conn.execute("SELECT COUNT(*) FROM locus").fetchone()[0]
    released = conn.execute(
        "SELECT COUNT(*) FROM chunk WHERE valid_to IS NOT NULL AND optical_level = 2"
    ).fetchone()[0]

    feedback_row = conn.execute(
        "SELECT COUNT(*) AS c, AVG(CASE WHEN positive = 1 THEN 1.0 ELSE 0.0 END) AS sat FROM feedback_log"
    ).fetchone()

    rooms = conn.execute(
        """
        SELECT r.name, COUNT(c.chunk_id) AS active_chunks
        FROM room r
        LEFT JOIN chunk c ON c.room_id = r.room_id AND c.valid_to IS NULL
        GROUP BY r.room_id
        HAVING COUNT(c.chunk_id) > 0
        ORDER BY active_chunks DESC
        """
    ).fetchall()

    resolution_rows = conn.execute(
        "SELECT resolution, COUNT(*) AS chunks FROM chunk WHERE valid_to IS NULL GROUP BY resolution"
    ).fetchall()
    resolution_counts = {row["resolution"]: row["chunks"] for row in resolution_rows}
    feedback_total = feedback_row["c"] or 0

    return {
        "version": __version__,
        "system": {
            "device": state.config.device,
            "embedder": state.config.embedding_model,
            "embedder_loaded": state.embedding_model_available,
        },
        "palace": {
            "rooms": room_count,
            "loci": locus_count,
            "active_chunks": chunk_count,
            "released_chunks": released,
            "resolution_counts": resolution_counts,
            "topology": [{"name": r["name"], "active_chunks": r["active_chunks"]} for r in rooms],
        },
        "tfc": {
            "e": env["e"],
            "a": env["a"],
            "tau": env["tau"],
            "r": env["r"],
        },
        "evaluation": _load_evaluation_summary(),
        "feedback": {
            "total": feedback_total,
            "satisfaction_pct": (
                round(feedback_row["sat"] * 100, 1) if feedback_row["sat"] is not None else None
            ),
        },
        "runtime": {
            "sovereign_mode": state.config.sovereign,
            "backup_enabled": state.config.backup_enabled,
            "encryption_mode": state.config.database_encryption_mode,
            "edition": state.config.edition,
        },
        "design_properties": {
            "bm25": "Lummenna uses SQLite FTS5; standard BM25",
            "dense": "Cosine similarity via sqlite-vec or brute-force",
            "hybrid": "RRF fusion; no external-system comparison has been run",
            "edge": (
                "Device profiles are implemented; the committed perf artifact "
                "records no host platform, so it attests to no specific hardware"
            ),
            "memory_lifecycle": "3-layer decay/interference/eviction",
        },
    }


# NOTE: these two resource functions deliberately take NO parameters, not even
# an optional `ctx`. FastMCP's @resource decorator branches on
# `has_uri_params or has_func_params` (mcp.server.fastmcp.FastMCP.resource);
# any function parameter sends the resource down the *template* path. A
# template whose URI carries no `{placeholder}` is unreachable: it never
# appears in list_resources() and read_resource() raises
# "Unknown resource: ...". Both resources were registered that way, and thus
# dead, until 2026-09-10. State is resolved through the module-level fallback
# in _state(), which app_lifespan populates.


@mcp.resource("dashboard://lummenna/effectiveness")
async def dashboard_effectiveness() -> str:
    """Lummenna memory store effectiveness overview.
    Shows real-time system health, artifact-backed evaluation, and TFC state.
    """
    try:
        state = _state()
        return json.dumps(_dashboard_metrics(state), indent=2, default=str)
    except Exception as exc:
        return _tool_error("dashboard_effectiveness", exc)


@mcp.resource("dashboard://lummenna/capabilities")
async def dashboard_capabilities() -> str:
    """Lummenna capability highlights (qualitative design properties).
    Includes committed retrieval metrics when benchmark artifacts exist;
    contains no comparisons to external systems.
    """
    try:
        state = _state()
        metrics = _dashboard_metrics(state)
        return json.dumps(
            {
                "design_properties": metrics["design_properties"],
                "evaluation": metrics["evaluation"],
                "runtime": metrics["runtime"],
            },
            indent=2,
            default=str,
        )
    except Exception as exc:
        return _tool_error("dashboard_capabilities", exc)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


#: Transports this server can serve. ``stdio`` is one process per client, which
#: is the MCP default and the right choice for a single editor session.
#: ``streamable-http`` serves many clients from one process, which is what makes
#: a multi-session setup affordable: palace init costs ~750 MB resident (the
#: ONNX runtime plus the tokenizer stack), so N concurrent stdio servers cost
#: N times that for one shared database.
TRANSPORTS = ("stdio", "streamable-http", "sse")


def main(
    transport: str = "stdio",
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Serve the palace over an MCP transport.

    Args:
        transport: One of :data:`TRANSPORTS`. ``stdio`` speaks the protocol on
            stdin/stdout for a client that spawned this process.
            ``streamable-http`` listens so that many sessions can share one
            process, one loaded embedder and one database connection.
        host: Bind address for network transports. Defaults to the configured
            ``api_host`` (loopback), never all-interfaces implicitly.
        port: Listen port for network transports. Defaults to ``api_port + 1``
            so an MCP endpoint never collides with the REST API.

    Raises:
        SystemExit: On an unknown transport, a missing ``mcp`` package, or a
            fatal serving error.
    """
    if not _MCP_AVAILABLE:
        print(
            "ERROR: The 'mcp' package is required for the MCP server. "
            "Install with: pip install lummenna[mcp]",
            file=sys.stderr,
        )
        sys.exit(1)
    if transport not in TRANSPORTS:
        print(
            f"ERROR: unknown transport '{transport}'; choose from {', '.join(TRANSPORTS)}",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        if transport == "stdio":
            mcp.run(transport="stdio")
            return

        config = LummennaConfig()
        bind_host = host if host is not None else config.api_host
        bind_port = port if port is not None else config.api_port + 1
        # FastMCP reads its bind address from settings rather than run() args.
        # main() already exited when the mcp package is absent, so the cast
        # is safe here; it exists purely to keep mypy from re-deriving the
        # union it cannot narrow across module scope.
        fastmcp_server = cast("FastMCP", mcp)
        fastmcp_server.settings.host = bind_host
        fastmcp_server.settings.port = bind_port
        # Network transports are a deliberate LAN exposure decision, so say so
        # on stderr rather than binding silently.
        print(
            f"Lummenna MCP serving {transport} on http://{bind_host}:{bind_port}/mcp\n"
            f"  store: {config.db_path}\n"
            f"  One process serves every connected session; "
            f"point clients at this URL instead of spawning per-session servers.",
            file=sys.stderr,
        )
        mcp.run(transport=transport)  # type: ignore[arg-type]
    except Exception as exc:
        logger.error("mcp_server_fatal_error", transport=transport, exc=str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
