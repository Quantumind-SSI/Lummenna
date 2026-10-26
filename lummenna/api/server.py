"""FastAPI server exposing Lummenna memory operations.

Endpoints:
  GET  /                 — canonical memory observatory (HTML)
  GET  /health           — liveness probe
  GET  /dashboard        — stable observatory alias (HTML)
  GET  /docs             — local OpenAPI reference (HTML)
  GET  /metrics          — machine-readable metrics
  GET  /v1/status        — palace overview
  POST /v1/search       — semantic + lexical hybrid search
  POST /v1/store        — store a memory chunk
  POST /v1/feedback     — log explicit or implicit feedback
  POST /v1/assemble     — retrieve + assemble context in one call
  POST /v1/turn         — store full conversation turn
  GET  /v1/dashboard-data — dashboard JSON data
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets as _secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from lummenna import __version__
from lummenna._init import initialize_palace
from lummenna.audit import log_audit_event
from lummenna.brand.errors import LicenseError, TenantQuotaError
from lummenna.config import LummennaConfig
from lummenna.controller import TwinForceController
from lummenna.conversation import ConversationMemory
from lummenna.force.mnemonic.forgetting_l3_budget import BYTES_PER_CHUNK_ESTIMATE
from lummenna.fusion import RetrievedChunk
from lummenna.logging import get_console_logger
from lummenna.observability import normalize_route
from lummenna.observability import registry as _metrics
from lummenna.search import SearchPipeline

logger = get_console_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_config = LummennaConfig()
_config.store_path.mkdir(parents=True, exist_ok=True)
_config.model_path.mkdir(parents=True, exist_ok=True)

limiter = Limiter(key_func=get_remote_address, default_limits=[_config.api_rate_limit])

app = FastAPI(
    title="Lummenna Memory API",
    version=__version__,
    description=(
        "Local-first memory API. When LUMMENNA_API_KEY is configured, send it "
        "in X-API-Key for every /v1 operation. X-Tenant-ID selects the tenant scope."
    ),
    docs_url=None,
    redoc_url=None,
)


def _custom_openapi() -> dict:
    """Add middleware-enforced API-key and tenant headers to OpenAPI."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes["ApiKeyAuth"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": "Required for /v1 operations when LUMMENNA_API_KEY is configured.",
    }
    tenant_parameter = {
        "name": "X-Tenant-ID",
        "in": "header",
        "required": False,
        "schema": {"type": "string", "default": "default", "maxLength": 128},
        "description": "Tenant scope. Non-default tenants require the multi_tenant entitlement.",
    }
    for path, path_item in schema.get("paths", {}).items():
        if not path.startswith("/v1/") or not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            operation["security"] = [{"ApiKeyAuth": []}]
            parameters = operation.setdefault("parameters", [])
            if not any(parameter.get("name") == "X-Tenant-ID" for parameter in parameters):
                parameters.append(dict(tenant_parameter))
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi  # type: ignore[method-assign]

v1_router = APIRouter(prefix="/v1")

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

# Clamp CORS to localhost origins when sovereign mode is enabled
_allowed_origins = [o.strip() for o in _config.allowed_origins.split(",")]
if _config.sovereign:
    _allowed_origins = ["http://localhost", "http://localhost:8000", "http://localhost:8848"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key", "X-Tenant-ID", "X-Request-ID"],
)

# Module-level state (single instance for pilot)
_state: dict = {}


def _get_pool():
    """Return the connection pool, or None before lifespan startup."""
    return _state.get("pool")


def _get_config() -> LummennaConfig:
    return _state.get("config", _config)  # type: ignore[no-any-return]


def _get_conn() -> sqlite3.Connection | None:
    """Return the shared administrative connection (probes, gauges, dashboard).

    Request handlers must NOT use this: they check connections out of the pool
    so concurrent requests never share one connection (and its implicit
    transaction state). The admin connection exists only for single-threaded
    operational reads (/health, /ready, /metrics, /dashboard-data).
    """
    return _state.get("conn")


# Simple in-memory brute-force tracker for failed API-key attempts.
# Maps remote_address -> (failure_count, first_failure_timestamp)
_auth_failures: dict[str, tuple[int, float]] = {}
_AUTH_FAILURES_MAX = 10_000  # bound memory against scanning floods


def _check_brute_force(remote_addr: str) -> bool:
    """Return True if the remote address is currently throttled."""
    now = time.time()
    count, first_ts = _auth_failures.get(remote_addr, (0, now))
    if now - first_ts > 60:
        # Window expired; reset
        _auth_failures[remote_addr] = (0, now)
        return False
    return count >= 5


def _record_auth_failure(remote_addr: str) -> None:
    """Record a failed auth attempt with bounded memory.

    When the tracker is full, expired windows are evicted first; if every
    window is still live, the oldest entry is dropped. An attacker rotating
    source addresses can therefore not grow this dict without bound.
    """
    now = time.time()
    if remote_addr not in _auth_failures and len(_auth_failures) >= _AUTH_FAILURES_MAX:
        expired = [k for k, (_, ts) in _auth_failures.items() if now - ts > 60]
        for k in expired:
            _auth_failures.pop(k, None)
        if len(_auth_failures) >= _AUTH_FAILURES_MAX:
            oldest = min(_auth_failures.items(), key=lambda kv: kv[1][1])[0]
            _auth_failures.pop(oldest, None)
    count, first_ts = _auth_failures.get(remote_addr, (0, now))
    _auth_failures[remote_addr] = (count + 1, first_ts)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
async def _auth_middleware(request: Request, call_next):
    public_paths = (
        "/",
        "/health",
        "/ready",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/dashboard",
        "/favicon.svg",
    )
    # NOTE: /metrics and /metrics/prometheus are deliberately NOT public.
    # They disclose version, edition, device, encryption mode, sovereign and
    # backup posture, and store size — recon-grade data for an internet-facing
    # deployment. Scrapers authenticate with the same X-API-Key as /v1
    # (Prometheus scrape_configs support Authorization/secret headers), or
    # the endpoint is bound behind a network policy to the scraper only.
    request_id = str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    tenant_id = (
        request.headers.get("X-Tenant-ID") or request.query_params.get("tenant_id") or "default"
    )
    request.state.tenant_id = tenant_id

    # Allow CORS preflight without authentication
    if request.method == "OPTIONS":
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    remote_addr = get_remote_address(request)

    if _config.api_key and request.url.path not in public_paths:
        provided = request.headers.get("X-API-Key")
        if not provided:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing API key"},
                headers={"X-Request-ID": request_id},
            )
        if not _secrets.compare_digest(provided, _config.api_key):
            if _check_brute_force(remote_addr):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many failed authentication attempts"},
                    headers={"X-Request-ID": request_id},
                )
            _record_auth_failure(remote_addr)
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid API key"},
                headers={"X-Request-ID": request_id},
            )
        # Successful auth: reset failures
        _auth_failures.pop(remote_addr, None)

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


app.middleware("http")(_auth_middleware)


async def _metrics_middleware(request: Request, call_next):
    """Record request count and latency per route template/method/status class.

    Route templates are bounded by :func:`normalize_route`, so label
    cardinality cannot explode. Failures inside handlers still record a 5xx.
    """
    route = normalize_route(request.url.path)
    start = time.perf_counter()
    status = "500"
    try:
        response = await call_next(request)
        status = str(response.status_code)
        return response
    finally:
        elapsed = time.perf_counter() - start
        labels = {
            "route": route,
            "method": request.method,
            "status": status[0] + "xx" if status else "5xx",
        }
        _metrics.inc("lummenna_http_requests_total", labels)
        _metrics.observe("lummenna_http_request_duration_seconds", elapsed, labels)


app.middleware("http")(_metrics_middleware)


class _SecurityHeadersMiddleware:
    """Add security headers to all responses; Cache-Control no-store for API paths."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        async def _send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append(
                    (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
                )
                headers.append((b"x-content-type-options", b"nosniff"))
                headers.append((b"x-frame-options", b"DENY"))
                headers.append(
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'",
                    )
                )
                headers.append((b"referrer-policy", b"strict-origin-when-cross-origin"))
                headers.append((b"permissions-policy", b"geolocation=(), microphone=(), camera=()"))
                if path.startswith("/v1/"):
                    headers.append((b"cache-control", b"no-store, max-age=0, must-revalidate"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, _send_with_headers)


app.add_middleware(_SecurityHeadersMiddleware)


class _SizeLimitMiddleware:
    def __init__(self, app, max_bytes: int = 1_048_576):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            content_length = 0
            for name, value in scope.get("headers", []):
                if name == b"content-length":
                    content_length = int(value)
                    break
            if content_length > self.max_bytes:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"detail":"Request body too large"}',
                    }
                )
                return
        await self.app(scope, receive, send)


app.add_middleware(_SizeLimitMiddleware, max_bytes=_config.request_max_size_bytes)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="Search query string")
    top_k: int = Field(5, ge=1, le=50)
    tenant_id: str = Field("default", max_length=128)


class SearchResponse(BaseModel):
    query: str
    results: list[dict]
    latency_ms: float


class StoreRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=50000)
    room: str = Field(..., min_length=1, max_length=128)
    locus: str | None = Field(None, max_length=128)
    source_type: str = Field(
        "user_input", pattern="^(user_input|agent_reasoning|consolidation|import|p2p_share)$"
    )
    tenant_id: str = Field("default", max_length=128)


class StoreResponse(BaseModel):
    chunk_id: int
    room: str


class FeedbackRequest(BaseModel):
    chunk_id: int = Field(..., ge=1)
    was_useful: bool
    user_id: str = "default"
    feedback_type: str = Field("explicit", pattern="^(implicit|explicit|repair)$")
    tenant_id: str = Field("default", max_length=128)


class FeedbackResponse(BaseModel):
    success: bool


class StatusResponse(BaseModel):
    rooms: int
    active_chunks: int
    tfc: dict
    embedding_model_available: bool


class DashboardRoom(BaseModel):
    name: str
    active_chunks: int
    loci: int


class DashboardRuntime(BaseModel):
    version: str
    node_name: str
    device: str
    edition: str
    vector_index: str
    embedding_model: str
    embedding_model_available: bool
    scheduler_running: bool
    sovereign_mode: bool
    backup_enabled: bool
    backup_running: bool
    telemetry_export_enabled: bool
    api_auth_enabled: bool
    encryption_mode: str


class DashboardPalace(BaseModel):
    active_chunks: int
    released_chunks: int
    rooms: int
    occupied_rooms: int
    loci: int
    topology: list[DashboardRoom]


class DashboardStorage(BaseModel):
    database_mb: float | None
    wal_mb: float | None
    vector_index_mb: float | None
    total_store_mb: float | None
    estimated_active_mb: float
    configured_limit_mb: int
    pressure_pct: float
    eviction_trigger_pct: int


class DashboardLifecycle(BaseModel):
    decay_scheduler_running: bool
    interference_mode: str
    budget_scheduler_running: bool
    resolution_counts: dict[str, int]


class DashboardFeedback(BaseModel):
    total: int
    positive: int
    satisfaction_pct: float | None


class DashboardDataResponse(BaseModel):
    measured_at: int
    tenant_id: str
    runtime: DashboardRuntime
    palace: DashboardPalace
    storage: DashboardStorage
    lifecycle: DashboardLifecycle
    tfc: dict[str, float | int]
    feedback_7d: DashboardFeedback
    evaluation: dict


class TurnRequest(BaseModel):
    user_msg: str = Field(..., min_length=1, max_length=50000)
    assistant_msg: str = Field(..., min_length=1, max_length=50000)
    room: str = Field("conversations", max_length=128)
    retrieved_chunk_ids: list[int] = Field(default_factory=list, max_length=100)
    tenant_id: str = Field("default", max_length=128)


class TurnResponse(BaseModel):
    user_chunk_id: int | None
    assistant_chunk_id: int | None
    room: str
    feedback_logged_for: list[int]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_pipeline() -> SearchPipeline | None:
    return _state.get("pipeline")


def _get_conversation_memory() -> ConversationMemory | None:
    return _state.get("conversation")


def _get_tfc(tenant_id: str = "default") -> TwinForceController:
    """Return the TFC controller for a tenant.

    TFC state is mutated on every search (Stage 5) and by the repair loop, so
    one shared instance made retrieval quality order-dependent across requests
    AND tenants. State is therefore scoped per tenant: tenants are independent
    palaces, and concurrent requests within one tenant still share that
    tenant's equilibrium (single-process API).
    """
    tfcs: dict[str, TwinForceController] = _state.setdefault("tfcs", {})
    tfc = tfcs.get(tenant_id)
    if not isinstance(tfc, TwinForceController):
        tfc = TwinForceController(config=_get_config())
        tfcs[tenant_id] = tfc
    return tfc


def _request_pipeline(conn: sqlite3.Connection, tenant_id: str) -> SearchPipeline:
    """Build a request-scoped pipeline over a pooled connection.

    The heavy per-connection work (vector index extension load, KNN grammar
    probe, FTS optimize) is amortised by the per-connection channel cache in
    ``lummenna.force.mnemonic.store``, so after a pooled connection's first
    use this is a cache hit on every channel. The TFC comes from the
    per-tenant registry so one tenant's repair-loop mutation cannot bleed
    into another tenant's search.
    """
    from lummenna.force.contextual.embed import MockEmbedder
    from lummenna.force.mnemonic.store import _get_lexical_channel, _get_vector_channel

    pipeline = SearchPipeline.__new__(SearchPipeline)
    pipeline.conn = conn
    pipeline.config = _get_config()
    pipeline.tfc = _get_tfc(tenant_id)
    if _state.get("intent_router") is None:
        from lummenna.intent import IntentRouter

        _state["intent_router"] = IntentRouter()
    pipeline.intent_router = _state["intent_router"]
    pipeline.lexical = _get_lexical_channel(conn)
    pipeline.vector = _get_vector_channel(conn, _get_config())
    embedder = _state.get("embedder")
    if embedder is None:
        embedder = MockEmbedder(dims=_get_config().embedding_dims)
    pipeline.embedder = embedder
    pipeline.graph = None
    return pipeline


def _request_conversation(conn: sqlite3.Connection, tenant_id: str) -> ConversationMemory:
    """Build a request-scoped ConversationMemory over a pooled connection.

    Construction cost is a handful of index reads (goals, epistemic state,
    user profile) after the pipeline's channel cache has warmed; the embedder
    is the process-wide instance, and the TFC is per tenant.
    """
    from lummenna.force.contextual.embed import MockEmbedder

    embedder = _state.get("embedder")
    if embedder is None:
        embedder = MockEmbedder(dims=_get_config().embedding_dims)
    return ConversationMemory(
        config=_get_config(),
        conn=conn,
        tfc=_get_tfc(tenant_id),
        embedder=embedder,
        tenant_id=tenant_id,
    )


@contextmanager
def pooled_conn(timeout: float = 30.0):
    """Check a pooled connection out for the duration of a request handler.

    Usage in sync handlers (which FastAPI runs on its threadpool)::

        with pooled_conn() as conn:
            ...

    Yields:
        An exclusive sqlite3.Connection. Broken connections (sqlite3.Error on
        release) are discarded and replaced lazily by the pool.
    """
    pool = _get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    conn = pool.acquire(timeout=timeout)
    try:
        yield conn
    except sqlite3.Error:
        pool.discard(conn)
        raise
    else:
        # Roll back any uncommitted implicit transaction so the next borrower
        # never inherits this handler's partial write state.
        with contextlib.suppress(sqlite3.Error):
            conn.rollback()
        pool.release(conn)


def _resolve_tenant(request: Request, requested_tenant: str = "default") -> str:
    """Resolve one tenant scope and enforce its entitlement at the API boundary."""
    explicit_tenant = request.headers.get("X-Tenant-ID") or request.query_params.get("tenant_id")
    if explicit_tenant and requested_tenant != "default" and explicit_tenant != requested_tenant:
        raise HTTPException(status_code=400, detail="Tenant header and request body disagree")
    tenant_id = explicit_tenant or requested_tenant
    if not tenant_id or len(tenant_id) > 128:
        raise HTTPException(status_code=422, detail="Tenant ID must contain 1 to 128 characters")
    if tenant_id != "default":
        try:
            _get_config().assert_licensed("multi_tenant")
        except LicenseError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    return tenant_id


def _scheduler_running() -> bool:
    scheduler = _state.get("scheduler")
    if scheduler is None:
        return False
    try:
        running = scheduler.is_running
        return bool(running() if callable(running) else running)
    except (AttributeError, RuntimeError):
        return False


def _size_mb(path: Path) -> float | None:
    try:
        return path.stat().st_size / (1024 * 1024)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


def _restore_store_if_configured() -> None:
    """Adopt the newest off-device snapshot when the local store is empty.

    Must run BEFORE initialize_palace, which opens the database: restoring
    underneath a live connection would corrupt it.

    A failure here is deliberately fatal. If a snapshot exists but cannot be
    restored, continuing would start the API on an empty store, the backup
    scheduler would then replicate that empty store, and retention pruning
    would eventually delete the last good snapshot. A refused startup is
    recoverable; that cascade is not.
    """
    if not _config.backup_enabled:
        return

    from lummenna.ops.backup import restore_if_missing

    restored = restore_if_missing(_config)
    logger.info("store_restore_checked", restored=restored)


def _start_backup_scheduler() -> None:
    """Start periodic off-device replication when it is enabled and legal."""
    if not _config.backup_enabled:
        return

    from lummenna.ops.backup import scheduler_if_enabled

    scheduler = scheduler_if_enabled(_config)
    if scheduler is None:
        return
    scheduler.start()
    _state["backup_scheduler"] = scheduler
    logger.info("backup_scheduler_started", interval_seconds=scheduler.interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if _state.get("conn") is not None:
        yield
        return

    # Opt-in Azure Monitor / Application Insights export. Raises (fail-loud)
    # if configured while sovereign, or configured without the azure extra.
    from lummenna.telemetry import configure_azure_monitor_if_enabled

    configure_azure_monitor_if_enabled(_config, app)

    # Strictly before initialize_palace: see the docstring for why a failure
    # here must stop startup rather than fall through to an empty store.
    _restore_store_if_configured()

    palace = initialize_palace(_config)

    # Connection pool: the first pooled connection runs the full schema setup
    # (WAL, migrations, permission sweep); later ones open lean. Handlers then
    # check connections out per request instead of sharing one.
    from lummenna.data.pool import ConnectionPool

    pool = ConnectionPool(_config, size=_config.api_db_pool_size)
    _state.update(palace)
    _state["pool"] = pool
    _start_backup_scheduler()
    # Start background forgetting scheduler so L1/L3 run without a separate daemon
    try:
        from lummenna.sleep import SleepScheduler

        scheduler = SleepScheduler(_config)
        scheduler.start()
        _state["scheduler"] = scheduler
        logger.info("background_scheduler_started")
    except Exception as exc:
        logger.error(
            "background_scheduler_failed",
            error=str(exc),
            effect="memory lifecycle DISABLED: L1 decay and L3 eviction will not run",
            remedy="install APScheduler or run 'lummenna daemon start' separately",
        )
    logger.info(
        "server_started",
        device=_config.device,
        model_available=_state.get("embedding_model_available", False),
    )
    yield
    if "scheduler" in _state:
        _state["scheduler"].stop()
    if "backup_scheduler" in _state:
        # Take a final snapshot on the way out so an orderly shutdown, which
        # is what a normal revision swap looks like, does not lose the writes
        # made since the last interval fired.
        _state["backup_scheduler"].run_cycle()
        _state["backup_scheduler"].stop()
    # Close the pool first: in-flight pooled connections were already checked
    # in; the admin connection is closed last so probes stay answerable while
    # the schedulers drain. (Regression guard: the old code called
    # _state.get("conn", sqlite3).close(), i.e. .close() on the sqlite3 module
    # itself when the key was absent.)
    shutdown_pool = _state.get("pool")
    if isinstance(shutdown_pool, ConnectionPool):
        shutdown_pool.close()
    conn = _state.get("conn")
    if conn is not None:
        conn.close()
    logger.info("server_stopped")


app.router.lifespan_context = lifespan


# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    # logger.exception renders the traceback (structlog's default processor
    # chain includes format_exc_info); the response body stays generic so
    # internals never leak to the caller, but on-call gets the stack.
    logger.exception(
        "unhandled_error",
        request_id=request_id,
        error_type=type(exc).__name__,
        path=request.url.path,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": request_id},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    conn = _get_conn()
    if conn is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    try:
        conn.execute("SELECT 1")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database unreachable") from exc


@app.get("/ready")
def ready() -> dict:
    """Kubernetes readiness probe: serve traffic only when fully initialised.

    Liveness (``/health``) answers "is the process alive"; readiness answers
    "can this instance serve correct results": database reachable AND the
    real embedding model loaded (or mock explicitly opted into for testing).
    A pod whose model failed to provision stays out of the Service endpoints
    instead of serving lexical-only degraded answers.
    """
    conn = _get_conn()
    if conn is None:
        raise HTTPException(status_code=503, detail="not ready: database not initialised")
    try:
        conn.execute("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="not ready: database unreachable") from exc
    model_ok = _state.get("embedding_model_available", False)
    if not model_ok and not _config.allow_mock_embedder:
        raise HTTPException(status_code=503, detail="not ready: embedding model not loaded")
    return {
        "status": "ready",
        "embedding_model_available": model_ok,
        "scheduler_running": "scheduler" in _state,
    }


@app.get("/metrics/prometheus")
def metrics_prometheus() -> Response:
    """Prometheus text exposition: request counters/latency + palace gauges.

    Scrape target for Prometheus, Azure Monitor managed Prometheus, or the
    Container Insights Prometheus agent. Unmeasured values are omitted,
    never rendered as zero.
    """
    gauges: dict[str, float | int | None] = {
        "lummenna_process_peak_rss_mb": _process_peak_rss_mb(),
    }
    conn = _get_conn()
    if conn is not None:
        try:
            gauges["lummenna_active_chunks"] = conn.execute(
                "SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL"
            ).fetchone()[0]
            gauges["lummenna_rooms"] = conn.execute("SELECT COUNT(*) FROM room").fetchone()[0]
            gauges["lummenna_forgotten_chunks"] = conn.execute(
                "SELECT COUNT(*) FROM chunk WHERE valid_to IS NOT NULL"
            ).fetchone()[0]
        except Exception as exc:
            logger.warning("prometheus_gauges_failed", error=str(exc))
    with contextlib.suppress(OSError):
        gauges["lummenna_db_size_bytes"] = _config.db_path.stat().st_size
    gauges["lummenna_embedding_model_available"] = int(
        _state.get("embedding_model_available", False)
    )
    body = _metrics.render(gauges)
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


@v1_router.post("/search")
@limiter.limit(_config.api_rate_limit)
def search(request: Request, req: SearchRequest) -> SearchResponse:
    """Sync handler: FastAPI runs it on the threadpool so ONNX inference and
    SQLite work never block the event loop. Uses a pooled connection and a
    per-tenant TFC, so concurrent searches never share mutable state."""
    t0 = time.perf_counter()
    tenant_id = _resolve_tenant(request, req.tenant_id)
    with pooled_conn() as conn:
        pipeline = _request_pipeline(conn, tenant_id)
        results = pipeline.execute(req.query, k=req.top_k, tenant_id=tenant_id)

        serialised = [
            {
                "chunk_id": rc.chunk_id,
                "room": rc.room_name,
                "locus": rc.locus_name,
                "content": rc.content,
                "score": round(rc.final_score, 4),
                "vm_score": round(rc.vm_score, 4),
                "provenance_id": rc.provenance_id,
            }
            for rc in results[: req.top_k]
        ]
        latency_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "search_completed",
            query=req.query[:100],
            results=len(serialised),
            latency_ms=round(latency_ms, 2),
        )
        request_id = getattr(request.state, "request_id", "unknown")
        log_audit_event(
            conn=conn,
            event_type="api_request",
            actor="anonymous",
            resource_type="query",
            resource_id=None,
            action="search",
            metadata_json=json.dumps(
                {
                    "query_prefix": req.query[:100],
                    "results": len(serialised),
                    "tenant_id": tenant_id,
                }
            ),
            client_ip=get_remote_address(request),
            request_id=request_id,
        )
    return SearchResponse(query=req.query, results=serialised, latency_ms=round(latency_ms, 2))


@v1_router.post("/store")
@limiter.limit(_config.api_rate_limit)
def store(request: Request, req: StoreRequest) -> StoreResponse:
    from lummenna.force.mnemonic.store import store_memory

    config = _get_config()
    tenant_id = _resolve_tenant(request, req.tenant_id)
    embedder = _state.get("embedder")
    # Inference before touching the pool: keeps the expensive CPU work off the
    # writer connection so its slot is held for the transaction only.
    embedding = embedder.encode_single(req.content) if embedder is not None else None

    with pooled_conn() as conn:
        try:
            chunk_id = store_memory(
                conn,
                content=req.content,
                room_name=req.room,
                locus_name=req.locus,
                source_type=req.source_type,
                embedding=embedding,
                config=config,
                tenant_id=tenant_id,
            )
        except TenantQuotaError as exc:
            # 429, not 403: the tenant may retry once the operator raises the
            # quota or memories are forgotten. Rate-limit semantics, permanent
            # fix is administrative.
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        request_id = getattr(request.state, "request_id", "unknown")
        log_audit_event(
            conn=conn,
            event_type="memory_stored",
            actor="anonymous",
            resource_type="chunk",
            resource_id=chunk_id,
            action="store",
            metadata_json=json.dumps({"room": req.room, "tenant_id": tenant_id}),
            client_ip=get_remote_address(request),
            request_id=request_id,
        )
    return StoreResponse(chunk_id=chunk_id, room=req.room)


@v1_router.get("/status")
def status(request: Request) -> StatusResponse:
    with pooled_conn() as conn:
        tenant_id = _resolve_tenant(request)
        room_count = conn.execute(
            "SELECT COUNT(*) FROM room WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()[0]
        chunk_count = conn.execute(
            "SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL AND tenant_id = ?",
            (tenant_id,),
        ).fetchone()[0]
        tfc = _get_tfc(tenant_id)
        return StatusResponse(
            rooms=room_count,
            active_chunks=chunk_count,
            tfc=tfc.to_env(),
            embedding_model_available=_state.get("embedding_model_available", False),
        )


@v1_router.post("/feedback")
@limiter.limit(_config.api_rate_limit)
def feedback(request: Request, req: FeedbackRequest) -> FeedbackResponse:
    tenant_id = _resolve_tenant(request, req.tenant_id)
    with pooled_conn() as conn:
        owner = conn.execute(
            "SELECT 1 FROM chunk WHERE chunk_id = ? AND tenant_id = ?",
            (req.chunk_id, tenant_id),
        ).fetchone()
        if owner is None:
            raise HTTPException(status_code=404, detail="Chunk not found in tenant")
        conversation = _request_conversation(conn, tenant_id)
        conversation.log_explicit_feedback(
            chunk_id=req.chunk_id,
            was_useful=req.was_useful,
            user_id=req.user_id,
            feedback_type=req.feedback_type,
            tenant_id=tenant_id,
        )
        request_id = getattr(request.state, "request_id", "unknown")
        log_audit_event(
            conn=conn,
            event_type="feedback_logged",
            actor=req.user_id,
            resource_type="feedback",
            resource_id=req.chunk_id,
            action="feedback",
            metadata_json=json.dumps({"was_useful": req.was_useful, "tenant_id": tenant_id}),
            client_ip=get_remote_address(request),
            request_id=request_id,
        )
    return FeedbackResponse(success=True)


@v1_router.post("/assemble")
@limiter.limit(_config.api_rate_limit)
def assemble(request: Request, req: SearchRequest) -> dict:
    t0 = time.perf_counter()
    tenant_id = _resolve_tenant(request, req.tenant_id)
    with pooled_conn() as conn:
        conversation = _request_conversation(conn, tenant_id)
        turn = conversation.retrieve_and_assemble(req.query, top_k=req.top_k, tenant_id=tenant_id)
    latency_ms = (time.perf_counter() - t0) * 1000
    return {
        "query": req.query,
        "assembled_context": turn.assembled_context,
        "retrieved_count": len(turn.retrieved_chunks),
        "latency_ms": round(latency_ms, 2),
    }


@v1_router.post("/turn")
@limiter.limit(_config.api_rate_limit)
def turn(request: Request, req: TurnRequest) -> TurnResponse:
    tenant_id = _resolve_tenant(request, req.tenant_id)

    with pooled_conn() as conn:
        conversation = _request_conversation(conn, tenant_id)
        stubs = []
        for cid in req.retrieved_chunk_ids:
            row = conn.execute(
                """SELECT c.content, r.name AS room_name, l.name AS locus_name,
                          c.vm_score, c.provenance_root,
                          (strftime('%s','now') - c.created_at) / 3600.0 AS age_hours
                   FROM chunk c
                   JOIN room r ON r.room_id = c.room_id
                   LEFT JOIN locus l ON l.locus_id = c.locus_id
                   WHERE c.chunk_id = ? AND c.valid_to IS NULL AND c.tenant_id = ?""",
                (cid, tenant_id),
            ).fetchone()
            if row:
                stubs.append(
                    RetrievedChunk(
                        chunk_id=cid,
                        room_name=row["room_name"] or "unknown",
                        locus_name=row["locus_name"] or "none",
                        content=row["content"] or "",
                        provenance_id=row["provenance_root"],
                        rrf_score=0.0,
                        vm_score=row["vm_score"] or 0.0,
                        frqad_score=0.0,
                        recency_hours=row["age_hours"] or 0.0,
                        final_score=0.0,
                    )
                )

        user_id, assistant_id = conversation.store_turn(
            user_msg=req.user_msg,
            assistant_msg=req.assistant_msg,
            retrieved_chunks=stubs,
            room_name=req.room,
            tenant_id=tenant_id,
        )
        request_id = getattr(request.state, "request_id", "unknown")
        log_audit_event(
            conn=conn,
            event_type="turn_stored",
            actor="anonymous",
            resource_type="chunk",
            resource_id=user_id,
            action="turn",
            metadata_json=json.dumps({"room": req.room, "tenant_id": tenant_id}),
            client_ip=get_remote_address(request),
            request_id=request_id,
        )
    return TurnResponse(
        user_chunk_id=user_id,
        assistant_chunk_id=assistant_id,
        room=req.room,
        feedback_logged_for=[stub.chunk_id for stub in stubs],
    )


# ---------------------------------------------------------------------------
# Dashboard & Metrics endpoints
# ---------------------------------------------------------------------------

_DASHBOARD_HTML_PATH = Path(__file__).with_name("dashboard.html")
_API_DOCS_HTML_PATH = Path(__file__).with_name("api_docs.html")
_FAVICON_PATH = Path(__file__).with_name("favicon.svg")


def _read_html(path: Path, missing_detail: str) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8")
    raise HTTPException(status_code=404, detail=missing_detail)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home() -> str:
    """Serve the observatory as the application's canonical frontend."""
    return _read_html(_DASHBOARD_HTML_PATH, "Dashboard HTML not found")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    """Serve the Lummenna effectiveness dashboard."""
    return _read_html(_DASHBOARD_HTML_PATH, "Dashboard HTML not found")


@app.get("/docs", response_class=HTMLResponse, include_in_schema=False)
@app.get("/redoc", response_class=HTMLResponse, include_in_schema=False)
async def api_docs() -> str:
    """Serve the dependency-free, observatory-themed OpenAPI reference."""
    return _read_html(_API_DOCS_HTML_PATH, "API documentation HTML not found")


@app.get("/favicon.svg", include_in_schema=False)
async def favicon() -> Response:
    """Serve the canonical Lummenna core mark."""
    if _FAVICON_PATH.is_file():
        return Response(_FAVICON_PATH.read_text(encoding="utf-8"), media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="Favicon not found")


def _load_evaluation_summary() -> dict:
    """Load all artifact-backed evaluation evidence for the dashboard."""
    from lummenna.bench_results import load_evaluation_summary

    return load_evaluation_summary()


@v1_router.get("/dashboard-data", response_model=DashboardDataResponse)
def dashboard_data(request: Request) -> DashboardDataResponse:
    """Return a measured, tenant-scoped snapshot for the observatory."""
    with pooled_conn() as conn:
        return _dashboard_data_impl(request, conn)


def _dashboard_data_impl(request: Request, conn: sqlite3.Connection) -> DashboardDataResponse:
    config = _get_config()
    tenant_id = _resolve_tenant(request)

    rooms = conn.execute(
        """
        SELECT
            r.name,
            COUNT(DISTINCT l.locus_id) AS loci,
            COUNT(DISTINCT c.chunk_id) AS active_chunks
        FROM room r
        LEFT JOIN locus l ON l.room_id = r.room_id AND l.tenant_id = ?
        LEFT JOIN chunk c
            ON c.room_id = r.room_id
            AND c.tenant_id = ?
            AND c.valid_to IS NULL
        WHERE r.tenant_id = ?
        GROUP BY r.room_id
        HAVING COUNT(DISTINCT c.chunk_id) > 0
        ORDER BY active_chunks DESC, r.name ASC
        """,
        (tenant_id, tenant_id, tenant_id),
    ).fetchall()

    active_chunks = conn.execute(
        "SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL AND tenant_id = ?", (tenant_id,)
    ).fetchone()[0]
    released_chunks = conn.execute(
        "SELECT COUNT(*) FROM chunk WHERE valid_to IS NOT NULL AND optical_level = 2 AND tenant_id = ?",
        (tenant_id,),
    ).fetchone()[0]
    room_count = conn.execute(
        "SELECT COUNT(*) FROM room WHERE tenant_id = ?", (tenant_id,)
    ).fetchone()[0]
    locus_count = conn.execute(
        "SELECT COUNT(*) FROM locus WHERE tenant_id = ?", (tenant_id,)
    ).fetchone()[0]
    resolution_rows = conn.execute(
        """
        SELECT resolution, COUNT(*) AS chunks
        FROM chunk
        WHERE valid_to IS NULL AND tenant_id = ?
        GROUP BY resolution
        """,
        (tenant_id,),
    ).fetchall()
    resolution_counts = {stage: 0 for stage in ("FP32", "FP16", "INT8", "BINARY")}
    for row in resolution_rows:
        if row["resolution"] in resolution_counts:
            resolution_counts[row["resolution"]] = row["chunks"]
    resolution_counts["RELEASED"] = released_chunks

    feedback_stats = conn.execute(
        """
        SELECT
            COUNT(*) AS total_feedback,
            SUM(CASE WHEN positive = 1 THEN 1 ELSE 0 END) AS positive_feedback
        FROM feedback_log
        WHERE created_at >= unixepoch('now', '-7 days') AND tenant_id = ?
        """,
        (tenant_id,),
    ).fetchone()
    feedback_total = feedback_stats["total_feedback"] or 0
    feedback_positive = feedback_stats["positive_feedback"] or 0

    db_size_mb = _size_mb(config.db_path)
    wal_size_mb = _size_mb(Path(f"{config.db_path}-wal"))
    vector_size_mb = _size_mb(config.store_path / "vectors.usearch")
    measured_sizes = [size for size in (db_size_mb, wal_size_mb, vector_size_mb) if size]
    total_store_mb = sum(measured_sizes) if db_size_mb is not None else None
    global_active_chunks = conn.execute(
        "SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL"
    ).fetchone()[0]
    estimated_active_mb = global_active_chunks * BYTES_PER_CHUNK_ESTIMATE / (1024 * 1024)
    memory_limit_mb = config.memory_limit_mb
    pressure_pct = (
        round((estimated_active_mb / memory_limit_mb) * 100, 3) if memory_limit_mb > 0 else 0.0
    )

    scheduler_running = _scheduler_running()
    backup_scheduler = _state.get("backup_scheduler")
    backup_running = bool(getattr(backup_scheduler, "running", False))
    tfc = _get_tfc(tenant_id)

    return DashboardDataResponse(
        measured_at=int(time.time()),
        tenant_id=tenant_id,
        runtime=DashboardRuntime(
            version=__version__,
            node_name=config.device_name,
            device=config.device,
            edition=config.edition,
            vector_index=config.vector_index,
            embedding_model=config.embedding_model,
            embedding_model_available=bool(_state.get("embedding_model_available", False)),
            scheduler_running=scheduler_running,
            sovereign_mode=config.sovereign,
            backup_enabled=config.backup_enabled,
            backup_running=backup_running,
            telemetry_export_enabled=bool(os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")),
            api_auth_enabled=bool(_config.api_key),
            encryption_mode=config.database_encryption_mode,
        ),
        palace=DashboardPalace(
            active_chunks=active_chunks,
            released_chunks=released_chunks,
            rooms=room_count,
            occupied_rooms=len(rooms),
            loci=locus_count,
            topology=[
                DashboardRoom(
                    name=row["name"], active_chunks=row["active_chunks"], loci=row["loci"]
                )
                for row in rooms
            ],
        ),
        storage=DashboardStorage(
            database_mb=round(db_size_mb, 3) if db_size_mb is not None else None,
            wal_mb=round(wal_size_mb, 3) if wal_size_mb is not None else None,
            vector_index_mb=round(vector_size_mb, 3) if vector_size_mb is not None else None,
            total_store_mb=round(total_store_mb, 3) if total_store_mb is not None else None,
            estimated_active_mb=round(estimated_active_mb, 3),
            configured_limit_mb=memory_limit_mb,
            pressure_pct=pressure_pct,
            eviction_trigger_pct=85,
        ),
        lifecycle=DashboardLifecycle(
            decay_scheduler_running=scheduler_running,
            interference_mode="on_write",
            budget_scheduler_running=scheduler_running,
            resolution_counts=resolution_counts,
        ),
        tfc=tfc.to_env(),
        feedback_7d=DashboardFeedback(
            total=feedback_total,
            positive=feedback_positive,
            satisfaction_pct=(
                round((feedback_positive / feedback_total) * 100, 1) if feedback_total else None
            ),
        ),
        evaluation=_load_evaluation_summary(),
    )


@app.get("/metrics")
def metrics() -> dict:
    """Return machine-readable effectiveness metrics for monitoring integrations."""
    with pooled_conn() as conn:
        room_count = conn.execute("SELECT COUNT(*) FROM room").fetchone()[0]
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunk WHERE valid_to IS NULL").fetchone()[
            0
        ]
        locus_count = conn.execute("SELECT COUNT(*) FROM locus").fetchone()[0]
        forgotten = conn.execute(
            "SELECT COUNT(*) FROM chunk WHERE valid_to IS NOT NULL"
        ).fetchone()[0]

        feedback_row = conn.execute(
            "SELECT COUNT(*) AS c, AVG(CASE WHEN positive = 1 THEN 1.0 ELSE 0.0 END) AS sat FROM feedback_log"
        ).fetchone()

    tfc = _get_tfc()
    config = _get_config()

    return {
        "lummenna_version": __version__,
        "system": {
            "device": config.device,
            "node_name": config.device_name,
            "edition": config.edition,
            "vector_index": config.vector_index,
            "embedding_model": config.embedding_model,
            "embedding_model_available": _state.get("embedding_model_available", False),
            "scheduler_running": _scheduler_running(),
        },
        "palace": {
            "rooms": room_count,
            "loci": locus_count,
            "active_chunks": chunk_count,
            "forgotten_chunks": forgotten,
            "retention_rate_pct": round((chunk_count / max(1, chunk_count + forgotten)) * 100, 2),
        },
        "tfc": tfc.to_env(),
        "effectiveness": {
            "evaluation": _load_evaluation_summary(),
            "feedback_total": feedback_row["c"] or 0,
            "feedback_satisfaction_pct": (
                round(feedback_row["sat"] * 100, 1) if feedback_row["sat"] is not None else None
            ),
        },
        "runtime": {
            "sovereign_mode": config.sovereign,
            "backup_enabled": config.backup_enabled,
            "telemetry_export_enabled": bool(
                os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
            ),
            "encryption_mode": config.database_encryption_mode,
            "api_auth_enabled": bool(_config.api_key),
            "process_peak_rss_mb": _process_peak_rss_mb(),
        },
    }


def _process_peak_rss_mb() -> float | None:
    """Return this process's peak resident set size in MB (measured, not estimated).

    Uses ``resource.getrusage``: ``ru_maxrss`` is reported in bytes on macOS
    and kilobytes on Linux. Returns ``None`` on platforms without the
    ``resource`` module (e.g. Windows).
    """
    try:
        import resource
        import sys as _sys

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        divisor = 1024 * 1024 if _sys.platform == "darwin" else 1024
        return round(peak / divisor, 1)
    except (ImportError, ValueError, OSError):
        return None


app.include_router(v1_router)


def main() -> None:
    """CLI entrypoint for production server."""
    import uvicorn

    cfg = LummennaConfig()
    uvicorn.run(
        "lummenna.api.server:app",
        host=cfg.api_host,
        port=cfg.api_port,
        log_level=cfg.log_level,
    )


if __name__ == "__main__":
    main()
