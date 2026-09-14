"""Tests for lummenna.force.contextual.token_budget."""

import pytest

import lummenna.force.contextual.token_budget as token_budget
from lummenna.force.contextual.token_budget import (
    CharHeuristicCounter,
    get_token_counter,
)


@pytest.fixture(autouse=True)
def clear_counter_state():
    """Isolate the module-level counter cache and warn-once set between tests."""
    token_budget._COUNTER_CACHE.clear()
    token_budget._WARNED_PATHS.clear()
    yield
    token_budget._COUNTER_CACHE.clear()
    token_budget._WARNED_PATHS.clear()


class TestCharHeuristicCounter:
    def test_basic_count(self):
        counter = CharHeuristicCounter()
        # "hello world" = 11 chars -> ~2-3 tokens
        assert counter.count("hello world") == 2

    def test_empty_string(self):
        counter = CharHeuristicCounter()
        assert counter.count("") == 1

    def test_long_text(self):
        counter = CharHeuristicCounter()
        text = "a" * 400
        assert counter.count(text) == 100


class TestTransformersTokenizerCounter:
    def test_real_tokenizer(self, tmp_path):
        # We can't easily ship a real tokenizer in tests, so we test the
        # fallback path when the model_path does not exist.
        counter = get_token_counter(tmp_path / "nonexistent")
        assert isinstance(counter, CharHeuristicCounter)

    def test_get_token_counter_with_none(self):
        counter = get_token_counter(None)
        assert isinstance(counter, CharHeuristicCounter)


class TestTokenBudgetIntegration:
    def test_budget_enforcement(self):
        counter = CharHeuristicCounter()
        text = "x" * 400  # 100 tokens
        assert counter.count(text) == 100
        assert counter.count(text * 2) == 200


class TestCounterCaching:
    """Context assembly runs per turn, so tokenizer loading must not repeat."""

    def test_successful_loads_are_cached(self, tmp_path, monkeypatch):
        model_dir = tmp_path / "tokenizer"
        model_dir.mkdir()
        loads = []

        class FakeCounter:
            def __init__(self, path):
                loads.append(path)

            def count(self, text: str) -> int:
                return len(text)

        monkeypatch.setattr(token_budget, "TransformersTokenizerCounter", FakeCounter)

        first = get_token_counter(model_dir)
        second = get_token_counter(model_dir)

        assert first is second
        assert len(loads) == 1

    def test_cache_is_invalidated_when_the_model_changes(self, tmp_path, monkeypatch):
        import os

        model_dir = tmp_path / "tokenizer"
        model_dir.mkdir()

        class FakeCounter:
            def __init__(self, path):
                pass

            def count(self, text: str) -> int:
                return len(text)

        monkeypatch.setattr(token_budget, "TransformersTokenizerCounter", FakeCounter)

        first = get_token_counter(model_dir)
        os.utime(model_dir, (0, 0))
        second = get_token_counter(model_dir)

        assert first is not second

    def test_cache_is_bounded(self, tmp_path, monkeypatch):
        class FakeCounter:
            def __init__(self, path):
                pass

            def count(self, text: str) -> int:
                return len(text)

        monkeypatch.setattr(token_budget, "TransformersTokenizerCounter", FakeCounter)

        for index in range(token_budget._COUNTER_CACHE_MAX + 4):
            model_dir = tmp_path / f"tokenizer{index}"
            model_dir.mkdir()
            get_token_counter(model_dir)

        assert len(token_budget._COUNTER_CACHE) <= token_budget._COUNTER_CACHE_MAX

    def test_failed_loads_are_not_cached_as_success(self, tmp_path, monkeypatch):
        model_dir = tmp_path / "broken"
        model_dir.mkdir()

        class ExplodingCounter:
            def __init__(self, path):
                raise RuntimeError("no tokenizer here")

        monkeypatch.setattr(token_budget, "TransformersTokenizerCounter", ExplodingCounter)

        assert isinstance(get_token_counter(model_dir), CharHeuristicCounter)
        assert not token_budget._COUNTER_CACHE


def _capture_warnings(monkeypatch) -> list[str]:
    """Record warning event names emitted by the token_budget logger.

    The module uses structlog, which does not route through pytest's caplog, so
    the logger itself is substituted.
    """
    events: list[str] = []

    class RecordingLogger:
        def warning(self, event, **_fields):
            events.append(event)

        def info(self, event, **_fields):
            pass

        def debug(self, event, **_fields):
            pass

    monkeypatch.setattr(token_budget, "logger", RecordingLogger())
    return events


class TestFallbackIsVisible:
    """Falling back to the heuristic makes the budget an estimate, not a bound.

    That is a correctness event and must be logged rather than passed over in
    silence, but only once per path so it cannot flood a per-turn code path.
    """

    def test_missing_path_warns_once(self, tmp_path, monkeypatch):
        events = _capture_warnings(monkeypatch)
        missing = tmp_path / "nonexistent"

        get_token_counter(missing)
        get_token_counter(missing)

        assert events == ["tokenizer_path_missing_using_char_heuristic"]

    def test_load_failure_warns_once(self, tmp_path, monkeypatch):
        events = _capture_warnings(monkeypatch)
        model_dir = tmp_path / "broken"
        model_dir.mkdir()

        class ExplodingCounter:
            def __init__(self, path):
                raise RuntimeError("corrupt vocab")

        monkeypatch.setattr(token_budget, "TransformersTokenizerCounter", ExplodingCounter)

        get_token_counter(model_dir)
        get_token_counter(model_dir)

        assert events == ["tokenizer_load_failed"]

    def test_warned_paths_set_is_bounded(self, tmp_path):
        for index in range(token_budget._WARNED_PATHS_MAX + 10):
            get_token_counter(tmp_path / f"missing{index}")

        assert len(token_budget._WARNED_PATHS) <= token_budget._WARNED_PATHS_MAX

    def test_none_path_does_not_warn(self, monkeypatch):
        events = _capture_warnings(monkeypatch)

        get_token_counter(None)

        assert events == []
