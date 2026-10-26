"""C9: CLI Implementation (Typer + Rich).

All commands execute real business logic.
"""

import asyncio
import os
import signal
import sqlite3
import sys
import time
from importlib import metadata
from typing import cast, get_args

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from lummenna.brand.errors import LicenseRequiredError, ModelNotAvailableError
from lummenna.cli.models import model_app
from lummenna.compliance.safety_forgetting import get_recent_audit_events
from lummenna.config import DeviceProfile, LummennaConfig
from lummenna.controller import TwinForceController
from lummenna.data.schema import ensure_schema, get_connection
from lummenna.force.contextual.embed import get_embedder
from lummenna.force.mnemonic.retrieval_graph import GraphChannel
from lummenna.force.mnemonic.store import store_memory
from lummenna.illuminate import run_onboarding_wizard
from lummenna.search import SearchPipeline

app = typer.Typer(name="lummenna", help="Twin-force memory and context framework")
memory_app = typer.Typer(name="memory", help="Memory operations")
palace_app = typer.Typer(name="palace", help="Palace topology operations")
tfc_app = typer.Typer(name="tfc", help="Twin-Force Controller operations")
compliance_app = typer.Typer(name="compliance", help="Compliance operations")
daemon_app = typer.Typer(name="daemon", help="Background scheduler")
p2p_app = typer.Typer(name="p2p", help="P2P memory sharing")

app.add_typer(memory_app)
app.add_typer(palace_app)
app.add_typer(tfc_app)
app.add_typer(compliance_app)
app.add_typer(model_app)
app.add_typer(daemon_app)
app.add_typer(p2p_app)


def _assert_pro_or_exit(feature: str, config: "LummennaConfig | None" = None) -> None:
    """Gate a Pro-only CLI command, exiting cleanly (no traceback) if unlicensed.

    Mirrors ``LummennaConfig.assert_licensed`` but renders a friendly Rich message
    and a non-zero exit code instead of surfacing a stack trace to the user.
    """
    cfg = config if config is not None else LummennaConfig()
    try:
        cfg.assert_licensed(feature)
    except LicenseRequiredError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(2) from exc


def _validate_device(device: str) -> DeviceProfile:
    """Validate a free-text ``--device`` value against the supported profiles.

    Returns the value narrowed to the ``DeviceProfile`` literal, or exits with a
    clear error listing the valid choices. This keeps the CLI boundary
    type-correct (LummennaConfig.device is a Literal) and rejects typos loudly
    rather than silently falling back to the generic profile.
    """
    valid = get_args(DeviceProfile)
    if device not in valid:
        console.print(f"[red]Invalid --device '{device}'. Choose one of: {', '.join(valid)}.[/red]")
        raise typer.Exit(2)
    return cast(DeviceProfile, device)


# ---------------------------------------------------------------------------
# API server command
# ---------------------------------------------------------------------------


@app.command(name="serve")
def serve(
    # Loopback by default: the memory API must not be exposed to the LAN
    # unless explicitly requested (sovereign, local-first posture).
    host: str = typer.Option("127.0.0.1", "--host", "-h"),
    port: int = typer.Option(8848, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload"),
    log_level: str = typer.Option("info", "--log-level"),
):
    """Start the FastAPI memory server."""
    import uvicorn

    config = LummennaConfig()
    host = host or config.api_host
    port = port or config.api_port
    log_level = log_level or config.log_level
    console.print(f"[bold green]Starting Lummenna API server on {host}:{port}[/bold green]")
    uvicorn.run("lummenna.api.server:app", host=host, port=port, log_level=log_level, reload=reload)


console = Console()

_state_tfc = TwinForceController()


def _ensure_conn(config: LummennaConfig):
    return get_connection(config)


@app.command()
def init(
    device: str = typer.Option("generic", "--device", "-d"),
    download_model: bool = typer.Option(
        False, "--download-model", help="Attempt to download the default embedding model"
    ),
):
    """Initialize Lummenna palace for the given device profile."""
    device_literal = _validate_device(device)
    config = LummennaConfig(device=device_literal)
    config.store_path.mkdir(parents=True, exist_ok=True)
    config.model_path.mkdir(parents=True, exist_ok=True)
    conn = get_connection(config)
    ensure_schema(conn)
    # Write default config TOML
    config_toml = config.store_path.parent / "config.toml"
    config_toml.write_text(
        f'device = "{device}"\ncontext_budget = {config.context_budget}\n'
        f'memory_limit_mb = {config.memory_limit_mb}\nvector_index = "{config.vector_index}"\n'
    )
    console.print(f"[bold green]Lummenna initialised for device: {device}[/bold green]")
    console.print(f"Store path: {config.store_path}")
    console.print(f"DB path: {config.db_path}")

    if download_model:
        model_dir = config.model_path / config.embedding_model
        if not model_dir.exists():
            from lummenna.cli.models import provision_embedding_model

            try:
                provision_embedding_model(config.embedding_model, config.model_path, config=config)
                console.print(
                    f"[bold green]Model '{config.embedding_model}' downloaded.[/bold green]"
                )
            except Exception as exc:
                console.print(f"[yellow]Model download failed: {exc}[/yellow]")
                console.print(
                    "[yellow]You can export an ONNX model manually with:[/yellow]\n"
                    "  optimum-cli export onnx --model BAAI/bge-small-en-v1.5 ~/.lummenna/models/bge-small-en-v1.5"
                )
        else:
            console.print(f"[green]Model already exists at {model_dir}[/green]")


@app.command(name="illuminate")
def illuminate():
    """Run the Palace Construction onboarding wizard."""
    config = LummennaConfig()
    conn = _ensure_conn(config)
    run_onboarding_wizard(conn)


@app.command()
def status():
    """Show current palace and TFC status."""
    config = LummennaConfig()
    conn = _ensure_conn(config)
    room_count = conn.execute("SELECT COUNT(*) FROM room").fetchone()[0]
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL").fetchone()[0]
    console.print("[bold]Lummenna Status[/bold]")
    console.print(f"Device: {config.device}")
    console.print(f"Rooms: {room_count}")
    console.print(f"Active chunks: {chunk_count}")
    console.print(f"Context budget: {config.context_budget} tokens")
    env = _state_tfc.to_env()
    console.print(f"TFC → e={env['e']:.2f} a={env['a']:.2f} tau={env['tau']:.1f} r={env['r']}")


# ---------------------------------------------------------------------------
# MCP server command
# ---------------------------------------------------------------------------


def _mcp_preflight() -> int:
    """Diagnose an MCP deployment without starting the protocol loop.

    Returns a process exit code: 0 when the server would start and serve a real
    embedder, 1 otherwise. Each check drives the same code path the server uses
    at startup (``initialize_palace``, the live FastMCP registry) rather than
    re-deriving the answer, so a pass here means the server starts.

    This exists because the usual failure is silent. An MCP client spawns the
    server as a subprocess and, when startup fails or exceeds the client's
    handshake timeout, most clients simply omit the tools with no error shown
    to the user. Reading the failure directly is the only fast way out.
    """
    from lummenna._init import initialize_palace
    from lummenna.integrations.mcp_server import _MCP_AVAILABLE, _MCP_ERROR
    from lummenna.integrations.mcp_server import mcp as mcp_server

    table = Table(title="Lummenna MCP preflight")
    table.add_column("Check", style="cyan")
    table.add_column("Result")
    table.add_column("Detail", style="green")

    def _row(name: str, passed: bool, detail: str) -> None:
        # Details carry filesystem paths and exception text, which legitimately
        # contain square brackets. Rich would parse those as console markup and
        # silently delete them: "pip install 'lummenna[mcp]'" renders as
        # "pip install 'lummenna'", advice that reproduces the very failure
        # being reported. Only the Result column is intentional markup.
        table.add_row(name, "[green]PASS[/green]" if passed else "[red]FAIL[/red]", escape(detail))

    def _finish(ok: bool) -> int:
        """Render the table and the verdict. Every exit goes through here."""
        console.print(table)
        if ok:
            console.print("[bold green]MCP server is ready to serve.[/bold green]")
        else:
            console.print(
                "[bold red]MCP server is not ready. Resolve the failures above.[/bold red]"
            )
        return 0 if ok else 1

    def _fail(name: str, detail: str) -> int:
        """Record a fatal check and stop: later checks would be meaningless."""
        _row(name, False, detail)
        return _finish(False)

    # 1. The optional dependency. Without it the module falls back to
    #    _UnavailableMCP and no later check means anything, so stop here.
    if not _MCP_AVAILABLE:
        return _fail(
            "mcp package",
            f"{_MCP_ERROR}. Install with: pip install 'lummenna[mcp]'",
        )
    try:
        mcp_version = metadata.version("mcp")
    except metadata.PackageNotFoundError:
        # Importable but not installed as a distribution (e.g. vendored or on
        # PYTHONPATH). Importability is what the server actually requires.
        mcp_version = "unknown version"
    _row("mcp package", True, f"mcp {mcp_version}")

    # 2. Writable store and model directories, created exactly as app_lifespan
    #    creates them.
    config = LummennaConfig()
    try:
        config.store_path.mkdir(parents=True, exist_ok=True)
        config.model_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _fail("store paths", f"cannot create {config.store_path}: {exc}")
    _row("store paths", True, f"store={config.store_path} model={config.model_path}")

    # 3. Schema + embedder, via the server's own bootstrap.
    try:
        palace = initialize_palace(config)
    except ModelNotAvailableError as exc:
        return _fail("embedding model", str(exc))
    except sqlite3.Error as exc:
        return _fail("palace database", f"{config.db_path}: {exc}")

    ok = True
    try:
        _row("palace database", True, str(config.db_path))
        if palace["embedding_model_available"]:
            _row("embedding model", True, config.embedding_model)
        else:
            # initialize_palace only reaches here with the mock opt-in set.
            # The server would start, but every semantic result would be noise,
            # so this is reported as a failure rather than a warning.
            ok = False
            _row(
                "embedding model",
                False,
                "mock embedder active via LUMMENNA_ALLOW_MOCK_EMBEDDER; "
                "semantic search is meaningless. Provision with: "
                f"lummenna model download {config.embedding_model}",
            )
    finally:
        palace["conn"].close()

    # 4. The surface a client will actually see.
    #
    # Past the _MCP_AVAILABLE guard above, the module-level object is a real
    # FastMCP rather than the _UnavailableMCP stand-in. mypy cannot narrow a
    # module-level union through a boolean flag, so the invariant is stated
    # here instead of giving _UnavailableMCP unreachable stub methods.
    from mcp.server.fastmcp import FastMCP

    server = cast(FastMCP, mcp_server)

    async def _surface() -> tuple[list[str], list[str]]:
        tools = sorted(t.name for t in await server.list_tools())
        resources = sorted(str(r.uri) for r in await server.list_resources())
        return tools, resources

    tool_names, resource_uris = asyncio.run(_surface())
    if not tool_names:
        ok = False
    if not resource_uris:
        ok = False
    _row("tools", bool(tool_names), f"{len(tool_names)}: {', '.join(tool_names) or 'none'}")
    _row(
        "resources",
        bool(resource_uris),
        f"{len(resource_uris)}: {', '.join(resource_uris) or 'none'}",
    )

    return _finish(ok)


@app.command(name="mcp")
def mcp_command(
    check: bool = typer.Option(
        False,
        "--check",
        help="Run preflight diagnostics and exit, instead of serving.",
    ),
    transport: str = typer.Option(
        "stdio",
        "--transport",
        help="stdio (one process per client) or streamable-http (one shared process).",
    ),
    host: str | None = typer.Option(
        None, "--host", help="Bind address for http transports. Defaults to api_host."
    ),
    port: int | None = typer.Option(
        None, "--port", help="Port for http transports. Defaults to api_port + 1."
    ),
) -> None:
    """Serve the Model Context Protocol server.

    Default (stdio) is meant to be spawned by an MCP client rather than run by
    hand, as the `command` in its config:

        {"command": ["lummenna", "mcp"]}

    The protocol owns stdout, so this command writes nothing there; logs go to
    stderr. `--check` is the exception: it prints a diagnostic table to stdout
    and exits without ever starting the protocol loop.

    For several editor sessions at once, serve one shared process instead:

        lummenna mcp --transport streamable-http

    and point every client at the printed URL. Each stdio server loads its own
    embedder and holds its own connection (~750 MB resident), so N sessions
    cost N times that against a single shared database. One HTTP server costs
    it once.
    """
    if check:
        raise typer.Exit(_mcp_preflight())

    from lummenna.integrations.mcp_server import main as run_mcp_server

    run_mcp_server(transport=transport, host=host, port=port)


@memory_app.command(name="store")
def memory_store(
    content: str = typer.Argument(..., help="Content to store"),
    room: str = typer.Option(..., "--room", "-r"),
    locus: str = typer.Option(None, "--locus", "-l"),
    allow_mock: bool = typer.Option(
        False,
        "--allow-mock",
        help="Allow deterministic mock embedder if model missing (testing only)",
    ),
):
    """Store a memory through the full pipeline."""
    config = LummennaConfig()
    conn = _ensure_conn(config)
    try:
        embedder = get_embedder(config, allow_mock=allow_mock)
    except ModelNotAvailableError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    embedding = embedder.encode_single(content)
    chunk_id = store_memory(
        conn, content, room_name=room, locus_name=locus, embedding=embedding, config=config
    )
    console.print(f"[bold green]Stored chunk_id={chunk_id} in room '{room}'[/bold green]")


@memory_app.command(name="retrieve")
def memory_retrieve(
    query: str = typer.Argument(..., help="Query to retrieve memories for"),
    top_k: int = typer.Option(5, "--top-k", "-k"),
    allow_mock: bool = typer.Option(
        False,
        "--allow-mock",
        help="Allow deterministic mock embedder if model missing (testing only)",
    ),
):
    """Run search pipeline and print top results."""
    config = LummennaConfig()
    conn = _ensure_conn(config)
    try:
        embedder = get_embedder(config, allow_mock=allow_mock)
    except ModelNotAvailableError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    graph = GraphChannel(conn)
    pipeline = SearchPipeline(conn, config, tfc=_state_tfc, embedder=embedder, graph=graph)
    results = pipeline.execute(query, k=max(top_k * 2, 10))
    if not results:
        console.print("[yellow]No memories found.[/yellow]")
        return
    table = Table(title="Retrieved Memories")
    table.add_column("Rank", style="cyan")
    table.add_column("Room", style="magenta")
    table.add_column("Locus", style="green")
    table.add_column("Score", justify="right")
    table.add_column("Content", max_width=60)
    for rank, rc in enumerate(results[:top_k], 1):
        table.add_row(
            str(rank), rc.room_name, rc.locus_name, f"{rc.final_score:.3f}", rc.content[:200]
        )
    console.print(table)


@palace_app.command(name="rooms")
def palace_rooms():
    """List all rooms."""
    config = LummennaConfig()
    conn = _ensure_conn(config)
    rows = conn.execute("SELECT room_id, name, room_type FROM room ORDER BY name").fetchall()
    table = Table(title="Palace Rooms")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="magenta")
    table.add_column("Type", style="green")
    for r in rows:
        table.add_row(str(r[0]), r[1], r[2] or "domain")
    console.print(table)


@palace_app.command(name="loci")
def palace_loci(room: str = typer.Argument(..., help="Room name")):
    """List loci in a room."""
    config = LummennaConfig()
    conn = _ensure_conn(config)
    row = conn.execute("SELECT room_id FROM room WHERE name = ?", (room,)).fetchone()
    if not row:
        console.print(f"[red]Room '{room}' not found.[/red]")
        raise typer.Exit(1)
    room_id = row[0]
    rows = conn.execute(
        "SELECT locus_id, name, description FROM locus WHERE room_id = ? ORDER BY name", (room_id,)
    ).fetchall()
    table = Table(title=f"Loci in '{room}'")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="magenta")
    table.add_column("Description", style="green")
    for r in rows:
        table.add_row(str(r[0]), r[1], r[2] or "")
    console.print(table)


@tfc_app.command(name="show")
def tfc_show():
    """Print TFC state."""
    env = _state_tfc.to_env()
    console.print(f"e (mnemonic bias)    = {env['e']:.2f}")
    console.print(f"a (attention temp)   = {env['a']:.2f}")
    console.print(f"tau (temporal horiz) = {env['tau']:.1f} days")
    console.print(f"r (resolution level) = {env['r']}")


@tfc_app.command(name="set")
def tfc_set(
    e: float = typer.Option(None, "--e"),
    a: float = typer.Option(None, "--a"),
    tau: float = typer.Option(None, "--tau"),
    r: int = typer.Option(None, "--r"),
):
    """Manually override TFC state variables."""
    if e is not None:
        _state_tfc.state.e = max(0.0, min(1.0, e))
    if a is not None:
        _state_tfc.state.a = max(0.0, min(1.0, a))
    if tau is not None:
        _state_tfc.state.tau = tau
    if r is not None:
        _state_tfc.state.r = max(0, min(5, r))
    console.print("[bold green]TFC state updated.[/bold green]")
    tfc_show()


@compliance_app.command(name="audit")
def compliance_audit(n: int = typer.Option(10, "--n")):
    """Show last N compliance events from the SQLite audit log."""
    import datetime
    import json as _json

    # Pro gate: audit-trail export is a commercial compliance feature.
    # Basic regex PII detection on write stays free (Community).
    _assert_pro_or_exit("compliance")

    events = get_recent_audit_events(n)
    if not events:
        console.print("[yellow]No compliance events found.[/yellow]")
        return
    table = Table(title="Compliance Audit Log")
    table.add_column("Timestamp", style="cyan")
    table.add_column("Event", style="magenta")
    table.add_column("Action", style="blue")
    table.add_column("Resource", style="green")
    table.add_column("Detail", style="yellow")
    for ev in events:
        ts = ev.get("created_at")
        ts_str = (
            datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
            if isinstance(ts, int | float)
            else str(ts or "")
        )
        detail = ""
        raw_meta = ev.get("metadata_json")
        if raw_meta:
            try:
                meta = _json.loads(raw_meta)
                detail = meta.get("reason") or _json.dumps(meta)[:60]
            except (ValueError, TypeError):
                detail = str(raw_meta)[:60]
        resource = f"{ev.get('resource_type') or ''}:{ev.get('resource_id') or ''}"
        table.add_row(
            ts_str, str(ev.get("event_type") or ""), str(ev.get("action") or ""), resource, detail
        )
    console.print(table)


@compliance_app.command(name="forget")
def compliance_forget(
    chunk_id: int = typer.Argument(..., help="Chunk ID to permanently forget"),
    reason: str = typer.Option(..., "--reason", help="Reason recorded in the audit log"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
):
    """Permanently forget a chunk (right-to-be-forgotten).

    Writes an audit event, removes the chunk from the FTS index, purges its
    provenance chain, and overwrites its content with ``[REDACTED]``.
    """
    from lummenna.compliance.safety_forgetting import safety_forget_chunk

    config = LummennaConfig()
    # Pro gate: right-to-be-forgotten (RTBF) is a commercial compliance feature.
    _assert_pro_or_exit("compliance", config)
    conn = get_connection(config)
    try:
        row = conn.execute(
            "SELECT chunk_id, valid_to FROM chunk WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        if row is None:
            console.print(f"[red]Chunk {chunk_id} does not exist.[/red]")
            raise typer.Exit(1)
        if not yes and not typer.confirm(
            f"Permanently forget chunk {chunk_id}? This cannot be undone."
        ):
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(0)
        safety_forget_chunk(conn, chunk_id, reason)
        conn.commit()
        console.print(f"[bold green]Chunk {chunk_id} forgotten (reason: {reason}).[/bold green]")
    finally:
        conn.close()


@daemon_app.command(name="start")
def daemon_start():
    """Start the SleepScheduler daemon in the foreground."""
    from pathlib import Path

    from lummenna.sleep import SleepScheduler

    config = LummennaConfig()
    scheduler = SleepScheduler(config)
    scheduler.start()

    pid_file = Path.home() / ".lummenna" / "lummenna_daemon.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    console.print("[bold green]Daemon started. Press Ctrl+C to stop.[/bold green]")

    def _signal_handler(sig, frame):  # noqa: ARG001
        console.print("\n[bold yellow]Shutting down daemon...[/bold yellow]")
        scheduler.stop()
        pid_file.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    while True:
        time.sleep(1)


@daemon_app.command(name="run-once")
def daemon_run_once():
    """Run a single consolidation pass immediately."""
    from lummenna.sleep import SleepScheduler

    config = LummennaConfig()
    scheduler = SleepScheduler(config)
    scheduler.run_now()
    console.print("[bold green]Consolidation pass completed.[/bold green]")


@daemon_app.command(name="status")
def daemon_status():
    """Check if a Lummenna daemon is running (checks PID file)."""
    from pathlib import Path

    pid_file = Path.home() / ".lummenna" / "lummenna_daemon.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            import os

            try:
                os.kill(pid, 0)
                console.print(f"[bold green]Daemon is running (PID {pid}).[/bold green]")
                return
            except OSError:
                console.print("[yellow]Stale PID file found. Daemon is not running.[/yellow]")
                pid_file.unlink(missing_ok=True)
                return
        except (ValueError, FileNotFoundError):
            pass
    console.print("[yellow]Daemon is not running.[/yellow]")


@p2p_app.command(name="share")
def p2p_share(
    room: str = typer.Option(..., "--room", "-r"),
    ttl: int = typer.Option(24, "--ttl"),
):
    """Share a room to discovered peers (requires LUMMENNA_SOVEREIGN=false)."""
    from lummenna.p2p.beam import BeamNode

    config = LummennaConfig()
    node = BeamNode(config)

    async def _share():
        await node.start()
        try:
            await node.share_room(room, ttl)
            await asyncio.sleep(0.5)
        finally:
            await node.stop()

    asyncio.run(_share())
    console.print(f"[bold green]Shared room '{room}' with {len(node.peers)} peer(s)[/bold green]")


@p2p_app.command(name="discover")
def p2p_discover(
    timeout: int = typer.Option(3, "--timeout", "-t"),
):
    """Discover peers on the local network."""
    from lummenna.p2p.beam import BeamNode

    config = LummennaConfig()
    node = BeamNode(config)

    async def _discover():
        await node.start()
        try:
            await asyncio.sleep(timeout)
            return list(node.peers.items())
        finally:
            await node.stop()

    peers = asyncio.run(_discover())
    if not peers:
        console.print("[yellow]No beam peers discovered.[/yellow]")
        return
    table = Table(title="Discovered Beam Peers")
    table.add_column("Name", style="magenta")
    table.add_column("Address", style="green")
    table.add_column("Port", justify="right")
    for name, (host, port) in peers:
        table.add_row(name, host, str(port))
    console.print(table)


if __name__ == "__main__":
    app()
