"""Tests for the first-run embedding-model auto-provision guard in _init."""

from __future__ import annotations

import pytest

from lummenna.brand.errors import ModelNotAvailableError
from lummenna.config import LummennaConfig
from lummenna.force.contextual.embed import MockEmbedder


def _config(tmp_path, **overrides):
    return LummennaConfig(
        store_path=tmp_path / "store",
        model_path=tmp_path / "models",
        **overrides,
    )


def test_guard_disabled_by_default_raises(tmp_path, monkeypatch):
    """Without auto_provision_model, a missing model still fails loudly."""
    monkeypatch.delenv("LUMMENNA_ALLOW_MOCK_EMBEDDER", raising=False)
    from lummenna import _init

    cfg = _config(tmp_path, auto_provision_model=False, allow_mock_embedder=False)
    with pytest.raises(ModelNotAvailableError):
        _init.initialize_palace(cfg)


def test_guard_provisions_then_loads(tmp_path, monkeypatch):
    """When opted in, the guard calls provision then loads the (now present) model."""
    from lummenna import _init

    calls = {"provision": 0, "embed": 0}

    def fake_provision(model_name, dest, config=None):
        calls["provision"] += 1
        # Simulate a successful export producing the on-disk model.
        (dest / model_name).mkdir(parents=True, exist_ok=True)
        (dest / model_name / "model.onnx").write_bytes(b"onnx")
        # The guard passes config=None (opt-in is the consent) so sovereign is
        # not asserted here.
        assert config is None
        return dest / model_name / "model.onnx"

    real_get_embedder = _init.get_embedder

    def fake_get_embedder(config, allow_mock=False):
        # First call (before provision) raises; subsequent call succeeds.
        if calls["provision"] == 0:
            raise ModelNotAvailableError("missing model")
        calls["embed"] += 1
        return MockEmbedder(dims=config.embedding_dims)

    monkeypatch.setattr("lummenna.cli.models.provision_embedding_model", fake_provision)
    monkeypatch.setattr(_init, "get_embedder", fake_get_embedder)

    cfg = _config(tmp_path, auto_provision_model=True, allow_mock_embedder=False)
    palace = _init.initialize_palace(cfg)

    assert calls["provision"] == 1
    assert calls["embed"] == 1
    assert palace["embedding_model_available"] is True
    palace["conn"].close()

    _init.get_embedder = real_get_embedder


def test_guard_falls_back_when_provision_fails(tmp_path, monkeypatch):
    """If provisioning fails, the guard falls through to the normal error path."""
    from lummenna import _init

    def fake_provision(model_name, dest, config=None):
        raise RuntimeError("no network")

    def always_missing(config, allow_mock=False):
        raise ModelNotAvailableError("missing model")

    monkeypatch.setattr("lummenna.cli.models.provision_embedding_model", fake_provision)
    monkeypatch.setattr(_init, "get_embedder", always_missing)

    cfg = _config(tmp_path, auto_provision_model=True, allow_mock_embedder=False)
    with pytest.raises(ModelNotAvailableError):
        _init.initialize_palace(cfg)
