"""C9: CLI Implementation (Typer + Rich).

All commands execute real business logic.
"""

import asyncio
import os
import signal
import sys
import time

import typer
from rich.console import Console
from rich.table import Table

from lumena.brand.errors import ModelNotAvailableError
from lumena.cli.models import model_app
from lumena.compliance.safety_forgetting import get_recent_audit_events
from lumena.config import LumenaConfig
from lumena.controller import TwinForceController
from lumena.data.schema import ensure_schema, get_connection
from lumena.force.contextual.embed import get_embedder
from lumena.force.mnemonic.retrieval_graph import GraphChannel
from lumena.force.mnemonic.store import store_memory
from lumena.illuminate import run_onboarding_wizard
from lumena.search import SearchPipeline

app = typer.Typer(name="lumena", help="Twin-force memory and context framework")
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

    config = LumenaConfig()
    host = host or config.api_host
    port = port or config.api_port
    log_level = log_level or config.log_level
    console.print(f"[bold green]Starting Lumena API server on {host}:{port}[/bold green]")
    uvicorn.run("lumena.api.server:app", host=host, port=port, log_level=log_level, reload=reload)


console = Console()

_state_tfc = TwinForceController()


def _ensure_conn(config: LumenaConfig):
    return get_connection(config)


@app.command()
def init(
    device: str = typer.Option("generic", "--device", "-d"),
    download_model: bool = typer.Option(
        False, "--download-model", help="Attempt to download the default embedding model"
    ),
):
    """Initialize Lumena palace for the given device profile."""
    config = LumenaConfig(device=device)
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
    console.print(f"[bold green]Lumena initialised for device: {device}[/bold green]")
    console.print(f"Store path: {config.store_path}")
    console.print(f"DB path: {config.db_path}")

    if download_model:
        model_dir = config.model_path / config.embedding_model
        if not model_dir.exists():
            from lumena.cli.models import provision_embedding_model

            try:
                provision_embedding_model(config.embedding_model, config.model_path, config=config)
                console.print(
                    f"[bold green]Model '{config.embedding_model}' downloaded.[/bold green]"
                )
            except Exception as exc:
                console.print(f"[yellow]Model download failed: {exc}[/yellow]")
                console.print(
                    "[yellow]You can export an ONNX model manually with:[/yellow]\n"
                    "  optimum-cli export onnx --model BAAI/bge-small-en-v1.5 ~/.lumena/models/bge-small-en-v1.5"
                )
        else:
            console.print(f"[green]Model already exists at {model_dir}[/green]")


@app.command(name="illuminate")
def illuminate():
    """Run the Palace Construction onboarding wizard."""
    config = LumenaConfig()
    conn = _ensure_conn(config)
    run_onboarding_wizard(conn)


@app.command()
def status():
    """Show current palace and TFC status."""
    config = LumenaConfig()
    conn = _ensure_conn(config)
    room_count = conn.execute("SELECT COUNT(*) FROM room").fetchone()[0]
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL").fetchone()[0]
    console.print("[bold]Lumena Status[/bold]")
    console.print(f"Device: {config.device}")
    console.print(f"Rooms: {room_count}")
    console.print(f"Active chunks: {chunk_count}")
    console.print(f"Context budget: {config.context_budget} tokens")
    env = _state_tfc.to_env()
    console.print(f"TFC → e={env['e']:.2f} a={env['a']:.2f} tau={env['tau']:.1f} r={env['r']}")


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
    config = LumenaConfig()
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
    config = LumenaConfig()
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
    config = LumenaConfig()
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
    config = LumenaConfig()
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
            if isinstance(ts, (int, float))
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
    from lumena.compliance.safety_forgetting import safety_forget_chunk

    config = LumenaConfig()
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

    from lumena.sleep import SleepScheduler

    config = LumenaConfig()
    scheduler = SleepScheduler(config)
    scheduler.start()

    pid_file = Path.home() / ".lumena" / "lumena_daemon.pid"
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
    from lumena.sleep import SleepScheduler

    config = LumenaConfig()
    scheduler = SleepScheduler(config)
    scheduler.run_now()
    console.print("[bold green]Consolidation pass completed.[/bold green]")


@daemon_app.command(name="status")
def daemon_status():
    """Check if a Lumena daemon is running (checks PID file)."""
    from pathlib import Path

    pid_file = Path.home() / ".lumena" / "lumena_daemon.pid"
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
    """Share a room to discovered peers (requires LUMENA_SOVEREIGN=false)."""
    from lumena.p2p.beam import BeamNode

    config = LumenaConfig()
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
    from lumena.p2p.beam import BeamNode

    config = LumenaConfig()
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
