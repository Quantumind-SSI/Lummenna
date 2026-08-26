"""MCP (Model Context Protocol) server for Lumena memory operations.

Exposes Lumena's palace via stdio to any MCP-compatible host:
  OpenCode, GitHub Copilot, Claude Desktop, etc.

Usage::

    python -m lummenna.integrations.mcp_server

Or configured as an MCP server in host config::

    {
      "mcp": {
        "lummenna": {
          "type": "local",
          "command": ["python3", "-m", "lummenna.integrations.mcp_server"],
          "environment": {"LUMENA_DEVICE": "generic"}
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
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

try:
    from mcp.server.fastmcp import Context, FastMCP
except Exception as _mcp_exc:  # pragma: no cover
    _MCP_AVAILABLE = False
    _MCP_ERROR: Exception | None = _mcp_exc
else:
    _MCP_AVAILABLE = True
    _MCP_ERROR = None

from lummenna._init import initialize_palace
from lummenna.config import LumenaConfig
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
class LumenaAppState:
    config: LumenaConfig
    conn: sqlite3.Connection
    embedder: Any
    pipeline: SearchPipeline
    conversation: ConversationMemory
    tfc: TwinForceController
    embedding_model_available: bool


_module_state: LumenaAppState | None = None


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[LumenaAppState]:
    """Initialize Lumena on server startup and close on shutdown."""
    global _module_state
    try:
        config = LumenaConfig()
        config.store_path.mkdir(parents=True, exist_ok=True)
        config.model_path.mkdir(parents=True, exist_ok=True)

        palace = initialize_palace(config)

        state = LumenaAppState(
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


mcp = FastMCP("lummenna", lifespan=app_lifespan) if _MCP_AVAILABLE else _UnavailableMCP()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(ctx: Context | None = None) -> LumenaAppState:
    if ctx is not None:
        try:
            state: LumenaAppState = ctx.request_context.lifespan_context
            return state
        except Exception:
            pass
    if _module_state is not None:
        return _module_state
    raise RuntimeError("Lumena MCP server is not initialized. Run inside an active lifespan.")


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

    ``lumena_search`` is a ranked index, not a full-content reader, so long
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
async def lumena_search(query: str, top_k: int = 5, ctx: Context = None) -> str:
    """Search the Lumena memory palace with semantic + lexical hybrid retrieval.

    Args:
        query: Natural-language search query.
        top_k: Maximum number of memories to return (1-50).
    """
    try:
        args = SearchInput(query=query, top_k=top_k)
    except ValidationError as exc:
        return _format_validation_error("lumena_search", exc)
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
        return _tool_error("lumena_search", exc)


@mcp.tool()
async def lumena_store(
    content: str,
    room: str,
    locus: str | None = None,
    source_type: str = "user_input",
    ctx: Context = None,
) -> str:
    """Store a memory chunk in the Lumena palace.

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
        return _format_validation_error("lumena_store", exc)
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
        return _tool_error("lumena_store", exc)


@mcp.tool()
async def lumena_assemble(query: str, top_k: int = 5, ctx: Context = None) -> str:
    """Retrieve memories and assemble a ready-to-use context string.

    Args:
        query: User message or task description.
        top_k: Max memories to retrieve.
    """
    try:
        args = AssembleInput(query=query, top_k=top_k)
    except ValidationError as exc:
        return _format_validation_error("lumena_assemble", exc)
    try:
        state = _state(ctx)
        turn = state.conversation.retrieve_and_assemble(args.query, top_k=args.top_k)
        return (
            f"Assembled context ({len(turn.retrieved_chunks)} chunks retrieved):\n\n"
            f"{turn.assembled_context}"
        )
    except Exception as exc:
        return _tool_error("lumena_assemble", exc)


@mcp.tool()
async def lumena_turn(
    user_msg: str,
    assistant_msg: str,
    room: str = "conversations",
    ctx: Context = None,
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
        return _format_validation_error("lumena_turn", exc)
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
        return _tool_error("lumena_turn", exc)


@mcp.tool()
async def lumena_feedback(
    chunk_id: int,
    was_useful: bool,
    feedback_type: str = "explicit",
    ctx: Context = None,
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
        return _format_validation_error("lumena_feedback", exc)
    try:
        state = _state(ctx)
        state.conversation.log_explicit_feedback(
            chunk_id=args.chunk_id,
            was_useful=args.was_useful,
            feedback_type=args.feedback_type,
        )
        return f"Feedback logged for chunk_id={args.chunk_id} (useful={args.was_useful})."
    except Exception as exc:
        return _tool_error("lumena_feedback", exc)


@mcp.tool()
async def lumena_status(ctx: Context = None) -> str:
    """Show current palace status: room count, active chunks, TFC state."""
    try:
        state = _state(ctx)
        room_count = state.conn.execute("SELECT COUNT(*) FROM room").fetchone()[0]
        chunk_count = state.conn.execute(
            "SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL"
        ).fetchone()[0]
        env = state.tfc.to_env()
        return (
            f"Lumena Status\n"
            f"  Device: {state.config.device}\n"
            f"  Rooms: {room_count}\n"
            f"  Active chunks: {chunk_count}\n"
            f"  Context budget: {state.config.context_budget} tokens\n"
            f"  TFC → e={env['e']:.2f} a={env['a']:.2f} tau={env['tau']:.1f} r={env['r']}\n"
            f"  Embedding model available: {state.embedding_model_available}\n"
            f"\nDashboard: http://localhost:{state.config.api_port}/dashboard\n"
            f"Run `lummenna serve` or `lumena_dashboard` for real-time effectiveness metrics."
        )
    except Exception as exc:
        return _tool_error("lumena_status", exc)


@mcp.tool()
async def lumena_dashboard(ctx: Context = None) -> str:
    """Display a real-time effectiveness dashboard: system health, locally measured benchmark results (when present), and memory lifecycle state.
    Call this when the user asks about Lumena's effectiveness, performance, or wants to see the dashboard.
    Metrics come from committed benchmark artifacts; anything not yet measured is shown as N/A.
    """
    try:
        state = _state(ctx)
        metrics = _dashboard_metrics(state)
        m = metrics

        room_lines = (
            "  " + ", ".join(f"{r['name']}({r['chunks']})" for r in m["palace"]["topology"])
            if m["palace"]["topology"]
            else "  (none)"
        )

        return (
            f"╔══════════════════════════════════════════════╗\n"
            f"║     Lumena Effectiveness Dashboard v1.0.0    ║\n"
            f"╠══════════════════════════════════════════════╣\n"
            f"║ System Health                               ║\n"
            f"╠══════════════════════════════════════════════╣\n"
            f"  Device:         {m['system']['device']}\n"
            f"  Embedder:       {m['system']['embedder']} ({'LOADED' if m['system']['embedder_loaded'] else 'MOCK'})\n"
            f"  Active Chunks:  {m['palace']['active_chunks']:,}\n"
            f"  Rooms / Loci:   {m['palace']['rooms']} / {m['palace']['loci']}\n"
            f"  Retention Rate: {m['palace']['retention_pct']}%\n"
            f"\n"
            f"  Retrieval Benchmarks (corpus: {m['retrieval']['corpus'] or 'not run'}):\n"
            f"  BM25 R@10:         {_fmt_metric(m['retrieval']['bm25_r10'])}\n"
            f"  Hybrid R@10:       {_fmt_metric(m['retrieval']['hybrid_r10_bge'])}\n"
            f"  Dense R@10:        {_fmt_metric(m['retrieval']['dense_r10_minilm'])}\n"
            f"  nDCG@10:           {_fmt_metric(m['retrieval']['ndcg10'])}\n"
            f"  p50 Latency BM25:  {_fmt_metric(m['retrieval']['latency_bm25_ms'])} ms\n"
            f"  p50 Latency Dense: {_fmt_metric(m['retrieval']['latency_dense_ms'])} ms\n"
            f"  p50 Latency Hybrid:{_fmt_metric(m['retrieval']['latency_hybrid_ms'])} ms\n"
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
            f"  L1 Time Decay:           {m['forgetting']['l1_ebbinghaus']} (horizon={m['forgetting']['half_life_days']:.0f}d)\n"
            f"  L2 Interference:         {m['forgetting']['l2_interference']}\n"
            f"  L3 Budget Eviction:      {m['forgetting']['l3_budget']}\n"
            f"  Degradation Chain:       FP32 → FP16 → INT8 → BINARY → RELEASED\n"
            f"  Current Stage:           {m['forgetting']['degradation_stage']}\n"
            f"  Forgotten Chunks:        {m['palace']['forgotten']}\n"
            f"\n"
            f"╔══════════════════════════════════════════════╗\n"
            f"║ Business Impact                             ║\n"
            f"╠══════════════════════════════════════════════╣\n"
            f"  API Cost / Query:         ${m['business']['cost_per_query_usd']:.2f}\n"
            f"  Data Sovereignty:         {m['business']['data_sovereignty_pct']}% (on-device)\n"
            f"  GDPR Native RTBF:         {'YES' if m['business']['gdpr_native_rtbf'] else 'NO'}\n"
            f"  Edge Deployable:          {'YES' if m['business']['edge_deployable'] else 'NO'} ({m['system']['device']})\n"
            f"  Feedback Satisfaction:    {m['business']['feedback_satisfaction_pct']}%\n"
            f"  Feedback Total:           {m['business']['feedback_total']}\n"
            f"\n"
            f"╔══════════════════════════════════════════════╗\n"
            f"║ Capability Highlights                      ║\n"
            f"╠══════════════════════════════════════════════╣\n"
            f"  BM25 + Vector + Graph retrieval\n"
            f"  Edge deployment (RPi5): {'YES' if m['business']['edge_deployable'] else 'NO'}\n"
            f"  Memory lifecycle: 3-layer decay/interference/eviction\n"
            f"  LangGraph checkpoint saver: built-in\n"
            f"  MCP / LangChain / FastAPI integrations\n"
            f"\n"
            f"Web dashboard: http://localhost:{state.config.api_port}/dashboard\n"
            f"Machine metrics: http://localhost:{state.config.api_port}/metrics\n"
            f"Run `lummenna serve` to start the full API + dashboard."
        )
    except Exception as exc:
        return _tool_error("lumena_dashboard", exc)


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


def _load_retrieval_benchmarks() -> dict:
    """Load committed retrieval benchmark metrics (shared loader).

    Delegates to :mod:`lummenna.bench_results` so the MCP dashboard and the
    HTTP API report identical artifact-backed numbers; unmeasured metrics
    are ``None`` — no hardcoded fallback numbers, ever.
    """
    from lummenna.bench_results import load_retrieval_benchmarks

    return load_retrieval_benchmarks()


def _dashboard_metrics(state: LumenaAppState) -> dict:
    """Build dashboard metrics dict used by resources and tools."""

    conn = state.conn
    env = state.tfc.to_env()

    room_count = conn.execute("SELECT COUNT(*) FROM room").fetchone()[0]
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL").fetchone()[0]
    locus_count = conn.execute("SELECT COUNT(*) FROM locus").fetchone()[0]
    forgotten = conn.execute("SELECT COUNT(*) FROM chunk WHERE valid_to IS NOT NULL").fetchone()[0]

    feedback_row = conn.execute(
        "SELECT COUNT(*) AS c, AVG(CASE WHEN positive = 1 THEN 1.0 ELSE 0.0 END) AS sat FROM feedback_log"
    ).fetchone()

    rooms = conn.execute(
        """
        SELECT r.name, COUNT(c.chunk_id) AS chunks
        FROM room r
        LEFT JOIN locus l ON l.room_id = r.room_id
        LEFT JOIN chunk c ON c.locus_id = l.locus_id AND c.valid_to IS NULL
        GROUP BY r.room_id
        ORDER BY chunks DESC
        """
    ).fetchall()

    stage_map = {5: "FP32", 4: "FP32", 3: "FP32", 2: "FP16", 1: "INT8", 0: "BINARY"}
    degradation_stage = stage_map.get(int(env["r"]), "FP32")

    return {
        "version": "1.0.0",
        "system": {
            "device": state.config.device,
            "embedder": state.config.embedding_model,
            "embedder_loaded": state.embedding_model_available,
        },
        "palace": {
            "rooms": room_count,
            "loci": locus_count,
            "active_chunks": chunk_count,
            "forgotten": forgotten,
            "retention_pct": round((chunk_count / max(1, chunk_count + forgotten)) * 100, 1),
            "topology": [{"name": r["name"], "chunks": r["chunks"]} for r in rooms],
        },
        "tfc": {
            "e": env["e"],
            "a": env["a"],
            "tau": env["tau"],
            "r": env["r"],
            "degradation_stage": degradation_stage,
        },
        "retrieval": _load_retrieval_benchmarks(),
        "business": {
            "cost_per_query_usd": 0.0,
            "data_sovereignty_pct": 100,
            "gdpr_native_rtbf": True,
            "edge_deployable": True,
            "feedback_total": feedback_row["c"] or 0,
            "feedback_satisfaction_pct": round((feedback_row["sat"] or 0) * 100, 1),
        },
        "forgetting": {
            "l1_ebbinghaus": "ACTIVE",
            "l2_interference": "ACTIVE",
            "l3_budget": "ACTIVE",
            "half_life_days": env["tau"],
            "degradation_stage": degradation_stage,
        },
        "design_properties": {
            "bm25": "Lumena uses SQLite FTS5; standard BM25",
            "dense": "Cosine similarity via sqlite-vec or brute-force",
            "hybrid": "RRF fusion; no external-system comparison has been run",
            "edge": "Designed for edge devices; RAM footprint not yet benchmarked (perf suite has no results)",
            "memory_lifecycle": "3-layer decay/interference/eviction",
        },
    }


@mcp.resource("dashboard://lummenna/effectiveness")
async def dashboard_effectiveness(ctx: Context = None) -> str:
    """Lumena memory store effectiveness overview.
    Shows real-time system health, retrieval metrics (if benchmarks available), TFC state, and business impact.
    """
    try:
        state = _state(ctx)
        return json.dumps(_dashboard_metrics(state), indent=2, default=str)
    except Exception as exc:
        return _tool_error("dashboard_effectiveness", exc)


@mcp.resource("dashboard://lummenna/capabilities")
async def dashboard_capabilities(ctx: Context = None) -> str:
    """Lumena capability highlights (qualitative design properties).
    Includes locally measured retrieval metrics when benchmark artifacts exist;
    contains no comparisons to external systems.
    """
    try:
        state = _state(ctx)
        metrics = _dashboard_metrics(state)
        return json.dumps(
            {
                "design_properties": metrics["design_properties"],
                "retrieval": metrics["retrieval"],
                "business": metrics["business"],
            },
            indent=2,
            default=str,
        )
    except Exception as exc:
        return _tool_error("dashboard_capabilities", exc)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    if not _MCP_AVAILABLE:
        print(
            "ERROR: The 'mcp' package is required for the MCP server. "
            "Install with: pip install lummenna[mcp]",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        mcp.run(transport="stdio")
    except Exception as exc:
        logger.error("mcp_server_fatal_error", exc=str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
