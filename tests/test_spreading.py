"""Tests for spreading activation module (D6)."""

import pytest

from lumena.force.mnemonic.spreading import SPREAD_GAMMA, spread_activation


@pytest.fixture
def chain_graph():
    """Return a simple 3-node chain 0–1–2."""
    pytest.importorskip("networkx")
    import networkx as nx

    g = nx.Graph()
    g.add_edge(0, 1, weight=1.0)
    g.add_edge(1, 2, weight=1.0)
    return g


@pytest.fixture
def star_graph():
    """Return a star graph with node 0 connected to 1, 2, 3."""
    pytest.importorskip("networkx")
    import networkx as nx

    g = nx.Graph()
    g.add_edge(0, 1, weight=1.0)
    g.add_edge(0, 2, weight=1.0)
    g.add_edge(0, 3, weight=1.0)
    return g


def test_spread_activation_basic(chain_graph):
    """Test basic spread on a toy NetworkX graph."""
    result = spread_activation(chain_graph, [0], a=1.0)

    assert result[0] == 1.0
    # Hop 1: 1.0 * 0.4^1 * 1.0 = 0.4 > threshold(0.1)
    assert result[1] == pytest.approx(1.0 * SPREAD_GAMMA)
    # Hop 2: 0.4 * 0.4^2 * 1.0 = 0.064 < threshold(0.1), so node 2 not activated
    assert 2 not in result


def test_higher_a_wider_spread(star_graph):
    """Test that higher `a` produces wider spread than lower `a`."""
    low = spread_activation(star_graph, [0], a=0.0)
    high = spread_activation(star_graph, [0], a=1.0)

    # a=0.0 → threshold=1.0, nothing spreads
    assert set(low.keys()) == {0}
    # a=1.0 → threshold=0.1, neighbors spread
    assert set(high.keys()) == {0, 1, 2, 3}


def test_empty_seeds():
    """Empty seeds returns empty dict."""
    pytest.importorskip("networkx")
    import networkx as nx

    g = nx.Graph()
    g.add_edge(0, 1)

    result = spread_activation(g, [], a=0.5)
    assert result == {}


def test_seed_not_in_graph():
    """Seed not in graph returns just the seed with activation 1.0."""
    pytest.importorskip("networkx")
    import networkx as nx

    g = nx.Graph()
    g.add_edge(0, 1)

    result = spread_activation(g, [42], a=0.5)
    assert result == {42: 1.0}


def test_graph_none():
    """graph=None returns seed activations without error."""
    result = spread_activation(None, [1, 2], a=0.5)
    assert result == {1: 1.0, 2: 1.0}


def test_multiple_seeds():
    """Multiple seeds both activate and can spread."""
    pytest.importorskip("networkx")
    import networkx as nx

    g = nx.Graph()
    g.add_edge(10, 20, weight=1.0)
    g.add_edge(20, 30, weight=1.0)

    result = spread_activation(g, [10, 30], a=1.0)

    assert result[10] == 1.0
    assert result[30] == 1.0
    # Node 20 reached from both seeds at hop 1
    assert result[20] == pytest.approx(SPREAD_GAMMA)


def test_spreading_stage_wired_into_search_pipeline(tmp_path, monkeypatch):
    """Stage 2c of SearchPipeline must actually run spreading activation.

    Regression test: search.py used GraphHit without importing it and read
    an unbound seed_ids variable, so every spreading invocation raised
    NameError which was silently swallowed at debug level. This test spies
    on spread_activation to prove the stage executes, and records the
    pipeline's failure log to prove it completes without an exception.
    """
    pytest.importorskip("networkx")
    import lumena.search as search_mod
    from lumena.config import LumenaConfig
    from lumena.data.schema import get_connection, init_db
    from lumena.force.contextual.embed import MockEmbedder
    from lumena.force.mnemonic.retrieval_graph import GraphChannel
    from lumena.force.mnemonic.store import store_memory
    from lumena.search import GraphHit, SearchPipeline  # GraphHit import is the fix

    assert GraphHit is not None

    config = LumenaConfig(
        store_path=tmp_path / "store",
        vector_index="sqlite-vec",
        device="generic",
        allow_mock_embedder=True,
    )
    conn = get_connection(config)
    init_db(conn)
    embedder = MockEmbedder(dims=config.embedding_dims)
    for i in range(5):
        store_memory(
            conn,
            content=f"note {i} about spreading activation graphs",
            room_name="graphs",
            embedding=embedder.encode_single(f"note {i}"),
            config=config,
        )
    conn.commit()

    # Spy on spread_activation: the stage must call it with real seeds.
    calls = {}
    real_spread = spread_activation

    def _spy(graph, seed_ids, a):
        calls["seed_ids"] = list(seed_ids)
        return real_spread(graph, seed_ids, a)

    monkeypatch.setattr("lumena.force.mnemonic.spreading.spread_activation", _spy)

    # Record any swallowed stage failure.
    failures = []
    real_warning = search_mod.logger.warning

    def _capture(event, *args, **kwargs):
        if event == "spreading_activation_failed":
            failures.append(kwargs)
        return real_warning(event, *args, **kwargs)

    monkeypatch.setattr(search_mod.logger, "warning", _capture)

    pipeline = SearchPipeline(conn, config, embedder=embedder, graph=GraphChannel(conn))
    results = pipeline.execute("spreading activation", k=5, max_repair_attempts=0)

    assert isinstance(results, list)
    assert calls.get("seed_ids"), "spreading stage must run with dense-hit seeds"
    assert not failures, f"spreading stage raised internally: {failures}"
    conn.close()
