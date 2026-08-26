"""Tests for lummenna.observability, /ready, /metrics/prometheus, telemetry gating."""

import threading

import pytest

from lummenna.observability import MetricsRegistry, normalize_route


class TestMetricsRegistry:
    def test_counter_increment_and_render(self):
        reg = MetricsRegistry()
        reg.inc(
            "lumena_http_requests_total", {"route": "/health", "method": "GET", "status": "2xx"}
        )
        reg.inc(
            "lumena_http_requests_total", {"route": "/health", "method": "GET", "status": "2xx"}
        )
        out = reg.render()
        assert "# TYPE lumena_http_requests_total counter" in out
        assert 'lumena_http_requests_total{method="GET",route="/health",status="2xx"} 2' in out

    def test_histogram_buckets_cumulative(self):
        reg = MetricsRegistry(buckets=(0.1, 1.0))
        reg.observe("lumena_http_request_duration_seconds", 0.05)
        reg.observe("lumena_http_request_duration_seconds", 0.5)
        reg.observe("lumena_http_request_duration_seconds", 5.0)
        out = reg.render()
        assert 'le="0.1"} 1' in out
        assert 'le="1.0"} 2' in out
        assert 'le="+Inf"} 3' in out
        assert "lumena_http_request_duration_seconds_count 3" in out

    def test_gauges_none_omitted(self):
        """Unmeasured values must be omitted, never rendered as zero."""
        reg = MetricsRegistry()
        out = reg.render(gauges={"lumena_measured": 7, "lumena_unmeasured": None})
        assert "lumena_measured 7" in out
        assert "lumena_unmeasured" not in out

    def test_label_cardinality_bounded(self):
        """New label sets past the cap are dropped; memory stays bounded."""
        reg = MetricsRegistry()
        for i in range(600):
            reg.inc("lumena_http_requests_total", {"route": f"r{i}"})
        assert len(reg._counters) <= 512

    def test_thread_safety_no_lost_updates(self):
        reg = MetricsRegistry()

        def hammer():
            for _ in range(1000):
                reg.inc("c", {"t": "x"})
                reg.observe("h", 0.01, {"t": "x"})

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        out = reg.render()
        assert 'c{t="x"} 8000' in out
        assert 'h_count{t="x"} 8000' in out

    def test_normalize_route_bounds_cardinality(self):
        assert normalize_route("/v1/search") == "/v1/search"
        assert normalize_route("/etc/passwd") == "other"
        assert normalize_route("/v1/search/../../x") == "other"


class TestServerEndpoints:
    @pytest.fixture
    def client(self, tmp_path):
        from fastapi.testclient import TestClient

        from lummenna.api import server as server_mod
        from lummenna.config import LumenaConfig
        from lummenna.conversation import ConversationMemory
        from lummenna.data.schema import get_connection, init_db
        from lummenna.force.contextual.embed import MockEmbedder
        from lummenna.search import SearchPipeline

        store = tmp_path / "store"
        store.mkdir()
        config = LumenaConfig(
            store_path=store,
            model_path=tmp_path / "models",
            vector_index="sqlite-vec",
            device="generic",
            allow_mock_embedder=True,
        )
        conn = get_connection(config)
        init_db(conn)
        embedder = MockEmbedder(dims=config.embedding_dims)
        server_mod._state = {
            "config": config,
            "conn": conn,
            "conversation": ConversationMemory(config=config, conn=conn, embedder=embedder),
            "tfc": None,
            "embedder": embedder,
            "embedding_model_available": False,
        }
        server_mod._state["pipeline"] = SearchPipeline(conn, config, embedder=embedder)
        original_allow_mock = server_mod._config.allow_mock_embedder
        server_mod._config.allow_mock_embedder = True
        with TestClient(server_mod.app) as c:
            yield c, server_mod
        server_mod._config.allow_mock_embedder = original_allow_mock
        conn.close()

    def test_ready_with_mock_optin(self, client):
        c, server_mod = client
        resp = c.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_ready_503_without_model_and_without_optin(self, client):
        c, server_mod = client
        server_mod._config.allow_mock_embedder = False
        try:
            resp = c.get("/ready")
            assert resp.status_code == 503
            assert "embedding model" in resp.json()["detail"]
        finally:
            server_mod._config.allow_mock_embedder = True

    def test_prometheus_exposition(self, client):
        c, _ = client
        c.get("/health")  # generate at least one request metric
        resp = c.get("/metrics/prometheus")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        body = resp.text
        assert "lumena_http_requests_total" in body
        assert "lumena_http_request_duration_seconds_bucket" in body
        assert "lumena_uptime_seconds" in body
        assert "lumena_active_chunks" in body
        assert "lumena_embedding_model_available 0" in body

    def test_prometheus_is_public_even_with_api_key(self, client):
        c, server_mod = client
        original = server_mod._config.api_key
        server_mod._config.api_key = "secret-key"
        try:
            assert c.get("/metrics/prometheus").status_code == 200
            assert c.get("/ready").status_code == 200
        finally:
            server_mod._config.api_key = original


class TestTelemetryGating:
    def test_not_configured_returns_false(self, monkeypatch, tmp_path):
        from lummenna.config import LumenaConfig
        from lummenna.telemetry import configure_azure_monitor_if_enabled

        monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
        config = LumenaConfig(store_path=tmp_path / "s", sovereign=True)
        assert configure_azure_monitor_if_enabled(config) is False

    def test_sovereign_mode_blocks_export(self, monkeypatch, tmp_path):
        """Telemetry export while sovereign must raise, never silently leak."""
        from lummenna.brand.errors import SovereignViolationError
        from lummenna.config import LumenaConfig
        from lummenna.telemetry import configure_azure_monitor_if_enabled

        monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=00000000")
        config = LumenaConfig(store_path=tmp_path / "s", sovereign=True)
        with pytest.raises(SovereignViolationError):
            configure_azure_monitor_if_enabled(config)

    def test_missing_package_fails_loud(self, monkeypatch, tmp_path):
        """Configured telemetry with missing package must abort startup."""
        import builtins

        from lummenna.config import LumenaConfig
        from lummenna.telemetry import configure_azure_monitor_if_enabled

        monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=00000000")
        config = LumenaConfig(store_path=tmp_path / "s", sovereign=False)

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("azure"):
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(RuntimeError, match="lummenna\\[azure\\]"):
            configure_azure_monitor_if_enabled(config)


class TestBusyTimeout:
    def test_busy_timeout_pragma_set(self, tmp_path):
        """Every connection must carry a busy_timeout against writer contention."""
        from lummenna.config import LumenaConfig
        from lummenna.data.schema import get_connection

        config = LumenaConfig(store_path=tmp_path / "store", vector_index="sqlite-vec")
        conn = get_connection(config)
        try:
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert timeout == 5000
        finally:
            conn.close()


class TestAuthFailureBound:
    def test_auth_failures_dict_is_bounded(self):
        """Scanning floods must not grow the tracker without bound."""
        from lummenna.api import server as server_mod

        server_mod._auth_failures.clear()
        for i in range(server_mod._AUTH_FAILURES_MAX + 500):
            server_mod._record_auth_failure(f"10.0.{i // 256}.{i % 256}")
        assert len(server_mod._auth_failures) <= server_mod._AUTH_FAILURES_MAX
        server_mod._auth_failures.clear()
