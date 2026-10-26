"""Tests for lummenna.integrations.mcp_server."""

import asyncio
import json

import pytest

pytestmark = pytest.mark.skipif(
    pytest.importorskip("mcp", reason="mcp package not installed") is None,
    reason="mcp package not installed",
)

from lummenna.integrations.mcp_server import _preview, app_lifespan, mcp  # noqa: E402

#: The tool surface every MCP client contracts against. Asserted as an exact
#: set, not a membership subset: a subset check passes when a tool is silently
#: dropped, which is precisely the regression this guards. Adding a tool is a
#: deliberate act, so updating this literal is part of the change.
EXPECTED_TOOLS = {
    "lummenna_search",
    "lummenna_store",
    "lummenna_assemble",
    "lummenna_turn",
    "lummenna_feedback",
    "lummenna_status",
    "lummenna_dashboard",
}

#: Resource URIs published alongside the tools.
EXPECTED_RESOURCES = {
    "dashboard://lummenna/effectiveness",
    "dashboard://lummenna/capabilities",
}


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    """Isolated palace on disk, with the explicit mock-embedder escape hatch.

    tmp_path holds no ONNX model, so initialize_palace would otherwise refuse
    to start. Every test that opens app_lifespan needs this.
    """
    store = tmp_path / "store"
    models = tmp_path / "models"
    store.mkdir()
    models.mkdir()
    monkeypatch.setenv("LUMMENNA_STORE_PATH", str(store))
    monkeypatch.setenv("LUMMENNA_MODEL_PATH", str(models))
    monkeypatch.setenv("LUMMENNA_ALLOW_MOCK_EMBEDDER", "1")
    return tmp_path


class TestMCPServer:
    def test_tools_registered(self):
        async def _list():
            tools = await mcp.list_tools()
            return {t.name for t in tools}

        assert asyncio.run(_list()) == EXPECTED_TOOLS

    def test_every_tool_has_a_description(self):
        """A tool with no description is unusable: the client cannot route to it."""

        async def _list():
            return [(t.name, t.description) for t in await mcp.list_tools()]

        for name, description in asyncio.run(_list()):
            assert description, f"tool {name} exposes no description"

    def test_lummenna_store_and_search(self, mcp_env):
        async def _run():
            async with app_lifespan(mcp):
                store_res = await mcp.call_tool(
                    "lummenna_store",
                    {
                        "content": "Use SQLite for the cache layer because it is zero-config.",
                        "room": "decisions",
                        "source_type": "user_input",
                    },
                )
                store_text = store_res[0][0].text if store_res and store_res[0] else ""
                assert "Stored chunk_id=" in store_text

                search_res = await mcp.call_tool(
                    "lummenna_search",
                    {"query": "SQLite cache", "top_k": 5},
                )
                search_text = search_res[0][0].text if search_res and search_res[0] else ""
                assert "SQLite" in search_text or "No memories found" in search_text

        asyncio.run(_run())

    def test_lummenna_status(self, mcp_env):
        async def _run():
            async with app_lifespan(mcp):
                res = await mcp.call_tool("lummenna_status", {})
                text = res[0][0].text if res and res[0] else ""
                assert "Lummenna Status" in text
                assert "Rooms:" in text

        asyncio.run(_run())

    def test_dashboard_labels_artifact_means_and_unranked_status(self, mcp_env):
        async def _run():
            async with app_lifespan(mcp):
                result = await mcp.call_tool("lummenna_dashboard", {})
                text = result[0][0].text if result and result[0] else ""
                assert "Mean latency Hybrid" in text
                assert "External rank:      N/A" in text
                assert "p50 Latency" not in text
                assert "100% (on-device)" not in text

        asyncio.run(_run())

    def test_invalid_arguments_return_validation_error(self, mcp_env):
        """Malformed tool args must yield a clean validation message, not a crash."""

        async def _run():
            async with app_lifespan(mcp):
                # top_k out of the documented 1-50 range
                res = await mcp.call_tool("lummenna_search", {"query": "x", "top_k": 999})
                text = res[0][0].text if res and res[0] else ""
                assert "Invalid arguments" in text
                assert "top_k" in text

                # empty content is rejected before any DB write
                res2 = await mcp.call_tool(
                    "lummenna_store", {"content": "   ", "room": "decisions"}
                )
                text2 = res2[0][0].text if res2 and res2[0] else ""
                assert "Invalid arguments" in text2

                # unknown source_type (outside the documented closed set)
                res3 = await mcp.call_tool(
                    "lummenna_store",
                    {"content": "hi", "room": "decisions", "source_type": "bogus"},
                )
                text3 = res3[0][0].text if res3 and res3[0] else ""
                assert "Invalid arguments" in text3
                assert "source_type" in text3

        asyncio.run(_run())


class TestMCPResources:
    """The two `dashboard://` resources. Untested until 2026-09-10.

    Resources are read by clients that never call a tool, so a serialisation
    error here is invisible to the tool tests above.
    """

    def test_resources_registered(self):
        async def _list():
            return {str(r.uri) for r in await mcp.list_resources()}

        assert asyncio.run(_list()) == EXPECTED_RESOURCES

    def test_effectiveness_resource_is_valid_json_with_expected_sections(self, mcp_env):
        async def _run():
            async with app_lifespan(mcp):
                contents = list(await mcp.read_resource("dashboard://lummenna/effectiveness"))
                assert len(contents) == 1
                payload = json.loads(contents[0].content)
                # Every top-level section _dashboard_metrics promises.
                assert set(payload) == {
                    "version",
                    "system",
                    "palace",
                    "tfc",
                    "evaluation",
                    "feedback",
                    "runtime",
                    "design_properties",
                }
                assert set(payload["tfc"]) == {"e", "a", "tau", "r"}
                assert payload["palace"]["active_chunks"] == 0
                assert payload["palace"]["released_chunks"] == 0
                assert payload["feedback"]["total"] == 0
                # No feedback logged, so satisfaction is unmeasured, not 0.0.
                assert payload["feedback"]["satisfaction_pct"] is None

        asyncio.run(_run())

    def test_capabilities_resource_exposes_only_its_three_sections(self, mcp_env):
        async def _run():
            async with app_lifespan(mcp):
                contents = list(await mcp.read_resource("dashboard://lummenna/capabilities"))
                assert len(contents) == 1
                payload = json.loads(contents[0].content)
                assert set(payload) == {"design_properties", "evaluation", "runtime"}
                assert payload["runtime"]["edition"] == "community"

        asyncio.run(_run())

    def test_capabilities_resource_makes_no_external_comparison(self, mcp_env):
        """Rule 2 (No False Claims): the payload must not rank Lummenna against
        another system. `_dashboard_metrics` documents this for `hybrid`, and the
        dashboard tool asserts it for the ASCII view; assert it for the resource.
        """

        async def _run():
            async with app_lifespan(mcp):
                contents = list(await mcp.read_resource("dashboard://lummenna/capabilities"))
                raw = contents[0].content.lower()
                for banned in ("faster than", "outperform", "better than", "beats "):
                    assert banned not in raw, f"external comparison leaked: {banned!r}"

        asyncio.run(_run())


class TestSearchPreview:
    """`_preview` bounds `lummenna_search` content to the configurable length."""

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
