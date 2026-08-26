"""Tests for lummenna.integrations.mcp_server."""

import asyncio

import pytest

pytestmark = pytest.mark.skipif(
    pytest.importorskip("mcp", reason="mcp package not installed") is None,
    reason="mcp package not installed",
)

from lummenna.integrations.mcp_server import _preview, app_lifespan, mcp  # noqa: E402


class TestMCPServer:
    def test_tools_registered(self):
        async def _list():
            tools = await mcp.list_tools()
            return [t.name for t in tools]

        tool_names = asyncio.run(_list())
        assert "lumena_search" in tool_names
        assert "lumena_store" in tool_names
        assert "lumena_assemble" in tool_names
        assert "lumena_turn" in tool_names
        assert "lumena_feedback" in tool_names
        assert "lumena_status" in tool_names

    def test_lumena_store_and_search(self, tmp_path, monkeypatch):
        store = tmp_path / "store"
        models = tmp_path / "models"
        store.mkdir()
        models.mkdir()
        monkeypatch.setenv("LUMENA_STORE_PATH", str(store))
        monkeypatch.setenv("LUMENA_MODEL_PATH", str(models))
        # No real ONNX model in tmp_path; opt into the explicit mock escape hatch.
        monkeypatch.setenv("LUMENA_ALLOW_MOCK_EMBEDDER", "1")

        async def _run():
            async with app_lifespan(mcp):
                store_res = await mcp.call_tool(
                    "lumena_store",
                    {
                        "content": "Use SQLite for the cache layer because it is zero-config.",
                        "room": "decisions",
                        "source_type": "user_input",
                    },
                )
                store_text = store_res[0][0].text if store_res and store_res[0] else ""
                assert "Stored chunk_id=" in store_text

                search_res = await mcp.call_tool(
                    "lumena_search",
                    {"query": "SQLite cache", "top_k": 5},
                )
                search_text = search_res[0][0].text if search_res and search_res[0] else ""
                assert "SQLite" in search_text or "No memories found" in search_text

        asyncio.run(_run())

    def test_lumena_status(self, tmp_path, monkeypatch):
        store = tmp_path / "store"
        models = tmp_path / "models"
        store.mkdir()
        models.mkdir()
        monkeypatch.setenv("LUMENA_STORE_PATH", str(store))
        monkeypatch.setenv("LUMENA_MODEL_PATH", str(models))
        # No real ONNX model in tmp_path; opt into the explicit mock escape hatch.
        monkeypatch.setenv("LUMENA_ALLOW_MOCK_EMBEDDER", "1")

        async def _run():
            async with app_lifespan(mcp):
                res = await mcp.call_tool("lumena_status", {})
                text = res[0][0].text if res and res[0] else ""
                assert "Lumena Status" in text
                assert "Rooms:" in text

        asyncio.run(_run())

    def test_invalid_arguments_return_validation_error(self, tmp_path, monkeypatch):
        """Malformed tool args must yield a clean validation message, not a crash."""
        store = tmp_path / "store"
        models = tmp_path / "models"
        store.mkdir()
        models.mkdir()
        monkeypatch.setenv("LUMENA_STORE_PATH", str(store))
        monkeypatch.setenv("LUMENA_MODEL_PATH", str(models))
        monkeypatch.setenv("LUMENA_ALLOW_MOCK_EMBEDDER", "1")

        async def _run():
            async with app_lifespan(mcp):
                # top_k out of the documented 1-50 range
                res = await mcp.call_tool("lumena_search", {"query": "x", "top_k": 999})
                text = res[0][0].text if res and res[0] else ""
                assert "Invalid arguments" in text
                assert "top_k" in text

                # empty content is rejected before any DB write
                res2 = await mcp.call_tool("lumena_store", {"content": "   ", "room": "decisions"})
                text2 = res2[0][0].text if res2 and res2[0] else ""
                assert "Invalid arguments" in text2

                # unknown source_type (outside the documented closed set)
                res3 = await mcp.call_tool(
                    "lumena_store",
                    {"content": "hi", "room": "decisions", "source_type": "bogus"},
                )
                text3 = res3[0][0].text if res3 and res3[0] else ""
                assert "Invalid arguments" in text3
                assert "source_type" in text3

        asyncio.run(_run())


class TestSearchPreview:
    """`_preview` bounds `lumena_search` content to the configurable length."""

    def test_short_content_unchanged(self):
        assert _preview("short", 300) == "short"

    def test_content_at_exact_boundary_not_marked(self):
        exact = "x" * 300
        assert _preview(exact, 300) == exact

    def test_long_content_cut_with_ellipsis(self):
        result = _preview("x" * 400, 300)
        assert result == "x" * 300 + "\u2026"
        assert len(result) == 301

    def test_larger_limit_shows_more(self):
        content = "x" * 1500
        assert _preview(content, 2000) == content
        assert _preview(content, 1000) == "x" * 1000 + "\u2026"

    def test_zero_disables_truncation(self):
        content = "x" * 5000
        assert _preview(content, 0) == content

    def test_negative_disables_truncation(self):
        content = "x" * 5000
        assert _preview(content, -7) == content

    def test_empty_content(self):
        assert _preview("", 300) == ""
