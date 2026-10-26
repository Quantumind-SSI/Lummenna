"""Tests for lummenna.api.server."""

import pytest
from fastapi.testclient import TestClient

from lummenna.api.server import app
from lummenna.config import LummennaConfig
from lummenna.controller import TwinForceController
from lummenna.conversation import ConversationMemory
from lummenna.data.schema import get_connection, init_db
from lummenna.force.contextual.embed import MockEmbedder


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Create a TestClient with isolated filesystem state."""
    store = tmp_path / "store"
    models = tmp_path / "models"
    store.mkdir()
    models.mkdir()

    config = LummennaConfig(
        store_path=store,
        model_path=models,
        vector_index="sqlite-vec",
        device="generic",
    )
    conn = get_connection(config)
    init_db(conn)
    embedder = MockEmbedder(dims=config.embedding_dims)

    # Pre-seed module state
    from lummenna.api import server as server_mod

    tfc = TwinForceController(config=config)
    server_mod._state.clear()
    server_mod._state.update(
        {
            "config": config,
            "conn": conn,
            "pipeline": None,  # built below
            "conversation": ConversationMemory(
                config=config, conn=conn, tfc=tfc, embedder=embedder
            ),
            "tfc": tfc,
            "embedder": embedder,
            "embedding_model_available": False,
        }
    )

    # Build pipeline after state is set
    from lummenna.search import SearchPipeline

    server_mod._state["pipeline"] = SearchPipeline(conn, config, tfc=tfc, embedder=embedder)

    # Handlers check connections out of a pool; seed one matching this store.
    from lummenna.data.pool import ConnectionPool

    server_mod._state["pool"] = ConnectionPool(config, size=2)

    with TestClient(app) as c:
        yield c

    pooled = server_mod._state.get("pool")
    if pooled is not None:
        pooled.close()
    conn.close()
    server_mod._state.clear()


class TestSecurityHeaders:
    def test_security_headers_present(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("x-frame-options") == "DENY"
        assert "strict-transport-security" in resp.headers
        assert "content-security-policy" in resp.headers
        assert "referrer-policy" in resp.headers


class TestAuthMiddleware:
    def test_protected_endpoint_without_key(self, client, monkeypatch):
        # Default fixture has no API key set, so endpoints are open
        resp = client.post("/v1/search", json={"query": "test", "top_k": 3})
        assert resp.status_code == 200

    def test_protected_endpoint_rejects_invalid_key(self, client, monkeypatch):
        # Temporarily set an API key on the global config used by the middleware
        from lummenna.api import server as server_mod

        original_key = server_mod._config.api_key
        server_mod._config.api_key = "supersecrettestkey"
        try:
            resp = client.post("/v1/search", json={"query": "test", "top_k": 3})
            assert resp.status_code == 401
            assert "API key" in resp.json()["detail"]

            resp = client.post(
                "/v1/search",
                json={"query": "test", "top_k": 3},
                headers={"X-API-Key": "wrong-key"},
            )
            assert resp.status_code == 401
        finally:
            server_mod._config.api_key = original_key

    def test_protected_endpoint_accepts_valid_key(self, client, monkeypatch):
        from lummenna.api import server as server_mod

        original_key = server_mod._config.api_key
        server_mod._config.api_key = "supersecrettestkey"
        try:
            resp = client.post(
                "/v1/search",
                json={"query": "test", "top_k": 3},
                headers={"X-API-Key": "supersecrettestkey"},
            )
            assert resp.status_code == 200
        finally:
            server_mod._config.api_key = original_key


class TestTenantBoundary:
    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("get", "/v1/status", None),
            ("get", "/v1/dashboard-data", None),
            ("post", "/v1/search", {"query": "x", "tenant_id": "team"}),
            (
                "post",
                "/v1/store",
                {"content": "x", "room": "r", "tenant_id": "team"},
            ),
            ("post", "/v1/assemble", {"query": "x", "tenant_id": "team"}),
            (
                "post",
                "/v1/feedback",
                {"chunk_id": 1, "was_useful": True, "tenant_id": "team"},
            ),
            (
                "post",
                "/v1/turn",
                {"user_msg": "u", "assistant_msg": "a", "tenant_id": "team"},
            ),
        ],
    )
    def test_non_default_tenant_requires_multi_tenant_entitlement(
        self, client, method, path, payload
    ):
        response = client.request(
            method,
            path,
            headers={"X-Tenant-ID": "team"},
            json=payload,
        )

        assert response.status_code == 403

    def test_conflicting_header_and_body_tenants_are_rejected(self, client):
        response = client.post(
            "/v1/search",
            headers={"X-Tenant-ID": "tenant-a"},
            json={"query": "x", "tenant_id": "tenant-b"},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "Tenant header and request body disagree"


class TestRateLimiting:
    def test_rate_limit_middleware_present(self, client):
        from lummenna.api import server as server_mod

        assert hasattr(server_mod.app.state, "limiter")
        assert server_mod.app.state.limiter is not None
        # Verify the decorator is wired by checking route handlers
        route = None
        for r in server_mod.app.routes:
            if getattr(r, "path", None) == "/v1/search":
                route = r
                break
            # Included routers in newer FastAPI versions
            router = getattr(r, "original_router", None)
            if router:
                for sr in getattr(router, "routes", []):
                    if getattr(sr, "path", None) == "/v1/search":
                        route = sr
                        break
            if route:
                break
        assert route is not None
        # Slowapi uses a wrapper, so endpoint is the limiter wrapper
        assert route.endpoint.__name__ == "search"

    def test_default_rate_limit_configured(self, client):
        from lummenna.api import server as server_mod

        assert "minute" in server_mod._config.api_rate_limit


class TestRequestSizeLimit:
    def test_request_too_large_rejected(self, client, monkeypatch):
        from lummenna.api import server as server_mod

        original_size = server_mod._config.request_max_size_bytes
        server_mod._config.request_max_size_bytes = 100
        # Re-initialise size limit middleware with new config (FastAPI applies middleware on add)
        # Since we can't easily swap middleware at runtime, we test via direct assertion
        # that the middleware class checks the header correctly.
        from lummenna.api.server import _SizeLimitMiddleware

        scope = {
            "type": "http",
            "headers": [(b"content-length", b"200")],
        }
        calls = []

        async def mock_app(scope, receive, send):
            calls.append("app")

        middleware = _SizeLimitMiddleware(mock_app, max_bytes=100)

        async def mock_send(msg):
            calls.append(msg)

        import asyncio

        asyncio.run(middleware(scope, None, mock_send))
        assert any(m.get("status") == 413 for m in calls if isinstance(m, dict))
        server_mod._config.request_max_size_bytes = original_size


class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestStatus:
    def test_status_initial(self, client):
        resp = client.get("/v1/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["rooms"] >= 0
        assert body["active_chunks"] >= 0
        assert "tfc" in body


class TestStoreAndSearch:
    def test_store_memory(self, client):
        resp = client.post(
            "/v1/store",
            json={
                "content": "The quick brown fox",
                "room": "animals",
                "source_type": "user_input",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["chunk_id"] > 0
        assert body["room"] == "animals"

    def test_search_finds_stored(self, client):
        # Store
        client.post(
            "/v1/store",
            json={
                "content": "project alpha requirements",
                "room": "projects",
                "source_type": "user_input",
            },
        )
        # Search
        resp = client.post("/v1/search", json={"query": "alpha requirements", "top_k": 5})
        assert resp.status_code == 200
        body = resp.json()
        assert body["query"] == "alpha requirements"
        assert len(body["results"]) >= 1


class TestFeedback:
    def test_feedback_creation(self, client):
        # Seed a chunk first so FK constraint is satisfied
        client.post(
            "/v1/store",
            json={
                "content": "feedback target",
                "room": "feedback_test",
                "source_type": "user_input",
            },
        )
        resp = client.post(
            "/v1/feedback",
            json={
                "chunk_id": 1,
                "was_useful": True,
                "user_id": "alice",
                "feedback_type": "explicit",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True


class TestAssemble:
    def test_assemble_empty(self, client):
        resp = client.post("/v1/assemble", json={"query": "what is the weather?", "top_k": 3})
        assert resp.status_code == 200
        body = resp.json()
        assert "assembled_context" in body
        assert body["retrieved_count"] >= 0


class TestTurn:
    def test_turn_store(self, client):
        resp = client.post(
            "/v1/turn",
            json={
                "user_msg": "Hello",
                "assistant_msg": "Hi there",
                "room": "conversations",
                "retrieved_chunk_ids": [],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_chunk_id"] > 0
        assert body["assistant_chunk_id"] > 0


class TestDashboard:
    def test_root_is_canonical_dashboard(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Lummenna Memory Palace" in resp.text
        assert "The evidence ledger" in resp.text

    def test_dashboard_serves_html(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Lummenna Memory Palace" in resp.text

    def test_themed_api_docs_are_generated_from_openapi(self, client):
        resp = client.get("/docs")
        assert resp.status_code == 200
        assert "Lummenna API | Local Reference" in resp.text
        assert 'fetch("/openapi.json"' in resp.text

    def test_openapi_documents_api_key_and_tenant_headers(self, client):
        schema = client.get("/openapi.json").json()

        assert schema["components"]["securitySchemes"]["ApiKeyAuth"] == {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "Required for /v1 operations when LUMMENNA_API_KEY is configured.",
        }
        operation = schema["paths"]["/v1/search"]["post"]
        assert operation["security"] == [{"ApiKeyAuth": []}]
        assert any(parameter["name"] == "X-Tenant-ID" for parameter in operation["parameters"])

    def test_favicon_is_served_locally(self, client):
        resp = client.get("/favicon.svg")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert "#28a982" in resp.text

    def test_dashboard_data(self, client):
        # Seed a room
        client.post(
            "/v1/store",
            json={
                "content": "dashboard test memory",
                "room": "dashboard_test",
                "source_type": "user_input",
            },
        )
        resp = client.get("/v1/dashboard-data")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == "default"
        assert body["palace"]["active_chunks"] == 1
        assert body["palace"]["rooms"] == 1
        assert body["palace"]["occupied_rooms"] == 1
        assert body["palace"]["topology"] == [
            {"name": "dashboard_test", "active_chunks": 1, "loci": 1}
        ]
        assert "tfc" in body
        assert body["runtime"]["embedding_model_available"] is False
        assert body["runtime"]["scheduler_running"] is False
        assert body["storage"]["configured_limit_mb"] == 512
        assert body["storage"]["database_mb"] is not None
        assert body["lifecycle"]["resolution_counts"]["FP32"] == 1
        assert body["lifecycle"]["resolution_counts"]["RELEASED"] == 0
        assert body["feedback_7d"]["satisfaction_pct"] is None
        assert body["evaluation"]["official_leaderboard_rank"] is None
        assert body["evaluation"]["retrieval"]["artifact_sha256"]

    def test_dashboard_data_does_not_leak_other_tenants(self, client):
        from lummenna.api import server as server_mod

        conn = server_mod._state["conn"]
        conn.execute("INSERT INTO room(name, tenant_id) VALUES(?, ?)", ("private", "other"))
        room_id = conn.execute(
            "SELECT room_id FROM room WHERE name = ? AND tenant_id = ?", ("private", "other")
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO chunk(room_id, content, content_hash, tenant_id)
            VALUES(?, ?, ?, ?)
            """,
            (room_id, "other tenant secret", "other-tenant-hash", "other"),
        )
        conn.commit()

        body = client.get("/v1/dashboard-data").json()

        assert body["palace"]["active_chunks"] == 0
        assert body["palace"]["topology"] == []

    def test_dashboard_reports_controller_mutated_by_live_search(self, client):
        before = client.get("/v1/dashboard-data").json()["tfc"]["a"]

        search = client.post("/v1/search", json={"query": "nothing has been stored", "top_k": 5})
        after = client.get("/v1/dashboard-data").json()["tfc"]["a"]

        assert search.status_code == 200
        assert search.json()["results"] == []
        assert after > before

    def test_metrics(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["lummenna_version"] == "1.0.1rc1"
        assert "system" in body
        assert "palace" in body
        assert "runtime" in body
        assert body["runtime"]["sovereign_mode"] is True
        assert body["effectiveness"]["evaluation"]["official_leaderboard_rank"] is None
